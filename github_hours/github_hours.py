#!/usr/bin/env python3
import argparse
import json
import os
import re
import sys
import tempfile
import threading
import time as time_module
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo
import requests

GITHUB_API = "https://api.github.com"
PT = ZoneInfo("America/Los_Angeles")
CACHE_VERSION = 1
CACHE_TTL = timedelta(hours=24)
COMMIT_FETCH_WORKERS = 8

# Monday=0 ... Sunday=6. None = no baseline (0h unless commits exist).
WORKING_HOURS = {
    0: {"start": time(8, 0),  "end": time(17, 0)},  # Mon
    1: {"start": time(7, 30), "end": time(17, 0)},  # Tue
    2: {"start": time(8, 0),  "end": time(17, 0)},  # Wed
    3: {"start": time(8, 0),  "end": time(17, 0)},  # Thu
    4: {"start": time(8, 0),  "end": time(17, 0)},  # Fri
    5: None,  # Sat
    6: None,  # Sun
}

_thread_state = threading.local()


def fmt_datetime_pt(dt):
    if dt is None:
        return "N/A"
    date_part = dt.strftime("%m/%d/%Y")
    time_part = dt.strftime("%I:%M %p").lstrip("0")  # 08:00 AM -> 8:00 AM
    return f"{date_part} {time_part} PT"


def parse_mmddyyyy(value: str):
    return datetime.strptime(value, "%m/%d/%Y").date()


def week_bounds_sunday(day_pt):
    """Return (Sunday, Saturday) for the week containing day_pt."""
    # Monday=0 ... Sunday=6 → days since Sunday
    sunday = day_pt - timedelta(days=(day_pt.weekday() + 1) % 7)
    saturday = sunday + timedelta(days=6)
    return sunday, saturday


def to_utc_z(dt):
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def github_headers(token: str):
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def thread_session():
    session = getattr(_thread_state, "session", None)
    if session is None:
        session = requests.Session()
        _thread_state.session = session
    return session


class RequestTiming:
    def __init__(self):
        self._lock = threading.Lock()
        self.calls = []
        self.cache_notes = []

    def record(self, phase, path, seconds, started=None):
        if started is None:
            started = time_module.perf_counter() - seconds
        with self._lock:
            self.calls.append((phase, path, seconds, started))

    def note_cache(self, day, status):
        with self._lock:
            self.cache_notes.append((day, status))

    def render(self):
        grouped = {}
        for call in self.calls:
            grouped.setdefault(call[0], []).append(call)

        lines = ["", "Timing:"]
        seen = set()
        for phase in ("discovery", "branch_commits", "pr_commits", *sorted(grouped)):
            if phase in seen or phase not in grouped:
                continue
            seen.add(phase)
            calls = grouped[phase]
            lines.append(f"  {phase}: {len(calls)} calls, {self._wall(calls):.2f}s")
        lines.append(f"  total: {len(self.calls)} calls, {self._wall(self.calls):.2f}s")
        if self.cache_notes:
            notes = ", ".join(
                f"{day.strftime('%m/%d/%Y')} {status}" for day, status in self.cache_notes
            )
            lines.append(f"  cache: {notes}")
        slowest = sorted(self.calls, key=lambda item: item[2], reverse=True)[:5]
        if slowest:
            lines.append("Slowest:")
            for phase, path, seconds, _started in slowest:
                lines.append(f"  {seconds:.2f}s {path} ({phase})")
        return "\n".join(lines)

    @staticmethod
    def _wall(calls):
        if not calls:
            return 0.0
        start = min(started for _phase, _path, _seconds, started in calls)
        end = max(started + seconds for _phase, _path, seconds, started in calls)
        return end - start


