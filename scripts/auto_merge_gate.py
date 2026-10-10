"""Decides whether a pull request's head commit is green enough to auto-merge.

Reads the commit's check runs and its combined commit status (as the GitHub REST API returns
them) and prints one word:

- `merge`: every check run has finished green (success, neutral or skipped), at least one
  succeeded, every required check succeeded, and no commit status is pending or failed;
- `wait`: something is still running, nothing has reported yet, or a required check has not
  succeeded (not started yet, or skipped);
- `stop`: something failed; a person looks at it.

Usage: python3 auto_merge_gate.py [--require NAME]... CHECK_RUNS_JSON COMBINED_STATUS_JSON
CHECK_RUNS_JSON is a JSON array of check runs; COMBINED_STATUS_JSON is the object from
`GET /repos/{repo}/commits/{sha}/status`. `--require NAME` names a check run that must have
succeeded, so a check that has not started yet cannot be skipped (ADR-022: gate-check).
Prints names and conclusions only, never titles.

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


def decide(check_runs, combined_status, required=()):
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
    succeeded = {c.get("name") for c in check_runs if c.get("conclusion") == "success"}
    if waiting or not succeeded or not set(required) <= succeeded:
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
    required = []
    args = argv[1:]
    while len(args) >= 2 and args[0] == "--require":
        required.append(args[1])
        args = args[2:]
    if len(args) != 2:
        print(__doc__.strip().splitlines()[0], file=sys.stderr)
        return 2
    with open(args[0], encoding="utf-8") as f:
        check_runs = json.load(f)
    with open(args[1], encoding="utf-8") as f:
        combined_status = json.load(f)
    for check in check_runs:
        print(f"check: {check.get('name')}: {check.get('status')} {check.get('conclusion')}",
              file=sys.stderr)
    for name in required:
        print(f"required: {name}", file=sys.stderr)
    print(decide(check_runs, combined_status, required))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
