"""gate-check (ADR-039, amended by ADR-040 §5): a PR passes only when passing runs of the gates
its files need cover its head.

1. Fails if the head branch starts with `gate/`.
2. Picks the gates the PR needs: dry-run on `gate/dry-run/<task>` and smoke on
   `gate/smoke/<task>` when it changes a platform file (PLATFORM below); log-batch on
   `gate/log-batch/<task>` when it changes a batch file or a fixture spec file (BATCH_DIRS
   and SPEC_FILES below). Passes if it needs none.
3. For each gate it needs, takes the latest successful run of its workflow on its branch and
   that run's commit X.
4. Passes only if, for every gate it needs, X is an ancestor of the head and every file changed
   since X is `*.md`. The gates may cover the head from different commits.
5. Prints which commits and runs it relied on; the workflow adds it to the job summary.

`<task>` is the head branch without its first segment: `claude/<task>` (ADR-038), and the
same for Kade's own branches (`kade/x` -> `x`); a branch without `/` is its own task.

Usage:
  python3 gate_check.py --gate-branch HEAD_BRANCH
      prints each gate's branch, one per line: `gate/dry-run/<task>`, `gate/smoke/<task>`,
      `gate/log-batch/<task>`.
  python3 gate_check.py HEAD_BRANCH BASE_SHA HEAD_SHA RUNS_JSON [REPO_DIR]
      RUNS_JSON is `GET /repos/{repo}/actions/runs?branch=<gate branch>` for each gate branch,
      merged (the object or its `workflow_runs` list); REPO_DIR holds the PR head with full
      history. Exits 0 on pass, 1 on fail.
"""

import json
import subprocess
import sys

DRY_RUN_WORKFLOW = ".github/workflows/dry-run.yml"
SMOKE_WORKFLOW = ".github/workflows/smoke.yml"
LOG_BATCH_WORKFLOW = ".github/workflows/log-batch.yml"
# Each gate: its name, its workflow, its branch prefix (ADR-038, ADR-040 §5).
GATES = (("dry-run", DRY_RUN_WORKFLOW, "gate/dry-run/"),
         ("smoke", SMOKE_WORKFLOW, "gate/smoke/"),
         ("log-batch", LOG_BATCH_WORKFLOW, "gate/log-batch/"))
PLATFORM_DIRS = ("charts/", "images/", "install/", "gates/")
GATE_WORKFLOWS = (DRY_RUN_WORKFLOW, SMOKE_WORKFLOW)
PLATFORM_GATES = ("dry-run", "smoke")
# ADR-040 §4 and §5: the batch files, and the fixture spec files by path (gates/fixtures.py's
# SPEC_FILES and VALUES, values-lakehouse.yaml whole). They need the log-batch gate.
BATCH_DIRS = ("images/polaris-log-batch/", "charts/polaris-log-batch/", "docs/contracts/")
SPEC_FILES = ("gates/logs_traffic.sh", ".github/workflows/create-fixtures.yml",
              "gates/fixtures.py", "charts/polaris/values-lakehouse.yaml")
LOG_BATCH_GATES = ("log-batch",)


def task_of(head_branch):
    return head_branch.split("/", 1)[1] if "/" in head_branch else head_branch


def gate_branches(head_branch):
    return [prefix + task_of(head_branch) for _, _, prefix in GATES]


def is_platform(path):
    return path.startswith(PLATFORM_DIRS) or path in GATE_WORKFLOWS


def is_log_batch(path):
    return path.startswith(BATCH_DIRS) or path in SPEC_FILES


def latest_run(runs, workflow, branch):
    passing = [r for r in runs
               if r.get("path") == workflow and r.get("head_branch") == branch
               and r.get("conclusion") == "success"]
    return max(passing, key=lambda r: r.get("created_at", ""), default=None)


def covers(name, workflow, branch, runs, git):
    """Returns (passed, lines) for one gate: its latest successful run covers the head."""
    run = latest_run(runs, workflow, branch)
    if run is None:
        return False, [f"No successful {name} on `{branch}`. Push the head there: "
                       f"`git push origin HEAD:refs/heads/{branch}`."]
    sha, url = run.get("head_sha", ""), run.get("html_url", "")
    lines = [f"Latest successful {name} on `{branch}`: commit `{sha}`, run {url}."]
    if not git.is_ancestor(sha):
        return False, lines + [f"Commit `{sha}` is not an ancestor of the head: push the new "
                               f"head to `{branch}`."]
    code = sorted(p for p in git.changed_since(sha) if not p.endswith(".md"))
    if code:
        return False, lines + [f"Changed since the {name}'s commit, not documentation: "
                               + ", ".join(f"`{p}`" for p in code)
                               + f". Push the new head to `{branch}`."]
    return True, lines + [f"The {name} passes: the head is `{sha}` or adds only "
                          "documentation to it."]


def decide(head_branch, changed, runs, git):
    """Returns (passed, lines). `git` answers is_ancestor(sha) and changed_since(sha)."""
    if head_branch.startswith("gate/"):
        return False, [f"Head branch `{head_branch}` is a gate/ branch: never opened as a PR "
                       "(ADR-038)."]
    platform = sorted(p for p in changed if is_platform(p))
    batch = sorted(p for p in changed if is_log_batch(p))
    if not platform and not batch:
        return True, ["The PR changes no platform, batch or fixture spec file; no gate run "
                      "needed."]
    lines, needed = [], []
    if platform:
        lines.append("Platform files changed: " + ", ".join(f"`{p}`" for p in platform) + ".")
        needed += PLATFORM_GATES
    if batch:
        lines.append("Batch or fixture spec files changed: "
                     + ", ".join(f"`{p}`" for p in batch) + ".")
        needed += LOG_BATCH_GATES
    passed = True
    for (name, workflow, _), branch in zip(GATES, gate_branches(head_branch)):
        if name in needed:
            ok, gate_lines = covers(name, workflow, branch, runs, git)
            passed, lines = passed and ok, lines + gate_lines
    verdict = ("Passed: every gate the PR needs covers the head." if passed
               else "Failed: not every gate the PR needs covers the head.")
    return passed, lines + [verdict]


def _git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True,
                          check=False)


def _names(result):
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())
    return [line for line in result.stdout.splitlines() if line]


def changed_files(repo, base, head):
    """Files the PR changes: since the merge base, renames listed under both names."""
    return _names(_git(repo, "diff", "--no-renames", "--name-only", f"{base}...{head}"))


class Git:
    def __init__(self, repo, head):
        self.repo, self.head = repo, head

    def is_ancestor(self, sha):
        return _git(self.repo, "merge-base", "--is-ancestor", sha, self.head).returncode == 0

    def changed_since(self, sha):
        return _names(_git(self.repo, "diff", "--no-renames", "--name-only", sha, self.head))


def load_runs(text):
    data = json.loads(text)
    return data.get("workflow_runs", []) if isinstance(data, dict) else data


def main(argv):
    if len(argv) == 3 and argv[1] == "--gate-branch":
        print("\n".join(gate_branches(argv[2])))
        return 0
    if len(argv) not in (5, 6):
        print(__doc__.strip().splitlines()[0], file=sys.stderr)
        return 2
    head_branch, base, head, runs_path = argv[1:5]
    repo = argv[5] if len(argv) == 6 else "."
    with open(runs_path, encoding="utf-8") as f:
        runs = load_runs(f.read())
    passed, lines = decide(head_branch, changed_files(repo, base, head), runs, Git(repo, head))
    print("\n\n".join(lines))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
