"""Tests for the log batch on the gate (ADR-036): `gate.py batch-image` and `check-logs`, and
logs_traffic.sh run here with this machine's curl against local fakes of Polaris replicas.
Run: python3 -m unittest discover -s gates"""

import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "images", "polaris-log-batch"))
import gate  # noqa: E402
import polaris_log_batch  # noqa: E402
from test_check_polaris import CREDS, TOKEN, fake_polaris  # noqa: E402

ENV = {"NS_CATALOG_POLARIS": "catalog-polaris"}
PODS = ["polaris-6d9f-aaaaa", "polaris-6d9f-bbbbb"]
PREFIX = "gate-logs-42"
NOW = "2026-10-11T13:01:01Z"


def polaris_line(pod, second, request_id=None):
    """One line as Polaris 1.6.0 writes it (docs/contracts/polaris-logs.md)."""
    return json.dumps({
        "timestamp": f"2026-10-11T12:40:{second:02d}.753600155Z", "sequence": second,
        "loggerClassName": "org.jboss.logging.Logger",
        "loggerName": "io.quarkus.http.access-log", "level": "INFO",
        "message": "10.0.0.9 - root [11/Oct/2026:12:40:47 +0000] "
                   "\"GET /api/management/v1/catalogs HTTP/1.1\" 200 15",
        "threadName": "executor-thread-2", "threadId": 31,
        "mdc": {"requestId": request_id or f"generated-{pod}-{second}", "realmId": "POLARIS"},
        "ndc": "", "hostName": pod, "processName": "/usr/lib/jvm/java-21/bin/java",
        "processId": 1}) + "\n"


def batch_outputs(log_dir, lines_by_pod, now=NOW):
    """Polaris's files written, then the real batch run over them as the gate runs it."""
    for pod, lines in lines_by_pod.items():
        path = os.path.join(log_dir, f"polaris-{pod}.log")
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(lines)
        os.utime(path, (1791722460, 1791722460))  # 2026-10-11T12:41:00Z
    out = subprocess.run([sys.executable, os.path.join(ROOT, "images", "polaris-log-batch",
                                                       "polaris_log_batch.py"),
                          "--log-dir", log_dir, "--no-pod-list", "--now", now],
                         capture_output=True, text=True, check=True).stdout
    return gate.batch_events(out)


def read_as_fluent_bit(log_dir):
    """What the stand-in prints: every line of both directories with its path added."""
    records = []
    for sub in ("processed-logs", "aggregated-logs"):
        for name in sorted(os.listdir(os.path.join(log_dir, sub))):
            with open(os.path.join(log_dir, sub, name), encoding="utf-8") as f:
                for line in f:
                    records.append({**json.loads(line),
                                    "source_file": f"/logs/{sub}/{name}"})
    return records


def listing(log_dir):
    out = subprocess.run(["sh", "-s", "--", log_dir], input=gate.LOGS_LISTING,
                         capture_output=True, text=True, check=True).stdout
    return gate.parse_listing(out)


class Fixture:
    """The batch run twice over both pods' lines, and what the reader saw."""

    def __init__(self, sent=None):
        self.sent = sent or gate.logs_request_ids(PREFIX, 2, PODS)
        self.dir = tempfile.TemporaryDirectory()
        lines = {pod: [polaris_line(pod, 1)] for pod in PODS}
        for n, (rid, pod) in enumerate(sorted(self.sent.items()), 2):
            lines[pod].append(polaris_line(pod, n, rid))
        self.events = [batch_outputs(self.dir.name, lines)]
        self.before = listing(self.dir.name)
        out = subprocess.run([sys.executable, os.path.join(
            ROOT, "images", "polaris-log-batch", "polaris_log_batch.py"),
            "--log-dir", self.dir.name, "--no-pod-list", "--now", NOW],
            capture_output=True, text=True, check=True).stdout
        self.events.append(gate.batch_events(out))
        self.after = listing(self.dir.name)
        self.records = read_as_fluent_bit(self.dir.name)

    def problems(self, **change):
        args = {"sent": self.sent, "pods": PODS, "events": self.events,
                "before": self.before, "after": self.after, "records": self.records}
        args.update(change)
        return gate.logs_problems(**args)


