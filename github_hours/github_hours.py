#!/usr/bin/env python3
import argparse
import os
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
import requests

GITHUB_API = "https://api.github.com"
PT = ZoneInfo("America/Los_Angeles")

# Monday=0 ... Sunday=6
WORKING_HOURS = {
    0: {"start": time(8, 0),  "end": time(17, 0)},  # Mon
    1: {"start": time(7, 30), "end": time(17, 0)},  # Tue
    2: {"start": time(8, 0),  "end": time(17, 0)},  # Wed
    3: {"start": time(8, 0),  "end": time(17, 0)},  # Thu
    4: {"start": time(8, 0),  "end": time(17, 0)},  # Fri
    5: {"start": time(8, 0),  "end": time(17, 0)},  # Sat
    6: {"start": time(8, 0),  "end": time(17, 0)},  # Sun
}


def fmt_datetime_pt(dt):
    if dt is None:
        return "N/A"
    date_part = dt.strftime("%m/%d/%Y")
    time_part = dt.strftime("%I:%M %p").lstrip("0")  # 08:00 AM -> 8:00 AM
    return f"{date_part} {time_part} PT"


def parse_mmddyyyy(value: str):
    return datetime.strptime(value, "%m/%d/%Y").date()


def to_utc_z(dt):
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def fetch_commit_items(token: str, username: str, org: str, day_pt):
    start_pt = datetime.combine(day_pt, time(0, 0), tzinfo=PT)
    end_pt = start_pt + timedelta(days=1)

    start_utc = to_utc_z(start_pt)
    end_utc = to_utc_z(end_pt)

    # Search commits authored by username, in org, within UTC window for PT day
    query = f"author:{username} org:{org} committer-date:{start_utc}..{end_utc}"

    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    items = []
    page = 1
    while True:
        r = requests.get(
            f"{GITHUB_API}/search/commits",
            headers=headers,
            params={"q": query, "per_page": 100, "page": page},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        batch = data.get("items", [])
        items.extend(batch)
        if len(batch) < 100:
            break
        page += 1

    # Filter again in PT window for safety
    filtered = []
    for it in items:
        ts_utc = datetime.fromisoformat(
            it["commit"]["committer"]["date"].replace("Z", "+00:00")
        )
        ts_pt = ts_utc.astimezone(PT)
        if start_pt <= ts_pt < end_pt:
            filtered.append((ts_pt, it["repository"]["full_name"]))
    return filtered


def get_baseline_window(day_pt):
    w = WORKING_HOURS[day_pt.weekday()]
    start_baseline = datetime.combine(day_pt, w["start"], tzinfo=PT)
    end_baseline = datetime.combine(day_pt, w["end"], tzinfo=PT)
    return start_baseline, end_baseline


def compute_hours(day_pt, commit_points):
    start_baseline, end_baseline = get_baseline_window(day_pt)

    if not commit_points:
        raw_hours = (end_baseline - start_baseline).total_seconds() / 3600.0
        return {
            "commit_count": 0,
            "first_commit_pt": None,
            "last_commit_pt": None,
            "effective_start_pt": start_baseline,
            "effective_end_pt": end_baseline,
            "hours": raw_hours,
            "repos": [],
            "effective_window_pt": {
                "start": start_baseline,
                "end": end_baseline,
            },
        }

    times = [t for t, _ in commit_points]
    repos = sorted({r for _, r in commit_points})

    first_commit = min(times)
    last_commit = max(times)

    effective_start = min(first_commit, start_baseline)
    effective_end = max(last_commit, end_baseline)

    raw_hours = (effective_end - effective_start).total_seconds() / 3600.0

    return {
        "commit_count": len(times),
        "first_commit_pt": first_commit,
        "last_commit_pt": last_commit,
        "effective_start_pt": effective_start,
        "effective_end_pt": effective_end,
        "hours": raw_hours,
        "repos": repos,
        "effective_window_pt": {
            "start": effective_start,
            "end": effective_end,
        },
    }


def self_test():
    d = datetime(2026, 7, 26).date()
    # commits 09:00 and 16:00 => should still be 9.0h (08:00 to 17:00)
    sample1 = [
        (datetime(2026, 7, 26, 9, 0, tzinfo=PT), "captiv8io/a"),
        (datetime(2026, 7, 26, 16, 0, tzinfo=PT), "captiv8io/b"),
    ]
    r1 = compute_hours(d, sample1)
    assert r1["hours"] == 9.0, f"Expected 9.0, got {r1['hours']}"

    # commits 07:30 and 19:15 => 11.75h
    sample2 = [
        (datetime(2026, 7, 26, 7, 30, tzinfo=PT), "captiv8io/a"),
        (datetime(2026, 7, 26, 19, 15, tzinfo=PT), "captiv8io/b"),
    ]
    r2 = compute_hours(d, sample2)
    assert r2["hours"] == 11.75, f"Expected 11.75, got {r2['hours']}"

    print("Self-test passed.")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--username", default="alexbegg", help="GitHub username")
    p.add_argument("--date", help="MM/DD/YYYY, e.g. 07/26/2026")
    p.add_argument("--org", default="captiv8io", help="GitHub organization")
    p.add_argument("--self-test", action="store_true")
    args = p.parse_args()

    if args.self_test:
        self_test()
        return

    if not args.username or not args.date:
        raise SystemExit("Please provide --username and --date MM/DD/YYYY")

    token = os.getenv("GITHUB_TOKEN")
    if not token:
        raise SystemExit("Set GITHUB_TOKEN in your environment.")

    day = parse_mmddyyyy(args.date)
    commit_points = fetch_commit_items(token, args.username, args.org, day)
    result = compute_hours(day, commit_points)

    print(f"Date (PT): {day.strftime('%m/%d/%Y')}")
    print(f"Org: {args.org}")
    print(f"Commits found: {result['commit_count']}")
    print(f"\nHours: {result['hours']:.2f}\n")
    print(f"First commit PT: {fmt_datetime_pt(result['first_commit_pt'])}")
    print(f"Last commit PT: {fmt_datetime_pt(result['last_commit_pt'])}")
    print(f"Effective start PT: {fmt_datetime_pt(result['effective_start_pt'])}")
    print(f"Effective end PT: {fmt_datetime_pt(result['effective_end_pt'])}")
    print(f"Repos touched: {', '.join(result['repos']) if result['repos'] else '(none)'}")


if __name__ == "__main__":
    main()