class GitHubClient:
    def __init__(self, token: str, timing: RequestTiming | None = None):
        self.headers = github_headers(token)
        self.timing = timing

    def get(self, url: str, params=None, phase="other"):
        started = time_module.perf_counter()
        try:
            response = thread_session().get(
                url, headers=self.headers, params=params, timeout=30
            )
        except requests.RequestException:
            self._record(phase, url, started)
            raise
        self._record(phase, url, started)
        response.raise_for_status()
        return response.json()

    def paginate(self, url: str, params=None, max_pages=50, phase="other"):
        params = dict(params or {})
        params.setdefault("per_page", 100)
        items = []
        page = 1
        while page <= max_pages:
            params["page"] = page
            batch = self.get(url, params, phase=phase)
            if not isinstance(batch, list):
                break
            items.extend(batch)
            if len(batch) < params["per_page"]:
                break
            page += 1
        return items

    def _record(self, phase, url, started):
        if self.timing is None:
            return
        self.timing.record(
            phase,
            urlparse(url).path,
            time_module.perf_counter() - started,
            started=started,
        )


def day_window_pt(day_pt):
    start_pt = datetime.combine(day_pt, time(0, 0), tzinfo=PT)
    end_pt = start_pt + timedelta(days=1)
    return start_pt, end_pt


def discover_repos_from_commit_search(client: GitHubClient, username: str, org: str, start_utc: str, end_utc: str):
    """Default-branch commit search still helps discover repos with merged activity."""
    query = f"author:{username} org:{org} committer-date:{start_utc}..{end_utc}"
    repos = set()
    page = 1
    while True:
        data = client.get(
            f"{GITHUB_API}/search/commits",
            params={"q": query, "per_page": 100, "page": page},
            phase="discovery",
        )
        batch = data.get("items", [])
        for it in batch:
            repos.add(it["repository"]["full_name"])
        if len(batch) < 100:
            break
        page += 1
    return repos


def discover_repos_from_events(client: GitHubClient, username: str, org: str):
    """
    User events include pushes to non-default branches.
    Limited to ~300 events / ~90 days by GitHub.
    """
    repos = set()
    # Prefer the user's own event stream (private events when authenticated as that user).
    try:
        events = client.paginate(
            f"{GITHUB_API}/users/{username}/events",
            params={"per_page": 100},
            max_pages=3,  # GitHub caps this resource around 300 items
            phase="discovery",
        )
    except requests.HTTPError:
        events = []

    prefix = f"{org}/"
    for event in events:
        if event.get("type") != "PushEvent":
            continue
        if event.get("actor", {}).get("login") != username:
            continue
        name = event.get("repo", {}).get("name", "")
        if name.startswith(prefix):
            repos.add(name)
    return repos


def discover_repos_and_prs(client: GitHubClient, username: str, org: str, day_pt):
    """Find PRs updated around the day; their commits exist even before merge."""
    # Widen slightly so PRs touched just outside the PT day still surface.
    start = (day_pt - timedelta(days=2)).isoformat()
    end = (day_pt + timedelta(days=1)).isoformat()
    query = f"author:{username} org:{org} type:pr updated:{start}..{end}"
    repos = set()
    prs = []  # (full_name, number)
    page = 1
    while True:
        data = client.get(
            f"{GITHUB_API}/search/issues",
            params={"q": query, "per_page": 100, "page": page},
            phase="discovery",
        )
        batch = data.get("items", [])
        for it in batch:
            # repository_url like https://api.github.com/repos/org/repo
            full_name = "/".join(it["repository_url"].rstrip("/").split("/")[-2:])
            repos.add(full_name)
            prs.append((full_name, it["number"]))
        if len(batch) < 100:
            break
        page += 1
    return repos, prs


def list_branches(client: GitHubClient, full_name: str):
    owner, repo = full_name.split("/", 1)
    branches = client.paginate(
        f"{GITHUB_API}/repos/{owner}/{repo}/branches",
        params={"per_page": 100},
        phase="discovery",
    )
    return [b["name"] for b in branches]


def fetch_commits_on_ref(client: GitHubClient, full_name: str, username: str, ref: str, start_utc: str, end_utc: str):
    owner, repo = full_name.split("/", 1)
    return client.paginate(
        f"{GITHUB_API}/repos/{owner}/{repo}/commits",
        params={
            "author": username,
            "sha": ref,
            "since": start_utc,
            "until": end_utc,
            "per_page": 100,
        },
        phase="branch_commits",
    )