class Pins(unittest.TestCase):
    def test_the_stand_in_fluent_bit_is_pinned_by_digest(self):
        pins = gate.load_pins()
        self.assertRegex(pins["standin_images"]["fluent_bit"]["image"],
                         r"^fluent/fluent-bit:\d+\.\d+\.\d+@sha256:[0-9a-f]{64}$")


class BatchImage(unittest.TestCase):
    def test_builds_the_image_release_py_names_then_loads_it_into_kind(self):
        build, load = gate.batch_image_commands()
        image = gate.release.batch_image()
        self.assertEqual(build[:2], ["docker", "build"])
        self.assertEqual(build[build.index("--tag") + 1], image)
        self.assertEqual(build[build.index("--file") + 1],
                         os.path.join(ROOT, "images", "polaris-log-batch", "Dockerfile"))
        self.assertEqual(build[-1], os.path.join(ROOT, "images", "polaris-log-batch"))
        self.assertEqual(load, ["kind", "load", "docker-image", image, "--name", gate.CLUSTER])


class Traffic(unittest.TestCase):
    def test_every_request_id_is_distinct_and_names_its_pod(self):
        sent = gate.logs_request_ids(PREFIX, 3, PODS)
        self.assertEqual(len(sent), 6)
        self.assertEqual(sorted(set(sent.values())), PODS)
        for rid, pod in sent.items():
            self.assertTrue(rid.startswith(f"{PREFIX}-{pod}-"), rid)

    def test_the_traffic_pod_gets_roots_credentials_and_asks_each_pod_by_its_address(self):
        argv = gate.logs_traffic_command(ENV, "curl:1@sha256:" + "e" * 64, "logs-traffic",
                                         PREFIX, 3, [(PODS[0], "http://10.0.0.1:8181"),
                                                     (PODS[1], "http://10.0.0.2:8181")])
        self.assertEqual(argv[:7], ["kubectl", "--context", gate.CONTEXT, "-n",
                                    "catalog-polaris", "run", "logs-traffic"])
        overrides = json.loads(argv[argv.index("--overrides") + 1])
        (container,) = overrides["spec"]["containers"]
        env = {e["name"]: e["valueFrom"]["secretKeyRef"] for e in container["env"]}
        self.assertEqual(env, {"ROOT_CLIENT_ID": {"name": "polaris-root", "key": "clientId"},
                               "ROOT_CLIENT_SECRET": {"name": "polaris-root",
                                                      "key": "clientSecret"}})
        self.assertEqual(argv[argv.index("--") + 1:],
                         ["sh", "-s", "--", "http://polaris:8181", "POLARIS", "3", PREFIX,
                          PODS[0], "http://10.0.0.1:8181", PODS[1], "http://10.0.0.2:8181"])


