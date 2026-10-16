"""Decides whether a pull request's head commit is green enough to auto-merge.

Reads the commit's check runs and its combined commit status (as the GitHub REST API returns
them) and prints one word:

- `merge`: every check run has finished green (success, neutral or skipped), at least one
  succeeded, every required check succeeded, and no commit status is pending or failed;
- `wait`: something is still running, nothing has reported yet, or a required check has not
  succeeded (not started yet, or skipped);
- `stop`: something failed, or the PR is left to Kade (ADR-030, ADR-059): it is a draft, it
  changes a protected path (PROTECTED below), or its file list was not read whole.

Usage: python3 auto_merge_gate.py [--require NAME]... [--pr PR_JSON [--files FILES_JSON]]
           CHECK_RUNS_JSON COMBINED_STATUS_JSON
CHECK_RUNS_JSON is a JSON array of check runs; COMBINED_STATUS_JSON is the object from
`GET /repos/{repo}/commits/{sha}/status`. `--require NAME` names a check run that must have
succeeded, so a check that has not started yet cannot be skipped (ADR-022: gate-check).
PR_JSON holds `draft` and `changed_files` from `GET /repos/{repo}/pulls/{n}`; FILES_JSON is
the array of `filename` and `previous_filename` from its files, all pages (ADR-030). With
--pr, a missing --files stops. Prints names and conclusions only, never titles.

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

# A PR that changes these is left to Kade: the merge rules and the checks (ADR-030), the
# decisions and the agent rules (ADR-059).
PROTECTED_DIRS = (".github/workflows/", "docs/adr/", ".claude/rules/")
PROTECTED_FILES = ("scripts/auto_merge_gate.py", "scripts/gate_check.py", "CLAUDE.md")


def protected_paths(files):
    """Protected paths a PR's files touch, a rename under both names; sorted, unique."""
    names = {n for f in files for n in (f.get("filename"), f.get("previous_filename")) if n}
    return sorted(n for n in names if n.startswith(PROTECTED_DIRS) or n in PROTECTED_FILES)


def left_to_kade(pr, files):
    """Why ADR-030 leaves this PR to Kade, or None."""
    if pr.get("draft"):
        return "draft"
    if files is None or len(files) != pr.get("changed_files"):
        return "file list not read whole"
    touched = protected_paths(files)
    if touched:
        return "protected paths: " + ", ".join(touched)
    return None


def decide(check_runs, combined_status, required=(), pr=None, files=None):
    if pr is not None and left_to_kade(pr, files):
        return "stop"
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
    required, options, args = [], {}, argv[1:]
    while len(args) >= 2 and args[0] in ("--require", "--pr", "--files"):
        if args[0] == "--require":
            required.append(args[1])
        else:
            options[args[0]] = args[1]
        args = args[2:]
    if len(args) != 2 or ("--files" in options and "--pr" not in options):
        print(__doc__.strip().splitlines()[0], file=sys.stderr)
        return 2
    pr = files = None
    if "--pr" in options:
        with open(options["--pr"], encoding="utf-8") as f:
            pr = json.load(f)
    if "--files" in options:
        with open(options["--files"], encoding="utf-8") as f:
            files = json.load(f)
    with open(args[0], encoding="utf-8") as f:
        check_runs = json.load(f)
    with open(args[1], encoding="utf-8") as f:
        combined_status = json.load(f)
    for check in check_runs:
        print(f"check: {check.get('name')}: {check.get('status')} {check.get('conclusion')}",
              file=sys.stderr)
    for name in required:
        print(f"required: {name}", file=sys.stderr)
    if pr is not None:
        print(f"left to Kade: {left_to_kade(pr, files) or 'no'}", file=sys.stderr)
    print(decide(check_runs, combined_status, required, pr, files))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