def fetch_pr_commits(client: GitHubClient, full_name: str, number: int):
    owner, repo = full_name.split("/", 1)
    return client.paginate(
        f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{number}/commits",
        params={"per_page": 100},
        phase="pr_commits",
    )


def commit_to_point(commit: dict, full_name: str, start_pt, end_pt):
    ts_utc = datetime.fromisoformat(
        commit["commit"]["committer"]["date"].replace("Z", "+00:00")
    )
    ts_pt = ts_utc.astimezone(PT)
    if start_pt <= ts_pt < end_pt:
        return commit["sha"], (ts_pt, full_name)
    return None, None


def _branch_commit_points(client, full_name, username, branch, start_utc, end_utc, start_pt, end_pt):
    try:
        commits = fetch_commits_on_ref(
            client, full_name, username, branch, start_utc, end_utc
        )
    except requests.HTTPError:
        return []
    points = []
    for commit in commits:
        sha, point = commit_to_point(commit, full_name, start_pt, end_pt)
        if sha:
            points.append((sha, point))
    return points


def _pr_commit_points(client, full_name, number, username, start_pt, end_pt):
    try:
        commits = fetch_pr_commits(client, full_name, number)
    except requests.HTTPError:
        return []
    points = []
    for commit in commits:
        # PR commit list is not author-filtered; keep only this user.
        author_login = (commit.get("author") or {}).get("login")
        if author_login != username:
            continue
        sha, point = commit_to_point(commit, full_name, start_pt, end_pt)
        if sha:
            points.append((sha, point))
    return points


def fetch_commit_items_live(token: str, username: str, org: str, day_pt, timing=None):
    """
    Collect commits authored by username in org on the PT day.

    GitHub's commit search API only indexes the default branch, so we also:
    - discover repos via user PushEvents and PR search
    - list commits on every branch of those repos in the day window
    - include commits attached to the user's PRs updated around that day
    """
    client = GitHubClient(token, timing)
    start_pt, end_pt = day_window_pt(day_pt)
    start_utc = to_utc_z(start_pt)
    end_utc = to_utc_z(end_pt)

    repos = set()
    repos |= discover_repos_from_commit_search(client, username, org, start_utc, end_utc)
    repos |= discover_repos_from_events(client, username, org)
    pr_repos, prs = discover_repos_and_prs(client, username, org, day_pt)
    repos |= pr_repos

    jobs = []
    for full_name in sorted(repos):
        try:
            branches = list_branches(client, full_name)
        except requests.HTTPError:
            continue
        for branch in branches:
            jobs.append(("branch", full_name, branch))
    for full_name, number in prs:
        jobs.append(("pr", full_name, number))

    by_sha = {}
    if jobs:
        with ThreadPoolExecutor(max_workers=COMMIT_FETCH_WORKERS) as pool:
            futures = []
            for kind, full_name, target in jobs:
                if kind == "branch":
                    futures.append(pool.submit(
                        _branch_commit_points,
                        client, full_name, username, target, start_utc, end_utc, start_pt, end_pt,
                    ))
                else:
                    futures.append(pool.submit(
                        _pr_commit_points,
                        client, full_name, target, username, start_pt, end_pt,
                    ))
            for future in as_completed(futures):
                for sha, point in future.result():
                    by_sha[sha] = point

    return sorted(by_sha.values(), key=lambda x: x[0])


def default_cache_dir():
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches"
    elif os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        base = Path(local) if local else Path.home() / "AppData" / "Local"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return base / "github_hours"


def commit_cache_path(directory, username: str, org: str, day_pt):
    def clean(value: str):
        return re.sub(r"[^A-Za-z0-9._-]", "_", value)

    name = f"v{CACHE_VERSION}-{clean(username)}-{clean(org)}-{day_pt.isoformat()}.json"
    return Path(directory) / name