class TrafficScript(unittest.TestCase):
    def setUp(self):
        self.servers = []

    def tearDown(self):
        for server in self.servers:
            server.shutdown()
            server.server_close()

    def replica(self, **kwargs):
        server, seen = fake_polaris(**kwargs)
        self.servers.append(server)
        return f"http://127.0.0.1:{server.server_port}", seen

    def run_script(self, api, pods):
        with open(os.path.join(HERE, "logs_traffic.sh"), encoding="utf-8") as f:
            text = f.read()
        args = [a for name, url in pods for a in (name, url)]
        return subprocess.run(["sh", "-s", "--", api, "POLARIS", "2", PREFIX, *args],
                              input=text, capture_output=True, text=True,
                              env={**os.environ, **CREDS}, timeout=60)

    def test_each_pod_gets_its_own_request_ids_with_roots_token(self):
        (a, seen_a), (b, seen_b) = self.replica(), self.replica()
        done = self.run_script(a, [(PODS[0], a), (PODS[1], b)])
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        for pod, seen in ((PODS[0], seen_a), (PODS[1], seen_b)):
            ids = [h.get("X-Request-ID") for method, path, h in seen
                   if path == "/api/management/v1/catalogs"]
            self.assertEqual(ids, [f"{PREFIX}-{pod}-1", f"{PREFIX}-{pod}-2"])
            for method, path, h in seen:
                if path == "/api/management/v1/catalogs":
                    self.assertEqual(h["Authorization"], f"Bearer {TOKEN}")
        self.assertNotIn(CREDS["ROOT_CLIENT_SECRET"], done.stdout + done.stderr)
        self.assertNotIn(TOKEN, done.stdout + done.stderr)

    def test_a_pod_that_refuses_fails_the_script(self):
        (a, _), (b, _) = self.replica(), self.replica(signing_key="other-key")
        done = self.run_script(a, [(PODS[0], a), (PODS[1], b)])
        self.assertEqual(done.returncode, 1)
        self.assertIn(f"FAIL {PREFIX}-{PODS[1]}-1", done.stdout)


class BatchRun(unittest.TestCase):
    def test_now_is_past_the_hour_the_traffic_ended_in(self):
        for t, now in (("2026-10-11T13:00:00+00:00", "2026-10-11T14:01:01Z"),
                       ("2026-10-11T13:59:59+00:00", "2026-10-11T14:01:01Z"),
                       ("2026-10-11T23:30:00+00:00", "2026-10-12T00:01:01Z")):
            with self.subTest(t=t):
                self.assertEqual(gate.batch_now(dt.datetime.fromisoformat(t)), now)

    def test_the_batch_publishes_the_hour_of_the_traffic_at_that_now(self):
        with tempfile.TemporaryDirectory() as d:
            t = dt.datetime(2026, 10, 11, 12, 59, 59, tzinfo=dt.timezone.utc)
            events = batch_outputs(d, {PODS[0]: [polaris_line(PODS[0], 7)]},
                                   now=gate.batch_now(t))
            published = [e["hour"] for e in events if e["event"] == "published"]
            self.assertEqual(published, ["20261011-12"])

    def test_the_job_is_the_cronjobs_own_template_with_now_added(self):
        cronjob = {"apiVersion": "batch/v1", "kind": "CronJob",
                   "metadata": {"name": "polaris-log-batch", "namespace": "catalog-polaris",
                                "uid": "u-1", "resourceVersion": "9"},
                   "spec": {"schedule": "3 * * * *", "jobTemplate": {
                       "metadata": {"labels": {"app.kubernetes.io/name": "polaris-log-batch"}},
                       "spec": {"backoffLimit": 0, "template": {"spec": {
                           "serviceAccountName": "polaris-log-batch",
                           "containers": [{"name": "batch", "image": "lakehouse/b:1"}],
                           "volumes": [{"name": "polaris-logs"}]}}}}}}
        job = gate.batch_job(cronjob, "polaris-log-batch-gate-1", NOW)
        self.assertEqual((job["apiVersion"], job["kind"]), ("batch/v1", "Job"))
        self.assertEqual(job["metadata"], {
            "name": "polaris-log-batch-gate-1", "namespace": "catalog-polaris",
            "labels": {"app.kubernetes.io/name": "polaris-log-batch"}})
        pod = job["spec"]["template"]["spec"]
        self.assertEqual(pod["containers"][0]["args"], ["--now", NOW])
        self.assertEqual(pod["serviceAccountName"], "polaris-log-batch")
        self.assertEqual(pod["volumes"], [{"name": "polaris-logs"}])
        self.assertEqual(job["spec"]["backoffLimit"], 0)
        self.assertNotIn("args", cronjob["spec"]["jobTemplate"]["spec"]["template"]["spec"]
                         ["containers"][0], "the CronJob read back is changed")

    def test_a_job_is_done_once_it_succeeded_or_failed(self):
        self.assertIsNone(gate.job_outcome({"status": {"active": 1}}))
        self.assertEqual(gate.job_outcome({"status": {"succeeded": 1}}), "succeeded")
        self.assertEqual(gate.job_outcome({"status": {"failed": 1}}), "failed")

    def test_the_batchs_events_are_its_json_lines(self):
        self.assertEqual(gate.batch_events('{"event":"idle"}\nTraceback\n\n'),
                         [{"event": "idle"}])


