"""gate-check (ADR-019): a platform PR passes only when a passing dry-run covers its head.

1. Fails if the head branch starts with `gate/`.
2. Passes if the PR changes no platform file (PLATFORM below).
3. Otherwise takes the latest successful dry-run on `gate/dry-run/<task>` and its commit X.
4. Passes only if X is an ancestor of the head and every file changed since X is `*.md`.
5. Prints which commit and run it relied on; the workflow adds it to the job summary.

`<task>` is the head branch without its first segment: `claude/<task>` (ADR-018), and the
same for Kade's own branches (`kade/x` -> `x`); a branch without `/` is its own task.

Usage:
  python3 gate_check.py --gate-branch HEAD_BRANCH
      prints `gate/dry-run/<task>`.
  python3 gate_check.py HEAD_BRANCH BASE_SHA HEAD_SHA RUNS_JSON [REPO_DIR]
      RUNS_JSON is `GET /repos/{repo}/actions/runs?branch=gate/dry-run/<task>` (the object
      or its `workflow_runs` list); REPO_DIR holds the PR head with full history. Exits 0 on
      pass, 1 on fail.
"""

import json
import subprocess
import sys

DRY_RUN_WORKFLOW = ".github/workflows/dry-run.yml"
PLATFORM_DIRS = ("charts/", "images/", "install/", "gates/")
GATE_WORKFLOWS = (DRY_RUN_WORKFLOW, ".github/workflows/smoke.yml")


def gate_branch(head_branch):
    task = head_branch.split("/", 1)[1] if "/" in head_branch else head_branch
    return f"gate/dry-run/{task}"


def is_platform(path):
    return path.startswith(PLATFORM_DIRS) or path in GATE_WORKFLOWS


def latest_dry_run(runs, branch):
    passing = [r for r in runs
               if r.get("path") == DRY_RUN_WORKFLOW and r.get("head_branch") == branch
               and r.get("conclusion") == "success"]
    return max(passing, key=lambda r: r.get("created_at", ""), default=None)


def decide(head_branch, changed, runs, git):
    """Returns (passed, lines). `git` answers is_ancestor(sha) and changed_since(sha)."""
    if head_branch.startswith("gate/"):
        return False, [f"Head branch `{head_branch}` is a gate/ branch: never opened as a PR "
                       "(ADR-018)."]
    platform = sorted(p for p in changed if is_platform(p))
    if not platform:
        return True, ["The PR changes no platform file; no dry-run needed."]
    lines = ["Platform files changed: " + ", ".join(f"`{p}`" for p in platform) + "."]
    branch = gate_branch(head_branch)
    run = latest_dry_run(runs, branch)
    if run is None:
        return False, lines + [f"No successful dry-run on `{branch}`. Push the head there: "
                               f"`git push origin HEAD:refs/heads/{branch}`."]
    sha, url = run.get("head_sha", ""), run.get("html_url", "")
    lines.append(f"Latest successful dry-run on `{branch}`: commit `{sha}`, run {url}.")
    if not git.is_ancestor(sha):
        return False, lines + [f"Commit `{sha}` is not an ancestor of the head: gate the new "
                               "head."]
    code = sorted(p for p in git.changed_since(sha) if not p.endswith(".md"))
    if code:
        return False, lines + ["Changed since the gated commit, not documentation: "
                               + ", ".join(f"`{p}`" for p in code) + ". Gate the new head."]
    return True, lines + [f"Passed: the head is `{sha}` or adds only documentation to it."]


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
        print(gate_branch(argv[2]))
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
