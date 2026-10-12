"""Tests for gates/log_batch_gate.py, the Log Batch Gate (ADR-040): a synthetic capture across a
UTC hour replayed through the real batch, read back as OpenSearch would hold it.
Run: python3 -m unittest discover -s gates"""

import gzip
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import fixtures  # noqa: E402
import gate  # noqa: E402
import log_batch_gate as lbg  # noqa: E402
from test_check_logs import PODS, polaris_lines  # noqa: E402

BATCH = os.path.join(ROOT, "images", "polaris-log-batch", "polaris_log_batch.py")
HOUR, ROLL = "20261011-17", "2026-10-11-17"
ROLL_MTIME, CURRENT_MTIME = 1791741601, 1791741690  # 18:00:01Z, 18:01:30Z
NOW = "2026-10-11T19:01:01Z"


def at(line, hour, second):
    record = json.loads(line)
    record["timestamp"] = f"2026-10-11T{hour}:{second // 60:02d}:{second % 60:02d}.5Z"
    return json.dumps(record) + "\n"


def capture(directory):
    """What Create Fixtures copies: each pod's roll of hour 17 (BEFORE's traffic, with the 500s)
    and its current file (AFTER's), with the manifest it writes."""
    sent_before = gate.logs_request_ids(fixtures.BEFORE, 2, PODS, failing=True)
    sent_after = gate.logs_request_ids(fixtures.AFTER, 2, PODS)
    for prefix_sent, hour, name, mtime, gz in (
            (sent_before, "17", "polaris-{pod}.log." + ROLL + ".gz", ROLL_MTIME, True),
            (sent_after, "18", "polaris-{pod}.log", CURRENT_MTIME, False)):
        lines = {pod: [] for pod in PODS}
        second = 60 if hour == "18" else 40 * 60
        for rid, (pod, kind) in sorted(prefix_sent.items()):
            for line in polaris_lines(pod or PODS[0], 0, rid, kind):
                lines[pod or PODS[0]].append(at(line, hour, second))
                second += 1
        for pod in PODS:
            path = os.path.join(directory, name.format(pod=pod))
            with (gzip.open(path, "wt", encoding="utf-8") if gz
                  else open(path, "w", encoding="utf-8")) as f:
                f.writelines(lines[pod])
            os.utime(path, (mtime, mtime))
    manifest = fixtures.build_manifest(
        directory, spec_hash=fixtures.spec_hash(fixtures.checkout_reader()), commit="c" * 40,
        polaris_image="apache/polaris:1.6.0@sha256:" + "a" * 64,
        boundary="2026-10-11T18:00:00Z", hour=HOUR, now=NOW, pods=PODS, count=2,
        before=fixtures.BEFORE, after=fixtures.AFTER, down=fixtures.DOWN_ENDPOINT)
    with open(os.path.join(directory, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f)
    return manifest


def local_batch(work, now):
    """The batch as its container runs it, here with this machine's Python."""
    return subprocess.run([sys.executable, BATCH, "--log-dir", work, "--no-pod-list",
                           "--now", now], capture_output=True, text=True, check=True).stdout


def as_indexed(work):
    """Every output line as the stand-in Fluent Bit ships it: its path, Fluent Bit's
    @timestamp, in the index of its directory."""
    docs = {}
    for sub, index in ((d, lbg.INDICES[d]) for d in gate.OUTPUT_DIRS):
        for name in sorted(os.listdir(os.path.join(work, sub))):
            with open(os.path.join(work, sub, name), encoding="utf-8") as f:
                docs.setdefault(index, []).extend(
                    {**json.loads(line), "source_file": f"/logs/{sub}/{name}",
                     "@timestamp": "2026-10-11T19:01:02.000Z"} for line in f)
    return docs


GOOD_MAPPINGS = {
    lbg.INDICES["processed-logs"]: {"mappings": {"properties": {
        "mdc": {"properties": {"requestId": {"type": "text"}}},
        "http_status": {"type": "long"},
        "exception": {"properties": {"frames": {"properties": {"line": {"type": "long"}}}}}}}},
    lbg.INDICES["aggregated-logs"]: {"mappings": {"properties": {
        "report_type": {"type": "text"}}}}}


class Replay(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.artifact = os.path.join(cls._tmp.name, "artifact")
        os.makedirs(cls.artifact)
        cls.manifest = capture(cls.artifact)
        cls.work = os.path.join(cls._tmp.name, "work")
        lbg.stage(cls.artifact, cls.manifest, cls.work)
        cls.events, cls.before, cls.after = lbg.replay(cls.work, cls.manifest, local_batch)
        cls.docs = as_indexed(cls.work)
        cls.records = lbg.documents(cls.docs)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def checks(self, **change):
        args = {"manifest": self.manifest, "events": self.events, "before": self.before,
                "after": self.after, "records": self.records, "mappings": GOOD_MAPPINGS,
                "fluent_bit_log": ""}
        args.update(change)
        return lbg.checks(**args)

    def test_a_good_capture_passes_every_check(self):
        problems, notes = self.checks()
        self.assertEqual(problems, [])
        text = "\n".join(notes)
        self.assertIn("2 500s kept with their ERROR line", text)
        self.assertIn(f"hours {HOUR} and 20261011-18", text)
        self.assertIn("OpenSearch", text)

    def test_the_records_lose_fluent_bits_timestamp_and_keep_their_path(self):
        self.assertTrue(self.records)
        self.assertFalse([r for r in self.records if "@timestamp" in r])
        self.assertTrue(all(r["source_file"].startswith("/logs/") for r in self.records))

    def test_the_sent_requests_are_both_rounds_with_the_500s_in_the_first(self):
        sent = lbg.sent(self.manifest)
        self.assertEqual(sent[f"{fixtures.BEFORE}-{PODS[0]}-failed-1"], (PODS[0], "failed"))
        self.assertNotIn(f"{fixtures.AFTER}-{PODS[0]}-failed-1", sent)
        self.assertIn(f"{fixtures.AFTER}-{PODS[1]}-commit-1", sent)

    def test_a_request_in_the_wrong_hour_fails(self):
        rid = f"{fixtures.AFTER}-{PODS[0]}-denied-1"
        records = [({**r, "source_file": f"/logs/processed-logs/{HOUR}.jsonl"}
                    if r.get("mdc", {}).get("requestId") == rid else r) for r in self.records]
        problems, _ = self.checks(records=records)
        self.assertIn(f"request {rid}", "\n".join(problems))
        self.assertIn("hour", "\n".join(problems))

    def test_an_hour_not_published_fails(self):
        events = [[e for e in self.events[0] if e.get("hour") != "20261011-18"],
                  self.events[1]]
        problems, _ = self.checks(events=events)
        self.assertIn("20261011-18", "\n".join(problems))

    def test_a_roll_the_batch_did_not_read_fails(self):
        events = [[{**e, "sources": [s for s in e.get("sources", [])
                                     if not s["file"].endswith(".gz")]}
                   for e in self.events[0]], self.events[1]]
        problems, _ = self.checks(events=events)
        self.assertIn(f"polaris-{PODS[0]}.log.{ROLL}.gz", "\n".join(problems))

    def test_a_document_opensearch_refused_fails(self):
        problems, _ = self.checks(records=self.records[1:])
        self.assertTrue(problems)

    def test_fluent_bit_errors_fail(self):
        problems, _ = self.checks(
            fluent_bit_log="[2026/10/11 19:01:03] [error] [output:opensearch:opensearch.0] "
                           "HTTP status=400 URI=/_bulk, response:\n{\"errors\":true}")
        self.assertIn("Fluent Bit", "\n".join(problems))


class Mappings(unittest.TestCase):
    def test_good_mappings_pass(self):
        self.assertEqual(lbg.mapping_problems(GOOD_MAPPINGS), [])

    def test_http_status_not_a_number_fails(self):
        bad = json.loads(json.dumps(GOOD_MAPPINGS))
        bad[lbg.INDICES["processed-logs"]]["mappings"]["properties"]["http_status"] = {
            "type": "text"}
        self.assertIn("http_status", "\n".join(lbg.mapping_problems(bad)))

    def test_mdc_or_exception_not_an_object_fails(self):
        for field in ("mdc", "exception"):
            with self.subTest(field=field):
                bad = json.loads(json.dumps(GOOD_MAPPINGS))
                bad[lbg.INDICES["processed-logs"]]["mappings"]["properties"][field] = {
                    "type": "text"}
                self.assertIn(field, "\n".join(lbg.mapping_problems(bad)))

    def test_a_field_missing_or_an_index_missing_fails(self):
        bad = json.loads(json.dumps(GOOD_MAPPINGS))
        del bad[lbg.INDICES["processed-logs"]]["mappings"]["properties"]["exception"]
        self.assertIn("exception", "\n".join(lbg.mapping_problems(bad)))
        self.assertTrue(lbg.mapping_problems({}))


class Verify(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.manifest = capture(self.dir)

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_good_artifact_passes_and_its_mtimes_come_back(self):
        path = os.path.join(self.dir, f"polaris-{PODS[0]}.log")
        os.utime(path, (1, 1))
        self.assertEqual(lbg.verify(self.dir), self.manifest)
        self.assertEqual(int(os.stat(path).st_mtime), CURRENT_MTIME)

    def test_a_changed_byte_is_refused(self):
        with open(os.path.join(self.dir, f"polaris-{PODS[0]}.log"), "a",
                  encoding="utf-8") as f:
            f.write("\n")
        with self.assertRaisesRegex(ValueError, f"polaris-{PODS[0]}.log"):
            lbg.verify(self.dir)

    def test_a_missing_or_unknown_file_is_refused(self):
        os.remove(os.path.join(self.dir, f"polaris-{PODS[1]}.log"))
        with self.assertRaisesRegex(ValueError, "missing"):
            lbg.verify(self.dir)
        shutil.copyfile(os.path.join(self.dir, f"polaris-{PODS[0]}.log"),
                        os.path.join(self.dir, "polaris-x.log"))
        with self.assertRaises(ValueError):
            lbg.verify(self.dir)

    def test_another_spec_hash_is_refused(self):
        self.manifest["spec_hash"] = "0" * 64
        with open(os.path.join(self.dir, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(self.manifest, f)
        with self.assertRaisesRegex(ValueError, "spec hash"):
            lbg.verify(self.dir)

    def test_no_manifest_is_refused(self):
        os.remove(os.path.join(self.dir, "manifest.json"))
        with self.assertRaisesRegex(ValueError, "manifest"):
            lbg.verify(self.dir)


class Cleanup(unittest.TestCase):
    def test_files_the_batch_wrote_as_another_user_do_not_fail_the_cleanup(self):
        # On the runner the batch's directories belong to uid 10000: unlink and chmod both
        # fail there (run 38165411360), which TemporaryDirectory's cleanup does not survive.
        from unittest import mock
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, "logs", "aggregated-logs"))
        open(os.path.join(d, "logs", "aggregated-logs", "20261011-19.jsonl"), "w").close()
        denied = PermissionError(13, "Permission denied")
        try:
            with mock.patch("os.unlink", side_effect=denied), \
                    mock.patch("os.rmdir", side_effect=denied), \
                    mock.patch("os.chmod", side_effect=PermissionError(1, "not permitted")):
                lbg.remove_tree(d)
        finally:
            shutil.rmtree(d)


class Commands(unittest.TestCase):
    def test_opensearch_is_pinned_single_node_without_security_on_localhost(self):
        pins = gate.load_pins()
        image = pins["standin_images"]["opensearch"]["image"]
        self.assertRegex(image, r"^opensearchproject/opensearch:3\.6\.0@sha256:[0-9a-f]{64}$")
        argv = lbg.opensearch_command(image, "os", "net")
        self.assertEqual(argv[:3], ["docker", "run", "-d"])
        for want in ("discovery.type=single-node", "DISABLE_SECURITY_PLUGIN=true",
                     "DISABLE_INSTALL_DEMO_CONFIG=true", "OPENSEARCH_JAVA_OPTS=-Xms512m -Xmx512m"):
            self.assertIn(want, argv)
        self.assertIn(f"127.0.0.1:{lbg.OPENSEARCH_PORT}:9200", argv)
        self.assertEqual(argv[-1], image)

    def test_the_batch_runs_without_a_network_or_the_kubernetes_api(self):
        argv = lbg.batch_command("lakehouse/b:1", "/w", NOW)
        self.assertEqual(argv[:6], ["docker", "run", "--rm", "--network", "none", "-v"])
        self.assertIn("/w:/logs", argv)
        self.assertEqual(argv[argv.index("lakehouse/b:1") + 1:],
                         ["--log-dir", "/logs", "--no-pod-list", "--now", NOW])

    def test_fluent_bit_reads_both_directories_read_only_into_one_index_each(self):
        argv = lbg.fluent_bit_command("fluent/fluent-bit:5@sha256:" + "f" * 64, "/w", "net",
                                      "os")
        self.assertIn("/w:/logs:ro", argv)
        self.assertIn(f"{gate.READER_UID}:{gate.READER_UID}", argv)
        self.assertIn("--read-only", argv)
        text = " ".join(argv)
        self.assertIn("path=/logs/processed-logs/*.jsonl,/logs/aggregated-logs/*.jsonl", text)
        self.assertIn("exit_on_eof=true", text)
        self.assertIn("path_key=source_file", text)
        for sub, index in lbg.INDICES.items():
            self.assertIn(f"-m lb.logs.{sub}.* -p host=os -p port=9200 -p index={index}", text)
        self.assertIn("trace_error=on", text)
        self.assertIn("suppress_type_name=on", text)


if __name__ == "__main__":
    unittest.main()
