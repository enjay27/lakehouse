"""Tests for auto_merge_gate.py. Run: python3 -m unittest discover -s scripts

Ported from stella-rain/.github scripts/test_auto_merge_gate.py at 144d317
(MIT License, Copyright (c) 2026 Kade).
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import auto_merge_gate as gate  # noqa: E402

NO_STATUSES = {"state": "pending", "total_count": 0, "statuses": []}


def run(name, status="completed", conclusion="success"):
    return {"name": name, "status": status, "conclusion": conclusion}


class CheckRuns(unittest.TestCase):
    def decide(self, runs, combined=NO_STATUSES):
        return gate.decide(runs, combined)

    def test_all_green_merges(self):
        self.assertEqual(self.decide([run("eol"), run("check")]), "merge")

    def test_skipped_and_neutral_count_as_green(self):
        runs = [run("eol"), run("a", conclusion="skipped"), run("b", conclusion="neutral")]
        self.assertEqual(self.decide(runs), "merge")

    def test_a_check_still_running_waits(self):
        runs = [run("eol"), run("check", status="in_progress", conclusion=None)]
        self.assertEqual(self.decide(runs), "wait")

    def test_a_queued_check_waits(self):
        runs = [run("eol"), run("check", status="queued", conclusion=None)]
        self.assertEqual(self.decide(runs), "wait")

    def test_a_failed_check_stops(self):
        runs = [run("eol"), run("check", conclusion="failure")]
        self.assertEqual(self.decide(runs), "stop")

    def test_failure_wins_over_a_running_check(self):
        runs = [run("a", conclusion="failure"), run("b", status="in_progress", conclusion=None)]
        self.assertEqual(self.decide(runs), "stop")

    def test_cancelled_and_timed_out_stop(self):
        for conclusion in ("cancelled", "timed_out", "action_required", "stale"):
            with self.subTest(conclusion=conclusion):
                self.assertEqual(self.decide([run("a", conclusion=conclusion)]), "stop")

    def test_no_checks_at_all_waits(self):
        self.assertEqual(self.decide([]), "wait")

    def test_only_skipped_checks_wait(self):
        self.assertEqual(self.decide([run("a", conclusion="skipped")]), "wait")


class CommitStatuses(unittest.TestCase):
    def combined(self, state):
        return {"state": state, "total_count": 1, "statuses": [{"state": state}]}

    def test_a_pending_status_waits(self):
        self.assertEqual(gate.decide([run("eol")], self.combined("pending")), "wait")

    def test_a_failed_status_stops(self):
        for state in ("failure", "error"):
            with self.subTest(state=state):
                self.assertEqual(gate.decide([run("eol")], self.combined(state)), "stop")

    def test_a_green_status_merges(self):
        self.assertEqual(gate.decide([run("eol")], self.combined("success")), "merge")

    def test_no_statuses_ignores_the_pending_default(self):
        # GitHub reports "pending" when a commit has no statuses at all.
        self.assertEqual(gate.decide([run("eol")], NO_STATUSES), "merge")


class RequiredChecks(unittest.TestCase):
    """ADR-022: once gate-check exists, the merge requires it by name, so a check that has
    not started yet cannot be skipped."""

    def test_a_required_check_not_reported_yet_waits(self):
        runs = [run("static")]
        self.assertEqual(gate.decide(runs, NO_STATUSES, required=["gate-check"]), "wait")

    def test_all_required_checks_green_merges(self):
        runs = [run("static"), run("gate-check")]
        self.assertEqual(gate.decide(runs, NO_STATUSES, required=["gate-check"]), "merge")

    def test_a_failure_still_stops_while_a_required_check_is_missing(self):
        runs = [run("static", conclusion="failure")]
        self.assertEqual(gate.decide(runs, NO_STATUSES, required=["gate-check"]), "stop")

    def test_a_skipped_required_check_does_not_count(self):
        runs = [run("static"), run("gate-check", conclusion="skipped")]
        self.assertEqual(gate.decide(runs, NO_STATUSES, required=["gate-check"]), "wait")


def pr(draft=False, changed_files=1):
    return {"draft": draft, "changed_files": changed_files}


def file(name, previous=None):
    return {"filename": name, "previous_filename": previous}


GREEN = [run("static"), run("gate-check")]


class DraftsAndProtectedPaths(unittest.TestCase):
    """ADR-030: drafts and PRs that change the merge rules or the checks are left to Kade."""

    def decide(self, pull, files, runs=GREEN):
        return gate.decide(runs, NO_STATUSES, ["static", "gate-check"], pull, files)

    def test_an_ordinary_pr_merges(self):
        self.assertEqual(self.decide(pr(), [file("docs/adr/README.md")]), "merge")

    def test_a_draft_stops(self):
        self.assertEqual(self.decide(pr(draft=True), [file("README.md")]), "stop")

    def test_a_draft_stops_even_while_checks_run(self):
        runs = [run("static", status="in_progress", conclusion=None)]
        self.assertEqual(self.decide(pr(draft=True), [file("README.md")], runs), "stop")

    def test_protected_paths_stop(self):
        for path in (".github/workflows/auto-merge.yml", ".github/workflows/new.yml",
                     "scripts/auto_merge_gate.py", "scripts/gate_check.py"):
            with self.subTest(path=path):
                self.assertEqual(self.decide(pr(), [file(path)]), "stop")

    def test_other_paths_are_not_protected(self):
        for path in (".github/labels.json", "scripts/test_gate_check.py",
                     "scripts/init_env.py", "docs/github/workflows/x.md"):
            with self.subTest(path=path):
                self.assertEqual(self.decide(pr(), [file(path)]), "merge")

    def test_a_rename_counts_under_its_old_name(self):
        files = [file("docs/old-gate.py", previous="scripts/gate_check.py")]
        self.assertEqual(self.decide(pr(), files), "stop")

    def test_a_file_list_not_read_whole_stops(self):
        self.assertEqual(self.decide(pr(changed_files=2), [file("README.md")]), "stop")

    def test_a_missing_file_list_stops(self):
        self.assertEqual(self.decide(pr(), None), "stop")

    def test_protected_paths_lists_both_names_of_a_rename(self):
        files = [file(".github/workflows/b.yml", previous=".github/workflows/a.yml"),
                 file("README.md")]
        self.assertEqual(gate.protected_paths(files),
                         [".github/workflows/a.yml", ".github/workflows/b.yml"])


class CommandLine(unittest.TestCase):
    def main(self, *args, pull=None, files=None):
        with tempfile.TemporaryDirectory() as d:
            def write(name, data):
                path = os.path.join(d, name)
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(data, f)
                return path
            runs = write("runs.json", [run("static")])
            status = write("status.json", NO_STATUSES)
            extra = []
            if pull is not None:
                extra += ["--pr", write("pr.json", pull)]
            if files is not None:
                extra += ["--files", write("files.json", files)]
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                code = gate.main(["auto_merge_gate.py", *args, *extra, runs, status])
            return code, out.getvalue().strip()

    def test_without_require_merges(self):
        self.assertEqual(self.main(), (0, "merge"))

    def test_require_names_a_check_to_wait_for(self):
        self.assertEqual(self.main("--require", "gate-check"), (0, "wait"))

    def test_pr_and_files_merge_an_ordinary_pr(self):
        self.assertEqual(self.main(pull=pr(), files=[file("README.md")]), (0, "merge"))

    def test_pr_and_files_stop_a_draft(self):
        self.assertEqual(self.main(pull=pr(draft=True), files=[file("README.md")]), (0, "stop"))

    def test_pr_without_files_stops(self):
        self.assertEqual(self.main(pull=pr()), (0, "stop"))

    def test_options_in_any_order(self):
        with tempfile.TemporaryDirectory() as d:
            paths = {}
            for name, data in (("runs", [run("static")]), ("status", NO_STATUSES),
                               ("pr", pr()), ("files", [file("scripts/gate_check.py")])):
                paths[name] = os.path.join(d, name)
                with open(paths[name], "w", encoding="utf-8") as f:
                    json.dump(data, f)
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                code = gate.main(["auto_merge_gate.py", "--files", paths["files"],
                                  "--require", "static", "--pr", paths["pr"],
                                  paths["runs"], paths["status"]])
            self.assertEqual((code, out.getvalue().strip()), (0, "stop"))


def ref(owner, name, number):
    return {"number": number, "repository": {"name": name, "owner": {"login": owner}}}


class IssuesToClose(unittest.TestCase):
    def test_same_repository_issues_are_closed_in_order(self):
        refs = [ref("stella-rain", "app", 9), ref("stella-rain", "app", 4)]
        self.assertEqual(gate.issues_to_close(refs, "stella-rain/app"), [4, 9])

    def test_issues_in_other_repositories_are_left_alone(self):
        refs = [ref("stella-rain", "core", 5), ref("stella-rain", "app", 4)]
        self.assertEqual(gate.issues_to_close(refs, "stella-rain/app"), [4])

    def test_repository_names_compare_without_case(self):
        refs = [ref("Stella-Rain", "App", 4)]
        self.assertEqual(gate.issues_to_close(refs, "stella-rain/app"), [4])

    def test_duplicates_are_closed_once(self):
        refs = [ref("stella-rain", "app", 4), ref("stella-rain", "app", 4)]
        self.assertEqual(gate.issues_to_close(refs, "stella-rain/app"), [4])

    def test_no_references_closes_nothing(self):
        self.assertEqual(gate.issues_to_close([], "stella-rain/app"), [])


if __name__ == "__main__":
    unittest.main()
