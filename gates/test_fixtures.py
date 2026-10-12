"""Tests for gates/fixtures.py, Create Fixtures (ADR-040).
Run: python3 -m unittest discover -s gates"""

import datetime as dt
import gzip
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import fixtures  # noqa: E402
import gate  # noqa: E402

VALUES = fixtures.VALUES


def checkout(root=ROOT):
    """A reader of this checkout's files, as the spec hash reads them."""
    def read(path):
        try:
            with open(os.path.join(root, path), encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            return None
    return read


def changed(read, path, old, new):
    """`read` with one file's text edited."""
    def edited(p):
        text = read(p)
        if p != path:
            return text
        assert old in text, (path, old)
        return text.replace(old, new)
    return edited


class SpecHash(unittest.TestCase):
    def setUp(self):
        self.read = checkout()
        self.hash = fixtures.spec_hash(self.read)

    def test_is_a_sha256_and_stable(self):
        self.assertRegex(self.hash, r"^[0-9a-f]{64}$")
        self.assertEqual(fixtures.spec_hash(checkout()), self.hash)

    def test_the_spec_files_are_the_traffic_the_workflow_and_the_capture(self):
        self.assertEqual(fixtures.SPEC_FILES, ("gates/logs_traffic.sh",
                                               ".github/workflows/create-fixtures.yml",
                                               "gates/fixtures.py"))

    def test_each_spec_file_changes_it(self):
        for path in fixtures.SPEC_FILES:
            with self.subTest(path=path):
                read = lambda p, path=path: (self.read(p) or "") + ("\n#\n" if p == path else "")
                self.assertNotEqual(fixtures.spec_hash(read), self.hash)

    def test_a_missing_spec_file_changes_it(self):
        read = lambda p: None if p == "gates/fixtures.py" else self.read(p)
        self.assertNotEqual(fixtures.spec_hash(read), self.hash)

    def test_polaris_image_log_file_settings_and_tz_change_it(self):
        for old, new in (('tag: "1.6.0@', 'tag: "1.6.1@'),
                         ("repository: apache/polaris", "repository: other/polaris"),
                         ('file-suffix: ".yyyy-MM-dd-HH.gz"', 'file-suffix: ".yyyy-MM-dd.gz"'),
                         ('max-backup-index: "50"', 'max-backup-index: "51"'),
                         ("    value: UTC", "    value: Etc/UTC")):
            with self.subTest(old=old):
                self.assertNotEqual(fixtures.spec_hash(changed(self.read, VALUES, old, new)),
                                    self.hash)

    def test_other_polaris_values_and_comments_do_not(self):
        for old, new in (("replicaCount: 2", "replicaCount: 3"),
                         ("polaris.authorization.type: opa",
                          "polaris.authorization.type: internal"),
                         ("# The file appender (ADR-036)", "# The file appender, ADR-036")):
            with self.subTest(old=old):
                self.assertEqual(fixtures.spec_hash(changed(self.read, VALUES, old, new)),
                                 self.hash)

    def test_the_values_spec_names_each_part(self):
        spec = fixtures.values_spec(self.read(VALUES))
        self.assertIn("repository: apache/polaris", spec)
        self.assertTrue(any(line.startswith('tag: "1.6.0@sha256:') for line in spec))
        self.assertIn('quarkus.log.file.rotation.file-suffix: ".yyyy-MM-dd-HH.gz"', spec)
        self.assertEqual(spec[-2:], ["- name: TZ", "value: UTC"])
        self.assertFalse([line for line in spec if line.startswith("#")])

    def test_values_without_a_part_are_refused(self):
        text = self.read(VALUES).replace("  - name: TZ\n    value: UTC\n", "")
        with self.assertRaises(ValueError):
            fixtures.values_spec(text)


class Changes(unittest.TestCase):
    def test_the_spec_changed_when_the_hashes_differ(self):
        head = checkout()
        base = changed(head, "gates/logs_traffic.sh", "set -u", "set -eu")
        self.assertEqual(fixtures.changes(base, head), (True, fixtures.spec_hash(head)))
        self.assertEqual(fixtures.changes(head, head), (False, fixtures.spec_hash(head)))

    def test_a_base_without_the_spec_files_changed(self):
        head = checkout()
        changed_, _ = fixtures.changes(lambda p: None if p in fixtures.SPEC_FILES else head(p),
                                       head)
        self.assertTrue(changed_)

    def test_reads_a_commit_with_git_show(self):
        with tempfile.TemporaryDirectory() as d:
            subprocess.run(["git", "init", "-q", d], check=True)
            os.makedirs(os.path.join(d, "gates"))
            with open(os.path.join(d, "gates", "logs_traffic.sh"), "w", encoding="utf-8") as f:
                f.write("x\n")
            subprocess.run(["git", "-C", d, "add", "-A"], check=True)
            subprocess.run(["git", "-C", d, "-c", "user.name=t", "-c",
                            "user.email=t@example.invalid", "commit", "-qm", "c"], check=True)
            read = fixtures.git_reader(d, "HEAD")
            self.assertEqual(read("gates/logs_traffic.sh"), "x\n")
            self.assertIsNone(read("gates/fixtures.py"))


class Wait(unittest.TestCase):
    def test_until_the_next_hour_plus_a_minute(self):
        for t, seconds in (("2026-10-11T17:30:00", 30 * 60 + 60),
                           ("2026-10-11T17:59:30", 90),
                           ("2026-10-11T17:00:00", 3660)):
            with self.subTest(t=t):
                now = dt.datetime.fromisoformat(t).replace(tzinfo=dt.timezone.utc)
                self.assertEqual(fixtures.wait_seconds(now), seconds)
                self.assertLessEqual(fixtures.wait_seconds(now), fixtures.MAX_WAIT)

    def test_the_boundary_is_that_hour(self):
        now = dt.datetime(2026, 10, 11, 17, 30, tzinfo=dt.timezone.utc)
        self.assertEqual(fixtures.boundary(now),
                         dt.datetime(2026, 10, 11, 18, tzinfo=dt.timezone.utc))


def write(path, lines, mtime=1791739386, gz=False):
    opener = gzip.open if gz else open
    with opener(path, "wt", encoding="utf-8") as f:
        f.writelines(json.dumps(line) + "\n" for line in lines)
    os.utime(path, (mtime, mtime))


class Files(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_the_claim_directory_is_the_one_local_path_made_for_polaris_logs(self):
        for name in ("pvc-1_catalog-polaris_polaris-logs", "pvc-2_other_polaris-logs",
                     "pvc-3_catalog-polaris_pgdata"):
            os.makedirs(os.path.join(self.dir, name))
        self.assertEqual(fixtures.claim_dir(self.dir, "catalog-polaris"),
                         os.path.join(self.dir, "pvc-1_catalog-polaris_polaris-logs"))
        os.makedirs(os.path.join(self.dir, "pvc-4_catalog-polaris_polaris-logs"))
        with self.assertRaises(ValueError):
            fixtures.claim_dir(self.dir, "catalog-polaris")

    def test_only_polaris_files_at_the_top_of_the_claim(self):
        for rel in ("polaris-p-a.log", "polaris-p-a.log.2026-10-11-17.gz",
                    "polaris-p-a.log.2026-10-11-17.1.gz", "other.log", "done/polaris-p-b.log"):
            os.makedirs(os.path.dirname(os.path.join(self.dir, rel)), exist_ok=True)
            open(os.path.join(self.dir, rel), "w").close()
        self.assertEqual(fixtures.log_files(self.dir),
                         ["polaris-p-a.log", "polaris-p-a.log.2026-10-11-17.1.gz",
                          "polaris-p-a.log.2026-10-11-17.gz"])

    def test_each_file_with_its_hash_size_mtime_and_lines(self):
        write(os.path.join(self.dir, "polaris-p-a.log.2026-10-11-17.gz"), [{"a": 1}] * 3,
              gz=True)
        path = os.path.join(self.dir, "polaris-p-a.log.2026-10-11-17.gz")
        with open(path, "rb") as f:
            data = f.read()
        self.assertEqual(fixtures.file_entry(path),
                         {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
                          "mtime": 1791739386, "lines": 3})

    def test_the_manifest_holds_its_meta_and_every_file(self):
        write(os.path.join(self.dir, "polaris-p-a.log"), [{"a": 1}])
        write(os.path.join(self.dir, "polaris-p-a.log.2026-10-11-17.gz"), [{"a": 1}], gz=True)
        m = fixtures.build_manifest(self.dir, spec_hash="h" * 64, pods=["p-a"])
        self.assertEqual(m["spec_hash"], "h" * 64)
        self.assertEqual(m["pods"], ["p-a"])
        self.assertEqual(sorted(m["files"]),
                         ["polaris-p-a.log", "polaris-p-a.log.2026-10-11-17.gz"])

    def test_each_pod_needs_one_roll_for_the_hour_and_its_current_file(self):
        files = {"polaris-p-a.log": {}, "polaris-p-a.log.2026-10-11-17.gz": {},
                 "polaris-p-b.log": {}}
        problems = fixtures.capture_problems(files, ["p-a", "p-b"], "2026-10-11-17")
        self.assertEqual(problems, ["pod p-b: no roll polaris-p-b.log.2026-10-11-17.gz"])
        files["polaris-p-b.log.2026-10-11-17.gz"] = {}
        self.assertEqual(fixtures.capture_problems(files, ["p-a", "p-b"], "2026-10-11-17"), [])
        del files["polaris-p-a.log"]
        self.assertEqual(fixtures.capture_problems(files, ["p-a", "p-b"], "2026-10-11-17"),
                         ["pod p-a: no current file polaris-p-a.log"])


class Commands(unittest.TestCase):
    def test_the_batch_is_paused_on_the_gates_context(self):
        env = {"NS_CATALOG_POLARIS": "catalog-polaris"}
        self.assertEqual(fixtures.suspend_command(env),
                         ["kubectl", "--context", gate.CONTEXT, "-n", "catalog-polaris",
                          "patch", "cronjob/polaris-log-batch", "--type=merge",
                          "-p", '{"spec": {"suspend": true}}'])

    def test_the_traffic_before_the_hour_reaches_the_down_endpoint_and_after_it_not(self):
        self.assertEqual(fixtures.DOWN_ENDPOINT, "http://127.0.0.1:9")
        self.assertEqual((fixtures.BEFORE, fixtures.AFTER), ("fx-pre", "fx-post"))


class Cli(unittest.TestCase):
    def test_spec_hash_prints_the_hash(self):
        out = subprocess.run([sys.executable, os.path.join(HERE, "fixtures.py"), "spec-hash"],
                             capture_output=True, text=True, check=True, cwd=ROOT).stdout
        self.assertEqual(out.strip(), fixtures.spec_hash(checkout()))

    def test_changes_prints_github_outputs(self):
        out = subprocess.run([sys.executable, os.path.join(HERE, "fixtures.py"), "changes",
                              "HEAD"], capture_output=True, text=True, check=True,
                             cwd=ROOT).stdout.split()
        self.assertEqual(len(out), 2)
        self.assertRegex(out[0], r"^spec_changed=(true|false)$")
        self.assertRegex(out[1], r"^spec_hash=[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
