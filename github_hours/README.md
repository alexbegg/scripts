# GitHub Hours

Detect working hours for a given day by checking GitHub commits in an organization, including activity that falls outside standard working hours.

The script finds commits authored by a GitHub user in a given org on a specific Pacific Time (PT) day, including commits on non-default branches and in open (unmerged) PRs. GitHub's commit search API only indexes the default branch, so the script also discovers candidate repos from your recent push events and PRs, then lists your commits across every branch in those repos. Branch and PR commit requests run concurrently. It compares those commit times against a baseline work window and expands the day when commits happen earlier or later than usual.

## How hours are calculated

For each day of the week, a baseline window is defined in America/Los_Angeles (PT):

| Day | Baseline |
|-----|----------|
| Monday | 8:00 AM – 5:00 PM |
| Tuesday | 7:30 AM – 5:00 PM |
| Wednesday–Friday | 8:00 AM – 5:00 PM |
| Saturday–Sunday | none (0 hours) |

- **Weekdays, no commits:** hours equal the baseline window length.
- **Weekdays, with commits:** hours = baseline + overtime. Overtime is the span from first→last commit **before** the baseline start, plus the span from first→last commit **after** the baseline end. Gaps between the baseline and outside commits are not counted. A single outside commit on either side counts as **0.5 hours** minimum.
- **Weekends:** baseline is 0. No commits → 0 hours. With commits → hours are first commit through last commit only.

This does **not** account for time the user is out of office (PTO, sick days, meetings away from the keyboard, etc.). On weekdays, a day with no commits still reports the full baseline length even if the user was away.

## Setup

1. Install dependencies:

```bash
pip install -r requirements.txt
```

2. Set a GitHub personal access token with permission to search commits in the target org:

```bash
export GITHUB_TOKEN=your_token_here
```

You can also put `GITHUB_TOKEN=...` in a `.env` file and load it into your shell by running `export $(xargs < .env)` before running the script (the script reads the token from the environment).

## Usage

```bash
python github_hours.py --date MM/DD/YYYY [--username <github-username>] [--org <org>]
python github_hours.py --week MM/DD/YYYY [--username <github-username>] [--org <org>]
```

### Arguments

| Argument | Required | Default | Description |
|----------|----------|---------|-------------|
| `--date` | Yes* | — | Single day to analyze, as `MM/DD/YYYY` (interpreted in PT) |
| `--week` | Yes* | — | Date in the week to summarize; prints Sunday–Saturday hours for that week |
| `--username` | No | `alexbegg` | GitHub username whose commits to search |
| `--org` | No | `captiv8io` | GitHub organization to search |
| `--self-test` | No | — | Run built-in calculation checks and exit |
| `--debug-timing` | No | — | Print GitHub call counts, phase timings, and the slowest endpoints |
| `--refresh` | No | — | Ignore cached days and replace them |
| `--no-cache` | No | — | Do not read or write the day cache |
| `--commits` | No | — | List each commit counted for the day (time, repo, short SHA, subject) |

\* Provide exactly one of `--date` or `--week` for a normal run (not for `--self-test`).

### Examples

```bash
# Hours for a specific day in the default org
python github_hours.py --username alexbegg --date 07/26/2026

# Sunday–Saturday summary for the week containing this date (prints each day as it
# finishes; stops after today in PT so future days are skipped)
python github_hours.py --week 10/20/2026

# Different org
python github_hours.py --username alexbegg --date 07/26/2026 --org my-org

# Verify hour math
python github_hours.py --self-test

# Show which GitHub calls dominated the scan
python github_hours.py --date 10/19/2026 --debug-timing

# Replace a cached day
python github_hours.py --date 10/19/2026 --refresh

# List the commits counted for that day
python github_hours.py --date 10/19/2026 --commits
```

### Sample output

```
Date (PT): 07/26/2026
Org: captiv8io
Commits found: 4

Hours: 11.75

First commit PT: 07/26/2026 7:30 AM PT
Last commit PT: 07/26/2026 7:15 PM PT
Effective start PT: 07/26/2026 7:30 AM PT
Effective end PT: 07/26/2026 7:15 PM PT
Repos touched: org/repo-a, org/repo-b
```

## Performance

Scanning every branch is what makes a day slow. A serial scan of 10/19/2026 took about 59 seconds and 200 GitHub calls; 171 of those calls (50 seconds) were commit queries for `captiv8-data`. Those branch and PR queries now run eight at a time.

Finished days are stored as JSON for 24 hours in the OS cache directory (`~/Library/Caches/github_hours` on macOS, `%LOCALAPPDATA%\github_hours` on Windows, and `$XDG_CACHE_HOME/github_hours` or `~/.cache/github_hours` on other systems). A repeat scan of a past day returns that file and does not call GitHub. The current PT day is always fetched again, because it can still gain commits. `--week` follows the same rule for each day. Use `--refresh` when a past day may have changed, or `--no-cache` for a fully live scan. `--debug-timing` prints the phase breakdown after the result.
