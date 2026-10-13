"""Tests for branch_cleanup.py. Run: python3 -m unittest discover -s scripts"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import branch_cleanup as cleanup  # noqa: E402

REPO = "o/r"


def branch(name, sha="a1"):
    return {"name": name, "sha": sha}


def pr(ref, sha="a1", state="closed", merged=True, repo=REPO):
    return {"ref": ref, "sha": sha, "repo": repo, "state": state, "merged": merged}


def delete(branches, prs):
    return cleanup.to_delete(branches, prs, REPO)


class ClaudeBranches(unittest.TestCase):
    def test_a_merged_branch_still_at_the_pr_head_is_deleted(self):
        self.assertEqual(delete([branch("claude/x")], [pr("claude/x")]), ["claude/x"])

    def test_a_branch_with_commits_after_the_merge_is_kept(self):
        self.assertEqual(delete([branch("claude/x", sha="b2")], [pr("claude/x", sha="a1")]), [])

    def test_a_pr_closed_without_merging_keeps_its_branch(self):
        self.assertEqual(delete([branch("claude/x")], [pr("claude/x", merged=False)]), [])

    def test_a_branch_without_a_pr_is_kept(self):
        self.assertEqual(delete([branch("claude/x")], []), [])

    def test_an_open_pr_from_the_branch_keeps_it(self):
        prs = [pr("claude/x"), pr("claude/x", state="open", merged=False)]
        self.assertEqual(delete([branch("claude/x")], prs), [])

    def test_a_merged_pr_from_a_fork_with_the_same_name_does_not_count(self):
        self.assertEqual(delete([branch("claude/x")], [pr("claude/x", repo="fork/r")]), [])

    def test_kades_and_other_branches_are_never_deleted(self):
        for name in ("main", "rc", "kade/x", "x"):
            with self.subTest(name=name):
                self.assertEqual(delete([branch(name)], [pr(name)]), [])

    def test_an_older_merged_pr_at_another_commit_does_not_hide_the_latest(self):
        prs = [pr("claude/x", sha="a1"), pr("claude/x", sha="b2")]
        self.assertEqual(delete([branch("claude/x", sha="b2")], prs), ["claude/x"])


class GateBranches(unittest.TestCase):
    def test_gate_branches_of_a_task_without_a_branch_are_deleted(self):
        branches = [branch("gate/dry-run/x"), branch("gate/smoke/x")]
        self.assertEqual(delete(branches, []), ["gate/dry-run/x", "gate/smoke/x"])

    def test_gate_branches_go_with_the_claude_branch_deleted_in_the_same_sweep(self):
        branches = [branch("claude/x"), branch("gate/dry-run/x"), branch("gate/smoke/x")]
        self.assertEqual(delete(branches, [pr("claude/x")]),
                         ["claude/x", "gate/dry-run/x", "gate/smoke/x"])

    def test_gate_branches_stay_while_the_claude_branch_exists(self):
        branches = [branch("claude/x"), branch("gate/dry-run/x"), branch("gate/smoke/x")]
        self.assertEqual(delete(branches, []), [])

    def test_gate_branches_stay_while_kades_branch_for_the_task_exists(self):
        self.assertEqual(delete([branch("kade/x"), branch("gate/dry-run/x")], []), [])

    def test_gate_branches_stay_while_an_open_pr_has_the_task(self):
        prs = [pr("claude/x", state="open", merged=False, repo="fork/r")]
        self.assertEqual(delete([branch("gate/dry-run/x")], prs), [])

    def test_only_the_task_of_the_deleted_branch_goes(self):
        branches = [branch("claude/x"), branch("claude/y"),
                    branch("gate/dry-run/x"), branch("gate/dry-run/y")]
        self.assertEqual(delete(branches, [pr("claude/x")]), ["claude/x", "gate/dry-run/x"])

    def test_a_task_with_slashes_is_matched_whole(self):
        branches = [branch("claude/a/b"), branch("gate/dry-run/a/b"), branch("gate/dry-run/a")]
        self.assertEqual(delete(branches, []), ["gate/dry-run/a"])

    def test_log_batch_gate_branches_go_with_the_others(self):
        branches = [branch("claude/x"), branch("gate/dry-run/x"), branch("gate/smoke/x"),
                    branch("gate/log-batch/x"), branch("gate/log-batch/y"),
                    branch("claude/y", sha="b2")]
        self.assertEqual(delete(branches, [pr("claude/x"), pr("claude/y")]),
                         ["claude/x", "gate/dry-run/x", "gate/log-batch/x", "gate/smoke/x"])

    def test_failover_measurement_branches_go_with_the_others(self):
        branches = [branch("claude/x"), branch("gate/failover/x"), branch("gate/failover/y"),
                    branch("claude/y", sha="b2")]
        self.assertEqual(delete(branches, [pr("claude/x"), pr("claude/y")]),
                         ["claude/x", "gate/failover/x"])

    def test_other_gate_names_are_kept(self):
        for name in ("gate/other/x", "gate/dry-run", "gate/dry-run/", "gate/log-batch/",
                     "gate/failover/", "gate"):
            with self.subTest(name=name):
                self.assertEqual(delete([branch(name)], []), [])


class Main(unittest.TestCase):
    def write(self, folder, name, data):
        path = os.path.join(folder, name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        return path

    def test_prints_one_branch_per_line(self):
        with tempfile.TemporaryDirectory() as d:
            branches = self.write(d, "b.json", [branch("claude/x"), branch("gate/smoke/x")])
            prs = self.write(d, "p.json", [pr("claude/x")])
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cleanup.main(["branch_cleanup.py", branches, prs, REPO])
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue(), "claude/x\ngate/smoke/x\n")

    def test_nothing_to_delete_prints_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            branches = self.write(d, "b.json", [branch("main")])
            prs = self.write(d, "p.json", [])
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cleanup.main(["branch_cleanup.py", branches, prs, REPO])
        self.assertEqual((code, out.getvalue()), (0, ""))

    def test_wrong_arguments_exit_2(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cleanup.main(["branch_cleanup.py"]), 2)


if __name__ == "__main__":
    unittest.main()
