#!/usr/bin/env python3
import argparse
import os
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
import requests

GITHUB_API = "https://api.github.com"
PT = ZoneInfo("America/Los_Angeles")

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


def github_get(url: str, headers: dict, params=None):
    r = requests.get(url, headers=headers, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def github_paginate(url: str, headers: dict, params=None, max_pages=50):
    params = dict(params or {})
    params.setdefault("per_page", 100)
    items = []
    page = 1
    while page <= max_pages:
        params["page"] = page
        batch = github_get(url, headers, params)
        if not isinstance(batch, list):
            break
        items.extend(batch)
        if len(batch) < params["per_page"]:
            break
        page += 1
    return items


def day_window_pt(day_pt):
    start_pt = datetime.combine(day_pt, time(0, 0), tzinfo=PT)
    end_pt = start_pt + timedelta(days=1)
    return start_pt, end_pt


def discover_repos_from_commit_search(headers: dict, username: str, org: str, start_utc: str, end_utc: str):
    """Default-branch commit search still helps discover repos with merged activity."""
    query = f"author:{username} org:{org} committer-date:{start_utc}..{end_utc}"
    repos = set()
    page = 1
    while True:
        data = github_get(
            f"{GITHUB_API}/search/commits",
            headers,
            params={"q": query, "per_page": 100, "page": page},
        )
        batch = data.get("items", [])
        for it in batch:
            repos.add(it["repository"]["full_name"])
        if len(batch) < 100:
            break
        page += 1
    return repos


def discover_repos_from_events(headers: dict, username: str, org: str):
    """
    User events include pushes to non-default branches.
    Limited to ~300 events / ~90 days by GitHub.
    """
    repos = set()
    # Prefer the user's own event stream (private events when authenticated as that user).
    try:
        events = github_paginate(
            f"{GITHUB_API}/users/{username}/events",
            headers,
            params={"per_page": 100},
            max_pages=3,  # GitHub caps this resource around 300 items
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


def discover_repos_and_prs(headers: dict, username: str, org: str, day_pt):
    """Find PRs updated around the day; their commits exist even before merge."""
    # Widen slightly so PRs touched just outside the PT day still surface.
    start = (day_pt - timedelta(days=2)).isoformat()
    end = (day_pt + timedelta(days=1)).isoformat()
    query = f"author:{username} org:{org} type:pr updated:{start}..{end}"
    repos = set()
    prs = []  # (full_name, number)
    page = 1
    while True:
        data = github_get(
            f"{GITHUB_API}/search/issues",
            headers,
            params={"q": query, "per_page": 100, "page": page},
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


def list_branches(headers: dict, full_name: str):
    owner, repo = full_name.split("/", 1)
    branches = github_paginate(
        f"{GITHUB_API}/repos/{owner}/{repo}/branches",
        headers,
        params={"per_page": 100},
    )
    return [b["name"] for b in branches]


def fetch_commits_on_ref(headers: dict, full_name: str, username: str, ref: str, start_utc: str, end_utc: str):
    owner, repo = full_name.split("/", 1)
    return github_paginate(
        f"{GITHUB_API}/repos/{owner}/{repo}/commits",
        headers,
        params={
            "author": username,
            "sha": ref,
            "since": start_utc,
            "until": end_utc,
            "per_page": 100,
        },
    )


def fetch_pr_commits(headers: dict, full_name: str, number: int):
    owner, repo = full_name.split("/", 1)
    return github_paginate(
        f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{number}/commits",
        headers,
        params={"per_page": 100},
    )


def commit_to_point(commit: dict, full_name: str, start_pt, end_pt):
    ts_utc = datetime.fromisoformat(
        commit["commit"]["committer"]["date"].replace("Z", "+00:00")
    )
    ts_pt = ts_utc.astimezone(PT)
    if start_pt <= ts_pt < end_pt:
        return commit["sha"], (ts_pt, full_name)
    return None, None


def fetch_commit_items(token: str, username: str, org: str, day_pt):
    """
    Collect commits authored by username in org on the PT day.

    GitHub's commit search API only indexes the default branch, so we also:
    - discover repos via user PushEvents and PR search
    - list commits on every branch of those repos in the day window
    - include commits attached to the user's PRs updated around that day
    """
    headers = github_headers(token)
    start_pt, end_pt = day_window_pt(day_pt)
    start_utc = to_utc_z(start_pt)
    end_utc = to_utc_z(end_pt)

    repos = set()
    repos |= discover_repos_from_commit_search(headers, username, org, start_utc, end_utc)
    repos |= discover_repos_from_events(headers, username, org)
    pr_repos, prs = discover_repos_and_prs(headers, username, org, day_pt)
    repos |= pr_repos

    by_sha = {}

    for full_name in sorted(repos):
        try:
            branches = list_branches(headers, full_name)
        except requests.HTTPError:
            continue
        for branch in branches:
            try:
                commits = fetch_commits_on_ref(
                    headers, full_name, username, branch, start_utc, end_utc
                )
            except requests.HTTPError:
                continue
            for commit in commits:
                sha, point = commit_to_point(commit, full_name, start_pt, end_pt)
                if sha:
                    by_sha[sha] = point

    # PR commits cover unmerged work even if a head branch was deleted.
    for full_name, number in prs:
        try:
            commits = fetch_pr_commits(headers, full_name, number)
        except requests.HTTPError:
            continue
        for commit in commits:
            # PR commit list is not author-filtered; keep only this user.
            author_login = (commit.get("author") or {}).get("login")
            if author_login != username:
                continue
            sha, point = commit_to_point(commit, full_name, start_pt, end_pt)
            if sha:
                by_sha[sha] = point

    return sorted(by_sha.values(), key=lambda x: x[0])


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

    print("Self-test passed.")


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


def print_week_summary(token, username, org, any_day):
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
        commit_points = fetch_commit_items(token, username, org, day)
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
    args = p.parse_args()

    if args.self_test:
        self_test()
        return

    if bool(args.date) == bool(args.week):
        raise SystemExit("Provide exactly one of --date or --week MM/DD/YYYY")

    token = os.getenv("GITHUB_TOKEN")
    if not token:
        raise SystemExit("Set GITHUB_TOKEN in your environment.")

    if args.week:
        print_week_summary(token, args.username, args.org, parse_mmddyyyy(args.week))
        return

    day = parse_mmddyyyy(args.date)
    commit_points = fetch_commit_items(token, args.username, args.org, day)
    result = compute_hours(day, commit_points)
    print_day_detail(day, result, args.org)


if __name__ == "__main__":
    main()