def read_commit_cache(path, now, ttl):
    try:
        data = json.loads(Path(path).read_text())
        if data.get("version") != CACHE_VERSION:
            return None
        cached_at = datetime.fromisoformat(data["cached_at"])
        if cached_at.tzinfo is None:
            cached_at = cached_at.replace(tzinfo=timezone.utc)
        if now - cached_at >= ttl:
            return None
        points = []
        for item in data["commits"]:
            repo = item["repo"]
            if not isinstance(repo, str):
                return None
            points.append((datetime.fromisoformat(item["committed_at"]), repo))
        return points
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError, AttributeError):
        return None


def write_commit_cache(path, points, cached_at):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": CACHE_VERSION,
        "cached_at": cached_at.isoformat(),
        "commits": [
            {"committed_at": ts.isoformat(), "repo": repo}
            for ts, repo in points
        ],
    }
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload))
    temporary.replace(path)


def load_day_commits(path, *, mode, now, ttl, fetch, timing=None, day=None):
    if mode not in {"normal", "refresh", "bypass"}:
        raise ValueError(f"Unknown cache mode: {mode}")
    if mode == "normal":
        cached = read_commit_cache(path, now, ttl)
        if cached is not None:
            if timing is not None and day is not None:
                timing.note_cache(day, "hit")
            return cached

    points = fetch()
    if mode != "bypass":
        try:
            write_commit_cache(path, points, now)
        except OSError as exc:
            print(f"Warning: could not write cache {path}: {exc}", file=sys.stderr)
        status = "refresh" if mode == "refresh" else "miss"
    else:
        status = "bypass"
    if timing is not None and day is not None:
        timing.note_cache(day, status)
    return points


def effective_cache_mode(requested_mode, day, today):
    if requested_mode == "bypass":
        return "bypass"
    if requested_mode == "refresh" or day == today:
        return "refresh"
    return "normal"


def fetch_commit_items(token: str, username: str, org: str, day_pt, *, cache_mode="normal", timing=None, cache_directory=None, now=None):
    directory = Path(cache_directory) if cache_directory is not None else default_cache_dir()
    path = commit_cache_path(directory, username, org, day_pt)
    now = now or datetime.now(timezone.utc)
    return load_day_commits(
        path,
        mode=cache_mode,
        now=now,
        ttl=CACHE_TTL,
        fetch=lambda: fetch_commit_items_live(token, username, org, day_pt, timing=timing),
        timing=timing,
        day=day_pt,
    )


def get_baseline_window(day_pt):
    w = WORKING_HOURS[day_pt.weekday()]
    if w is None:
        return None, None
    start_baseline = datetime.combine(day_pt, w["start"], tzinfo=PT)
    end_baseline = datetime.combine(day_pt, w["end"], tzinfo=PT)
    return start_baseline, end_baseline


def _span_hours(times):
    if not times:
        return 0.0
    if len(times) == 1:
        return 0.5  # minimum credit for a single outside-baseline commit
    return (max(times) - min(times)).total_seconds() / 3600.0


def compute_hours(day_pt, commit_points):
    start_baseline, end_baseline = get_baseline_window(day_pt)

    if not commit_points:
        if start_baseline is None:
            raw_hours = 0.0
            effective_start = effective_end = None
        else:
            raw_hours = (end_baseline - start_baseline).total_seconds() / 3600.0
            effective_start, effective_end = start_baseline, end_baseline
        return {
            "commit_count": 0,
            "first_commit_pt": None,
            "last_commit_pt": None,
            "effective_start_pt": effective_start,
            "effective_end_pt": effective_end,
            "hours": raw_hours,
            "baseline_hours": raw_hours,
            "overtime_hours": 0.0,
            "repos": [],
            "effective_window_pt": {
                "start": effective_start,
                "end": effective_end,
            },
        }

    times = [t for t, _ in commit_points]
    repos = sorted({r for _, r in commit_points})

    first_commit = min(times)
    last_commit = max(times)

    if start_baseline is None:
        # Weekend / no baseline: hours are just first→last commit.
        baseline_hours = 0.0
        overtime_hours = _span_hours(times)
        effective_start = first_commit
        effective_end = last_commit
    else:
        baseline_hours = (end_baseline - start_baseline).total_seconds() / 3600.0
        pre = [t for t in times if t < start_baseline]
        post = [t for t in times if t > end_baseline]
        # Do not stretch the baseline across gaps. Only add the span of
        # commits that fall before start and/or after end.
        overtime_hours = _span_hours(pre) + _span_hours(post)
        effective_start = start_baseline
        effective_end = end_baseline

    raw_hours = baseline_hours + overtime_hours

    return {
        "commit_count": len(times),
        "first_commit_pt": first_commit,
        "last_commit_pt": last_commit,
        "effective_start_pt": effective_start,
        "effective_end_pt": effective_end,
        "hours": raw_hours,
        "baseline_hours": baseline_hours,
        "overtime_hours": overtime_hours,
        "repos": repos,
        "effective_window_pt": {
            "start": effective_start,
            "end": effective_end,
        },
    }


