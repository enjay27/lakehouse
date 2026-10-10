"""Decides whether a pull request's head commit is green enough to auto-merge.

Reads the commit's check runs and its combined commit status (as the GitHub REST API returns
them) and prints one word:

- `merge`: every check run has finished green (success, neutral or skipped), at least one
  succeeded, and no commit status is pending or failed;
- `wait`: something is still running, or nothing has reported yet;
- `stop`: something failed; a person looks at it.

Usage: python3 auto_merge_gate.py CHECK_RUNS_JSON COMBINED_STATUS_JSON
CHECK_RUNS_JSON is a JSON array of check runs; COMBINED_STATUS_JSON is the object from
`GET /repos/{repo}/commits/{sha}/status`. Prints names and conclusions only, never titles.

After a merge: python3 auto_merge_gate.py --issues-to-close REFS_JSON OWNER/REPO
REFS_JSON is the PR's `closingIssuesReferences` (from `gh pr view --json`); prints the
issue numbers in OWNER/REPO to close, one per line. A merge made with GITHUB_TOKEN does not
apply closing keywords, so the workflow closes them itself.

Ported from stella-rain/.github scripts/auto_merge_gate.py at 144d317
(MIT License, Copyright (c) 2026 Kade).
"""

import json
import sys

GREEN = {"success", "neutral", "skipped"}


def decide(check_runs, combined_status):
    waiting = False
    for check in check_runs:
        if check.get("status") != "completed":
            waiting = True
        elif check.get("conclusion") not in GREEN:
            return "stop"
    if combined_status.get("total_count", 0) > 0:
        state = combined_status.get("state")
        if state in ("failure", "error"):
            return "stop"
        if state != "success":
            waiting = True
    if waiting or not any(c.get("conclusion") == "success" for c in check_runs):
        return "wait"
    return "merge"


def issues_to_close(refs, repo):
    """Numbers of the referenced issues that live in `repo` (owner/name), sorted, unique."""
    wanted = repo.lower()
    numbers = set()
    for r in refs:
        repository = r.get("repository") or {}
        full = f"{(repository.get('owner') or {}).get('login', '')}/{repository.get('name', '')}"
        if full.lower() == wanted:
            numbers.add(r["number"])
    return sorted(numbers)


def main(argv):
    if len(argv) == 4 and argv[1] == "--issues-to-close":
        with open(argv[2], encoding="utf-8") as f:
            refs = json.load(f)
        for number in issues_to_close(refs, argv[3]):
            print(number)
        return 0
    if len(argv) != 3:
        print(__doc__.strip().splitlines()[0], file=sys.stderr)
        return 2
    with open(argv[1], encoding="utf-8") as f:
        check_runs = json.load(f)
    with open(argv[2], encoding="utf-8") as f:
        combined_status = json.load(f)
    for check in check_runs:
        print(f"check: {check.get('name')}: {check.get('status')} {check.get('conclusion')}",
              file=sys.stderr)
    print(decide(check_runs, combined_status))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
