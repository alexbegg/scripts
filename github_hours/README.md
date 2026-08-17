# GitHub Hours

Detect working hours for a given day by checking GitHub commits in an organization, including activity that falls outside standard working hours.

The script searches commits authored by a GitHub user in a given org on a specific Pacific Time (PT) day. It compares those commit times against a baseline work window and expands the day when commits happen earlier or later than usual.

## How hours are calculated

For each day of the week, a baseline window is defined in America/Los_Angeles (PT):

| Day | Baseline |
|-----|----------|
| Monday | 8:00 AM – 5:00 PM |
| Tuesday | 7:30 AM – 5:00 PM |
| Wednesday–Sunday | 8:00 AM – 5:00 PM |

- If there are **no commits**, hours equal the baseline window length.
- If there **are commits**, the effective window is from the earlier of (first commit, baseline start) to the later of (last commit, baseline end).

So commits outside standard hours extend the reported working day; commits only inside the baseline do not shrink it.

This does **not** account for time the user is out of office (PTO, sick days, meetings away from the keyboard, etc.). Hours are inferred only from the baseline window and commit timestamps—there is no calendar or OOO integration—so a day with no commits still reports the full baseline length even if the user was away.

## Setup

1. Install dependencies:

```bash
pip install -r requirements.txt
```

2. Set a GitHub personal access token with permission to search commits in the target org:

```bash
export GITHUB_TOKEN=your_token_here
```

You can also put `GITHUB_TOKEN=...` in a `.env` file and load it into your shell before running (the script reads the token from the environment).

## Usage

```bash
python github_hours.py --date MM/DD/YYYY [--username <github-username>] [--org <org>]
```

### Arguments

| Argument | Required | Default | Description |
|----------|----------|---------|-------------|
| `--date` | Yes* | — | Day to analyze, as `MM/DD/YYYY` (interpreted in PT) |
| `--username` | No | `alexbegg` | GitHub username whose commits to search |
| `--org` | No | `captiv8io` | GitHub organization to search |
| `--self-test` | No | — | Run built-in calculation checks and exit |

\* `--date` is required for a normal run (not for `--self-test`).

### Examples

```bash
# Hours for a specific day in the default org
python github_hours.py --username alexbegg --date 07/26/2026

# Different org
python github_hours.py --username alexbegg --date 07/26/2026 --org my-org

# Verify hour math
python github_hours.py --self-test
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
