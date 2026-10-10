"""Branch cleanup (ADR-032): which branches have outlived their work.

1. `claude/<task>` goes when a merged PR of this repository has it as its head and the branch
   still points at that PR's head commit, and no open PR has it as its head.
2. `gate/dry-run/<task>` and `gate/smoke/<task>` go when no other branch has the same task,
   counting the branches step 1 deletes as gone, and no open PR has that task.
3. Everything else stays: `main`, `rc`, Kade's branches, other `gate/` names.

`<task>` is the branch without its first segment, as in gate-check (ADR-019).

Usage:
  python3 branch_cleanup.py BRANCHES_JSON PRS_JSON OWNER/REPO
      BRANCHES_JSON is a list of {name, sha}; PRS_JSON a list of {ref, sha, repo, state,
      merged} for every PR, open and closed. Prints the branches to delete, one per line.
"""

import json
import sys

GATE_KINDS = ("dry-run", "smoke")


def task(name):
    return name.split("/", 1)[1] if "/" in name else name


def gate_task(name):
    parts = name.split("/", 2)
    if len(parts) == 3 and parts[0] == "gate" and parts[1] in GATE_KINDS and parts[2]:
        return parts[2]
    return None


def to_delete(branches, prs, repo):
    open_prs = [p for p in prs if p["state"] == "open"]
    open_refs = {p["ref"] for p in open_prs}
    open_tasks = {task(p["ref"]) for p in open_prs}
    merged = {(p["ref"], p["sha"]) for p in prs if p.get("merged") and p.get("repo") == repo}

    delete = [b["name"] for b in branches
              if b["name"].startswith("claude/")
              and (b["name"], b["sha"]) in merged
              and b["name"] not in open_refs]
    tasks_left = {task(b["name"]) for b in branches
                  if not b["name"].startswith("gate/") and b["name"] not in delete}
    for b in branches:
        t = gate_task(b["name"])
        if t is not None and t not in tasks_left and t not in open_tasks:
            delete.append(b["name"])
    return sorted(delete)


def main(argv):
    if len(argv) != 4:
        print(__doc__.strip().splitlines()[0], file=sys.stderr)
        return 2
    branches_path, prs_path, repo = argv[1:4]
    with open(branches_path, encoding="utf-8") as f:
        branches = json.load(f)
    with open(prs_path, encoding="utf-8") as f:
        prs = json.load(f)
    for name in to_delete(branches, prs, repo):
        print(name)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