def self_test():
    # Week bounds: a Wednesday belongs to Sun 07/26 – Sat 08/01
    wed = datetime(2026, 7, 29).date()
    sun, sat = week_bounds_sunday(wed)
    assert sun == datetime(2026, 7, 26).date()
    assert sat == datetime(2026, 8, 1).date()
    assert week_bounds_sunday(sun) == (sun, sat)

    d = datetime(2026, 7, 26).date()  # Sunday
    # Weekend, no commits => 0h
    r0 = compute_hours(d, [])
    assert r0["hours"] == 0.0, f"Expected 0.0, got {r0['hours']}"

    # Weekend commits 21:28 and 23:33 => span only (2.083...h)
    sample_weekend = [
        (datetime(2026, 7, 26, 21, 28, tzinfo=PT), "captiv8io/a"),
        (datetime(2026, 7, 26, 23, 33, tzinfo=PT), "captiv8io/b"),
    ]
    rw = compute_hours(d, sample_weekend)
    assert abs(rw["hours"] - 2.0833333333) < 1e-9, f"Expected ~2.0833, got {rw['hours']}"

    weekday = datetime(2026, 7, 27).date()  # Monday
    # commits inside baseline => 9.0h only
    sample1 = [
        (datetime(2026, 7, 27, 9, 0, tzinfo=PT), "captiv8io/a"),
        (datetime(2026, 7, 27, 16, 0, tzinfo=PT), "captiv8io/b"),
    ]
    r1 = compute_hours(weekday, sample1)
    assert r1["hours"] == 9.0, f"Expected 9.0, got {r1['hours']}"
    assert r1["overtime_hours"] == 0.0

    # one commit before + one after baseline => 0.5 + 0.5 overtime
    sample2 = [
        (datetime(2026, 7, 27, 7, 30, tzinfo=PT), "captiv8io/a"),
        (datetime(2026, 7, 27, 19, 15, tzinfo=PT), "captiv8io/b"),
    ]
    r2 = compute_hours(weekday, sample2)
    assert r2["hours"] == 10.0, f"Expected 10.0, got {r2['hours']}"
    assert r2["overtime_hours"] == 1.0

    # pre 07:00–07:30 (0.5) + post 19:00–20:00 (1.0) + baseline 9 => 10.5
    sample3 = [
        (datetime(2026, 7, 27, 7, 0, tzinfo=PT), "captiv8io/a"),
        (datetime(2026, 7, 27, 7, 30, tzinfo=PT), "captiv8io/a"),
        (datetime(2026, 7, 27, 19, 0, tzinfo=PT), "captiv8io/b"),
        (datetime(2026, 7, 27, 20, 0, tzinfo=PT), "captiv8io/b"),
    ]
    r3 = compute_hours(weekday, sample3)
    assert r3["hours"] == 10.5, f"Expected 10.5, got {r3['hours']}"
    assert r3["baseline_hours"] == 9.0
    assert r3["overtime_hours"] == 1.5

    _self_test_cache()
    print("Self-test passed.")


