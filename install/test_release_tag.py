"""Tests for release_tag.py. Run: python3 -m unittest discover -s install"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import release  # noqa: E402
import release_tag as rt  # noqa: E402

PINS = {
    "node_image": {"image": "kindest/node:v1.36.4@sha256:" + "a" * 64},
    "cnpg_chart": {"version": "0.29.1", "operator_version": "1.30.1"},
}
SUPPORTED = {"cnpg_operator": ["1.30"]}
BATCH_IMAGE = "ghcr.io/o/r/polaris-log-batch:0123456789ab"


def check(name, conclusion="success", status="completed", url=None):
    return {"name": name, "status": status, "conclusion": conclusion,
            "html_url": url or f"https://github.com/o/r/actions/runs/1/job/{len(name)}"}


def all_green():
    return [check(name) for name in rt.REQUIRED_CHECKS]


class Previous(unittest.TestCase):
    def test_no_tag_gives_none(self):
        self.assertIsNone(rt.previous([]))

    def test_the_latest_date_wins(self):
        tags = ["platform-2026.10.10", "platform-2026.11.02", "platform-2026.10.31"]
        self.assertEqual(rt.previous(tags), "platform-2026.11.02")

    def test_a_second_release_the_same_day_is_later_and_10_follows_9(self):
        tags = ["platform-2026.10.10", "platform-2026.10.10.2", "platform-2026.10.10.10",
                "platform-2026.10.10.9"]
        self.assertEqual(rt.previous(tags), "platform-2026.10.10.10")

    def test_other_names_are_ignored(self):
        self.assertIsNone(rt.previous(["v1.0.0", "platform-x", "platform-2026.10.10.1a"]))


class NextTag(unittest.TestCase):
    def test_the_first_release_of_a_day_has_no_suffix(self):
        self.assertEqual(rt.next_tag("2026-10-10", ["platform-2026.10.09"]),
                         "platform-2026.10.10")

    def test_the_second_release_of_a_day_is_2(self):
        self.assertEqual(rt.next_tag("2026-10-10", ["platform-2026.10.10"]),
                         "platform-2026.10.10.2")

    def test_the_next_follows_the_highest_suffix(self):
        tags = ["platform-2026.10.10", "platform-2026.10.10.2", "platform-2026.10.10.3"]
        self.assertEqual(rt.next_tag("2026-10-10", tags), "platform-2026.10.10.4")

    def test_a_malformed_date_raises(self):
        for date in ("2026-1-10", "20261010", "2026.10.10"):
            with self.subTest(date=date), self.assertRaises(ValueError):
                rt.next_tag(date, [])


class Verify(unittest.TestCase):
    def test_all_five_green_passes(self):
        self.assertEqual(rt.verify(all_green()), [])

    def test_the_required_checks_are_the_rulesets(self):
        self.assertEqual(rt.REQUIRED_CHECKS, ("the head is main", "static", "dry-run / dry-run",
                                              "smoke upgrade / smoke", "smoke fresh / smoke"))

    def test_a_missing_check_is_named(self):
        runs = [r for r in all_green() if r["name"] != "smoke fresh / smoke"]
        self.assertEqual(rt.verify(runs), ["smoke fresh / smoke: not reported"])

    def test_a_failed_or_skipped_check_is_named(self):
        for conclusion in ("failure", "skipped", "cancelled", "neutral"):
            with self.subTest(conclusion=conclusion):
                runs = [r for r in all_green() if r["name"] != "dry-run / dry-run"]
                runs.append(check("dry-run / dry-run", conclusion=conclusion))
                self.assertEqual(rt.verify(runs), [f"dry-run / dry-run: {conclusion}"])

    def test_a_running_check_is_named(self):
        runs = [r for r in all_green() if r["name"] != "static"]
        runs.append(check("static", conclusion=None, status="in_progress"))
        self.assertEqual(rt.verify(runs), ["static: in_progress"])

    def test_one_green_run_of_a_name_is_enough(self):
        runs = all_green() + [check("static", conclusion="failure")]
        self.assertEqual(rt.verify(runs), [])

    def test_other_checks_do_not_matter(self):
        runs = all_green() + [check("gate-check", conclusion="failure")]
        self.assertEqual(rt.verify(runs), [])


class Notes(unittest.TestCase):
    def notes(self, previous="platform-2026.10.09"):
        return rt.notes(tag="platform-2026.10.10", commit="c" * 40, previous=previous,
                        pr="https://github.com/o/r/pull/7", checks=all_green(),
                        log=["abc1234 A change", "def5678 Another"], pins=PINS,
                        supported=SUPPORTED, batch_image=BATCH_IMAGE)

    def test_names_the_tag_commit_and_pr(self):
        text = self.notes()
        self.assertIn("# platform-2026.10.10", text)
        self.assertIn("c" * 40, text)
        self.assertIn("https://github.com/o/r/pull/7", text)

    def test_names_the_gate_kubernetes_and_its_node_image_digest(self):
        text = self.notes()
        self.assertIn("Kubernetes v1.36.4", text)
        self.assertIn("kindest/node:v1.36.4@sha256:" + "a" * 64, text)

    def test_names_the_cnpg_pins_and_the_supported_operators(self):
        text = self.notes()
        self.assertIn("chart 0.29.1, operator 1.30.1", text)
        self.assertIn("1.30", text)

    def test_links_every_release_gate_check(self):
        text = self.notes()
        for run in all_green():
            self.assertIn(f"[{run['name']}]({run['html_url']})", text)

    def test_lists_the_changes_since_the_previous_release(self):
        text = self.notes()
        self.assertIn("since platform-2026.10.09", text)
        self.assertIn("- abc1234 A change", text)

    def test_names_the_batch_image_with_a_digest_the_tag_job_fills_in(self):
        text = self.notes()
        self.assertIn(f"`{BATCH_IMAGE}@{rt.DIGEST_PLACEHOLDER}`", text)
        self.assertEqual(text.count(rt.DIGEST_PLACEHOLDER), 1)
        self.assertNotIn("none in this release", text)

    def test_the_first_release_says_so(self):
        self.assertIn("First release: nothing to upgrade from.", self.notes(previous=None))


class Main(unittest.TestCase):
    def run_main(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = rt.main(["release_tag.py"] + argv)
        return code, out.getvalue()

    def test_previous_prints_the_tag_or_nothing(self):
        self.assertEqual(self.run_main(["previous", "platform-2026.10.10"]),
                         (0, "platform-2026.10.10\n"))
        self.assertEqual(self.run_main(["previous"]), (0, ""))

    def test_next_prints_the_name(self):
        self.assertEqual(self.run_main(["next", "2026-10-10", "platform-2026.10.10"]),
                         (0, "platform-2026.10.10.2\n"))

    def test_verify_exits_1_and_names_the_problems(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "checks.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(all_green()[1:], f)
            self.assertEqual(self.run_main(["verify", path]), (1, "the head is main: not reported\n"))
            with open(path, "w", encoding="utf-8") as f:
                json.dump(all_green(), f)
            self.assertEqual(self.run_main(["verify", path])[0], 0)

    def test_notes_reads_the_committed_pins(self):
        with tempfile.TemporaryDirectory() as d:
            checks, log = os.path.join(d, "checks.json"), os.path.join(d, "log.txt")
            with open(checks, "w", encoding="utf-8") as f:
                json.dump(all_green(), f)
            with open(log, "w", encoding="utf-8") as f:
                f.write("abc1234 A change\n")
            code, out = self.run_main(["notes", "platform-2026.10.10", "c" * 40, "-",
                                       "https://github.com/o/r/pull/7", checks, log])
        self.assertEqual(code, 0)
        self.assertIn("node image `kindest/node:v1.36.4@sha256:", out)
        self.assertIn("First release", out)
        self.assertIn(f"`{release.batch_image()}@{rt.DIGEST_PLACEHOLDER}`", out)

    def test_unknown_arguments_exit_2(self):
        self.assertEqual(self.run_main(["release"])[0], 2)


if __name__ == "__main__":
    unittest.main()