class Listing(unittest.TestCase):
    def test_each_output_file_with_its_hash_and_lines_and_nothing_else(self):
        with tempfile.TemporaryDirectory() as d:
            for sub in ("processed-logs", "aggregated-logs", "done", ".state"):
                os.makedirs(os.path.join(d, sub))
            for rel, text in (("processed-logs/20261011-12.jsonl", "{}\n{}\n"),
                              ("processed-logs/20261011-11.jsonl", ""),
                              ("aggregated-logs/20261011-12.jsonl", "{}\n"),
                              ("done/x.log", "{}\n"), (".state/checkpoint.json", "{}"),
                              ("polaris-a.log", "{}\n")):
                with open(os.path.join(d, rel), "w", encoding="utf-8") as f:
                    f.write(text)
            found = listing(d)
        self.assertEqual(sorted(found), ["aggregated-logs/20261011-12.jsonl",
                                         "processed-logs/20261011-11.jsonl",
                                         "processed-logs/20261011-12.jsonl"])
        self.assertEqual(found["processed-logs/20261011-12.jsonl"]["lines"], 2)
        self.assertEqual(found["processed-logs/20261011-11.jsonl"]["lines"], 0)
        self.assertRegex(found["aggregated-logs/20261011-12.jsonl"]["sha256"], r"^[0-9a-f]{64}$")

    def test_no_output_yet_is_an_empty_listing(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(listing(d), {})

    def test_the_listing_pod_mounts_the_claim_read_only(self):
        argv = gate.logs_listing_command(ENV, "curl:1@sha256:" + "e" * 64, "logs-list")
        self.assertEqual(argv[:7], ["kubectl", "--context", gate.CONTEXT, "-n",
                                    "catalog-polaris", "run", "logs-list"])
        spec = json.loads(argv[argv.index("--overrides") + 1])["spec"]
        self.assertEqual(spec["volumes"], [{"name": "polaris-logs", "persistentVolumeClaim": {
            "claimName": "polaris-logs", "readOnly": True}}])
        self.assertEqual(spec["containers"][0]["volumeMounts"], [
            {"name": "polaris-logs", "mountPath": "/logs", "readOnly": True}])
        self.assertEqual(argv[argv.index("--") + 1:], ["sh", "-s", "--", "/logs"])


class FluentBit(unittest.TestCase):
    def test_the_stand_in_reads_both_directories_from_the_start_read_only(self):
        pod = gate.fluent_bit_pod(ENV, "fluent/fluent-bit:1@sha256:" + "f" * 64, "fb-1")
        self.assertEqual(pod["metadata"]["namespace"], "catalog-polaris")
        spec = pod["spec"]
        self.assertEqual(spec["restartPolicy"], "Never")
        self.assertEqual(spec["volumes"], [{"name": "polaris-logs", "persistentVolumeClaim": {
            "claimName": "polaris-logs", "readOnly": True}}])
        (container,) = spec["containers"]
        self.assertEqual(container["volumeMounts"], [
            {"name": "polaris-logs", "mountPath": "/logs", "readOnly": True}])
        self.assertTrue(container["securityContext"]["readOnlyRootFilesystem"])
        self.assertNotIn(spec["securityContext"]["runAsUser"], (0, 10000),
                         "the reader is neither root nor the user Polaris and the batch run as")
        args = " ".join(container["command"] + container["args"])
        for part in ("-i tail", "path=/logs/processed-logs/*.jsonl,/logs/aggregated-logs/*.jsonl",
                     "read_from_head=true", "exit_on_eof=true", "parser=json",
                     "path_key=source_file", "-o stdout", "format=json_lines",
                     "json_date_key=false"):
            self.assertIn(part, args)

    def test_its_records_are_the_json_lines_of_its_output(self):
        text = ("Fluent Bit v5.1.2\n[info] [engine] started\n"
                '{"a":1,"source_file":"/logs/processed-logs/x.jsonl"}\n')
        self.assertEqual(gate.fluent_bit_records(text),
                         [{"a": 1, "source_file": "/logs/processed-logs/x.jsonl"}])


class Problems(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.f = Fixture()

    @classmethod
    def tearDownClass(cls):
        cls.f.dir.cleanup()

    def test_the_real_batchs_output_read_as_the_reader_does_passes(self):
        problems, notes = self.f.problems()
        self.assertEqual(problems, [])
        text = "\n".join(notes)
        self.assertIn("4 request IDs", text)
        for pod in PODS:
            self.assertIn(pod, text)
        self.assertIn("contract v1", text)
        self.assertIn("second run", text)

    def test_a_request_id_missing_or_read_twice_fails(self):
        rid = sorted(self.f.sent)[0]
        records = [r for r in self.f.records if r.get("mdc", {}).get("requestId") != rid]
        problems, _ = self.f.problems(records=records)
        self.assertIn(rid, "\n".join(problems))
        twice = [r for r in self.f.records if r.get("mdc", {}).get("requestId") == rid]
        problems, _ = self.f.problems(records=self.f.records + twice)
        self.assertIn(rid, "\n".join(problems))

    def test_a_request_id_from_another_pod_fails(self):
        rid, pod = sorted(self.f.sent.items())[0]
        records = [dict(r, hostName=PODS[1] if pod == PODS[0] else PODS[0])
                   if r.get("mdc", {}).get("requestId") == rid else r for r in self.f.records]
        problems, _ = self.f.problems(records=records)
        self.assertIn(rid, "\n".join(problems))

    def test_a_line_the_contract_refuses_fails(self):
        records = [dict(self.f.records[0], event_id="not-a-sha1")] + self.f.records[1:]
        problems, _ = self.f.problems(records=records)
        self.assertIn("event_id", "\n".join(problems))

    def test_fewer_records_than_lines_fails(self):
        summary = [r for r in self.f.records if "aggregated-logs" in r["source_file"]]
        problems, _ = self.f.problems(records=[r for r in self.f.records if r not in summary])
        self.assertIn("aggregated-logs/", "\n".join(problems))

    def test_a_pod_whose_file_was_not_read_fails(self):
        problems, _ = self.f.problems(pods=PODS + ["polaris-6d9f-ccccc"])
        self.assertIn("polaris-6d9f-ccccc", "\n".join(problems))

    def test_a_second_run_that_changes_a_file_or_publishes_fails(self):
        path = sorted(self.f.after)[0]
        after = {**self.f.after, path: {**self.f.after[path], "sha256": "0" * 64}}
        problems, _ = self.f.problems(after=after)
        self.assertIn(path, "\n".join(problems))
        problems, _ = self.f.problems(events=[self.f.events[0], self.f.events[0]])
        self.assertIn("second run", "\n".join(problems))

    def test_a_first_run_that_published_nothing_fails(self):
        skipped = [{"event": "skipped", "reason": "another run holds the lock"}]
        problems, _ = self.f.problems(events=[skipped, self.f.events[1]])
        self.assertIn("another run holds the lock", "\n".join(problems))

    def test_an_hour_without_its_summary_fails(self):
        after = {k: v for k, v in self.f.after.items() if not k.startswith("aggregated-logs/")}
        problems, _ = self.f.problems(before=after, after=after, records=[
            r for r in self.f.records if "aggregated-logs" not in r["source_file"]])
        self.assertIn("aggregated-logs/", "\n".join(problems))


if __name__ == "__main__":
    unittest.main()