def _self_test_cache():
    today = datetime(2026, 10, 26).date()
    past = datetime(2026, 10, 19).date()
    assert effective_cache_mode("normal", past, today) == "normal"
    assert effective_cache_mode("normal", today, today) == "refresh"
    assert effective_cache_mode("refresh", past, today) == "refresh"
    assert effective_cache_mode("bypass", today, today) == "bypass"

    timing = RequestTiming()
    timing.record("discovery", "/search/commits", 0.2, started=0.0)
    timing.record("branch_commits", "/repos/captiv8io/captiv8-data/commits", 0.4, started=0.2)
    timing.note_cache(past, "miss")
    report = timing.render()
    assert "discovery: 1 calls, 0.20s" in report
    assert "branch_commits: 1 calls, 0.40s" in report
    assert "total: 2 calls, 0.60s" in report
    assert "0.40s /repos/captiv8io/captiv8-data/commits (branch_commits)" in report
    assert "10/19/2026 miss" in report

    overlap = RequestTiming()
    overlap.record("branch_commits", "/a", 0.5, started=1.0)
    overlap.record("branch_commits", "/b", 0.5, started=1.1)
    overlap.record("pr_commits", "/c", 0.2, started=1.2)
    overlap_report = overlap.render()
    assert "branch_commits: 2 calls, 0.60s" in overlap_report
    assert "pr_commits: 1 calls, 0.20s" in overlap_report
    assert "total: 3 calls, 0.60s" in overlap_report

    with tempfile.TemporaryDirectory() as directory:
        directory = Path(directory)
        path = commit_cache_path(directory, "alexbegg", "captiv8io", past)
        assert path.name == "v1-alexbegg-captiv8io-2026-10-19.json"
        now = datetime(2026, 10, 26, 20, 0, tzinfo=timezone.utc)
        original = [(datetime(2026, 10, 19, 9, 0, tzinfo=PT), "captiv8io/a")]
        updated = [(datetime(2026, 10, 19, 10, 0, tzinfo=PT), "captiv8io/b")]
        calls = {"n": 0}

        def fetch_original():
            calls["n"] += 1
            return list(original)

        assert load_day_commits(path, mode="normal", now=now, ttl=CACHE_TTL, fetch=fetch_original) == original
        assert calls["n"] == 1
        still_fresh = now + timedelta(hours=23, minutes=59)
        assert load_day_commits(path, mode="normal", now=still_fresh, ttl=CACHE_TTL, fetch=fetch_original) == original
        assert calls["n"] == 1

        def fetch_updated():
            calls["n"] += 1
            return list(updated)

        expired_now = now + CACHE_TTL
        assert load_day_commits(path, mode="normal", now=expired_now, ttl=CACHE_TTL, fetch=fetch_updated) == updated
        assert calls["n"] == 2

        def fetch_bypass():
            calls["n"] += 1
            return []

        before = path.read_text()
        assert load_day_commits(path, mode="bypass", now=expired_now, ttl=CACHE_TTL, fetch=fetch_bypass) == []
        assert path.read_text() == before
        assert calls["n"] == 3

        def fetch_refresh():
            calls["n"] += 1
            return list(original)

        assert load_day_commits(path, mode="refresh", now=expired_now, ttl=CACHE_TTL, fetch=fetch_refresh) == original
        assert calls["n"] == 4
        assert load_day_commits(path, mode="normal", now=expired_now, ttl=CACHE_TTL, fetch=fetch_refresh) == original
        assert calls["n"] == 4

        path.write_text("{not json")
        assert load_day_commits(path, mode="normal", now=expired_now, ttl=CACHE_TTL, fetch=fetch_updated) == updated
        assert calls["n"] == 5
        assert read_commit_cache(path, expired_now, CACHE_TTL) == updated


DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def print_day_detail(day, result, org):
    print(f"Date (PT): {day.strftime('%m/%d/%Y')}")
    print(f"Org: {org}")
    print(f"Commits found: {result['commit_count']}")
    print(f"\nHours: {result['hours']:.2f}")
    print(f"  Baseline: {result['baseline_hours']:.2f}")
    print(f"  Overtime (outside baseline commit spans): {result['overtime_hours']:.2f}\n")
    print(f"First commit PT: {fmt_datetime_pt(result['first_commit_pt'])}")
    print(f"Last commit PT: {fmt_datetime_pt(result['last_commit_pt'])}")
    print(f"Baseline start PT: {fmt_datetime_pt(result['effective_start_pt'])}")
    print(f"Baseline end PT: {fmt_datetime_pt(result['effective_end_pt'])}")
    print(f"Repos touched: {', '.join(result['repos']) if result['repos'] else '(none)'}")


def print_week_summary(token, username, org, any_day, *, cache_mode="normal", timing=None):
    sunday, saturday = week_bounds_sunday(any_day)
    today_pt = datetime.now(PT).date()
    print(f"Week (PT): {sunday.strftime('%m/%d/%Y')} – {saturday.strftime('%m/%d/%Y')}")
    print(f"Org: {org}")
    print(f"User: {username}\n", flush=True)

    total = 0.0
    for offset in range(7):
        day = sunday + timedelta(days=offset)
        if day > today_pt:
            break
        commit_points = fetch_commit_items(
            token,
            username,
            org,
            day,
            cache_mode=effective_cache_mode(cache_mode, day, today_pt),
            timing=timing,
        )
        result = compute_hours(day, commit_points)
        total += result["hours"]
        # weekday(): Mon=0 ... Sun=6; week walks Sun..Sat
        name = DAY_NAMES[day.weekday()]
        print(
            f"{name} {day.strftime('%m/%d/%Y')}  "
            f"{result['hours']:5.2f}h  "
            f"(commits: {result['commit_count']}, "
            f"baseline: {result['baseline_hours']:.2f}, "
            f"overtime: {result['overtime_hours']:.2f})",
            flush=True,
        )
        if day == today_pt:
            break

    print(f"\nTotal: {total:.2f}h", flush=True)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--username", default="alexbegg", help="GitHub username")
    p.add_argument("--date", help="MM/DD/YYYY, e.g. 07/26/2026")
    p.add_argument(
        "--week",
        metavar="MM/DD/YYYY",
        help="Summarize Sunday–Saturday hours for the week containing this date",
    )
    p.add_argument("--org", default="captiv8io", help="GitHub organization")
    p.add_argument("--self-test", action="store_true")
    p.add_argument(
        "--debug-timing",
        action="store_true",
        help="Print GitHub call counts, phase timings, and the slowest endpoints",
    )
    p.add_argument(
        "--refresh",
        action="store_true",
        help="Ignore cached days and replace them",
    )
    p.add_argument(
        "--no-cache",
        action="store_true",
        help="Do not read or write the day cache",
    )
    args = p.parse_args()

    if args.self_test:
        self_test()
        return

    if bool(args.date) == bool(args.week):
        raise SystemExit("Provide exactly one of --date or --week MM/DD/YYYY")
    if args.refresh and args.no_cache:
        raise SystemExit("Use only one of --refresh or --no-cache")

    token = os.getenv("GITHUB_TOKEN")
    if not token:
        raise SystemExit("Set GITHUB_TOKEN in your environment.")

    if args.no_cache:
        cache_mode = "bypass"
    elif args.refresh:
        cache_mode = "refresh"
    else:
        cache_mode = "normal"
    timing = RequestTiming() if args.debug_timing else None

    if args.week:
        print_week_summary(
            token, args.username, args.org, parse_mmddyyyy(args.week),
            cache_mode=cache_mode, timing=timing,
        )
    else:
        day = parse_mmddyyyy(args.date)
        commit_points = fetch_commit_items(
            token,
            args.username,
            args.org,
            day,
            cache_mode=effective_cache_mode(cache_mode, day, datetime.now(PT).date()),
            timing=timing,
        )
        result = compute_hours(day, commit_points)
        print_day_detail(day, result, args.org)

    if timing is not None:
        print(timing.render(), flush=True)


if __name__ == "__main__":
    main()
