"""Tests for metrics.py and the scrape contract it loads (ADR-046 §3, §5).
Run: python3 -m unittest discover -s gates"""

import json
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import gate  # noqa: E402
import metrics  # noqa: E402
import network  # noqa: E402

ENV = {"NS_CATALOG_POLARIS": "catalog-polaris", "NS_STANDINS": "standins"}
PYTHON = "python:3.12.14-slim@sha256:" + "f" * 64
PODS = {"polaris": ["polaris-a", "polaris-b"],
        "polaris-pg": ["polaris-pg-1", "polaris-pg-2", "polaris-pg-3"]}


def contract():
    with open(metrics.CONTRACT, encoding="utf-8") as f:
        return f.read()


def sample(name, job, pod, value):
    return {"metric": {"__name__": name, "job": job, "pod": pod,
                       "namespace": "catalog-polaris"}, "value": [1760000000, value]}


ZGC = ("ZGC Young Generation", "ZGC Old Generation")
PARALLEL = ("PS Eden Space", "PS Survivor Space", "PS Old Gen")


def pools(pod, ids):
    """jvm_memory_used_bytes{area="heap"} of one Polaris pod, one sample per pool."""
    found = []
    for pool in ids:
        s = sample("jvm_memory_used_bytes", "polaris", pod, "1048576")
        s["metric"].update({"area": "heap", "id": pool})
        found.append(s)
    return found


def answer(up=None, http=None, cnpg=None, heap=None, targets=()):
    up = up if up is not None else [sample("up", job, pod, "1")
                                    for job, pods in PODS.items() for pod in pods]
    http = http if http is not None else [sample("http_server_requests_seconds_count",
                                                 "polaris", "polaris-a", "12")]
    cnpg = cnpg if cnpg is not None else [sample("cnpg_collector_up", "polaris-pg", pod, "1")
                                          for pod in PODS["polaris-pg"]]
    heap = heap if heap is not None else [s for pod in PODS["polaris"] for s in pools(pod, ZGC)]
    return {"answers": {"up": up, "http": http, "cnpg": cnpg, "heap": heap},
            "targets": list(targets)}


class Contract(unittest.TestCase):
    """§3: the fragment handed to the observability team is the one the gate loads."""

    def test_its_only_placeholder_is_polaris_namespace(self):
        text = contract()
        self.assertIn(metrics.PLACEHOLDER, text)
        self.assertEqual(text.count("${"), text.count(metrics.PLACEHOLDER))

    def test_one_job_per_target_of_adr_046(self):
        text = contract()
        self.assertTrue(text.startswith("scrape_configs:") or "\nscrape_configs:" in text)
        for job, path, port in (("polaris", "/q/metrics", "polaris-mgmt"),
                                ("polaris-pg", "/metrics", "metrics")):
            with self.subTest(job=job):
                self.assertIn(f"- job_name: {job}\n", text)
                self.assertIn(f"metrics_path: {path}\n", text)
                self.assertIn(f"regex: {port}\n", text)

    def test_it_selects_pods_by_the_labels_adr_046_names(self):
        text = contract()
        self.assertIn("__meta_kubernetes_pod_label_app_kubernetes_io_name", text)
        self.assertIn("__meta_kubernetes_pod_label_app_kubernetes_io_instance", text)
        self.assertIn("__meta_kubernetes_pod_label_cnpg_io_cluster", text)
        self.assertIn("__meta_kubernetes_pod_label_cnpg_io_podRole", text)
        self.assertIn("target_label: pod\n", text)

    def test_the_markdown_names_the_same_ports_paths_and_series(self):
        with open(os.path.join(os.path.dirname(metrics.CONTRACT), "polaris-metrics.md"),
                  encoding="utf-8") as f:
            text = f.read()
        for word in ("8182", "/q/metrics", "9187", "/metrics", "METRICS_NAMESPACE_LABEL",
                     "polaris-prometheus-scrape.yaml", *metrics.SERIES):
            with self.subTest(word=word):
                self.assertIn(word, text)


class Config(unittest.TestCase):
    def test_the_contract_with_the_namespace_filled_in_and_a_short_interval(self):
        text = metrics.prometheus_config(contract(), ENV)
        self.assertNotIn("${", text)
        self.assertIn('"catalog-polaris"', text)
        self.assertTrue(text.startswith("global:\n"))
        self.assertIn(f"scrape_interval: {metrics.SCRAPE_INTERVAL}\n", text)
        self.assertTrue(text.endswith(contract().replace(metrics.PLACEHOLDER,
                                                         "catalog-polaris")))

    def test_another_placeholder_is_refused(self):
        with self.assertRaises(ValueError):
            metrics.prometheus_config("scrape_configs: ${OTHER}\n", ENV)


class Query(unittest.TestCase):
    def test_a_short_lived_check_pod_in_polaris_namespace(self):
        argv = metrics.query_command(ENV, PYTHON, "check-metrics-1", 5)
        self.assertEqual(argv[:7], ["kubectl", "--context", gate.CONTEXT, "-n",
                                    "catalog-polaris", "run", "check-metrics-1"])
        for flag in (f"--image={PYTHON}", "--restart=Never", "--rm", "-i",
                     f"--labels={network.CHECK_LABEL}=true"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[-3:], ["http://prometheus.standins.svc.cluster.local:9090",
                                     str(metrics.TIMEOUT), "5"])

    def test_the_script_is_python_that_compiles(self):
        compile(metrics.QUERY, "query", "exec")

    def test_the_script_asks_each_query_and_the_targets(self):
        self.assertIn(json.dumps(metrics.QUERIES), metrics.QUERY)
        self.assertIn("/api/v1/query", metrics.QUERY)
        self.assertIn("/api/v1/targets", metrics.QUERY)

    def test_the_answer_is_the_last_json_line(self):
        out = "noise\n" + json.dumps(answer()) + "\n"
        self.assertEqual(metrics.parse_answer(out), answer())
        self.assertIsNone(metrics.parse_answer("no json here\n"))


class Problems(unittest.TestCase):
    """§5: every running pod scraped, and each required series present."""

    def test_everything_scraped_is_no_problem(self):
        problems, notes = metrics.problems(answer(), PODS)
        self.assertEqual(problems, [])
        self.assertTrue(any("polaris-pg-3" in n for n in notes))

    def test_a_pod_down_or_missing_is_a_problem(self):
        up = [sample("up", "polaris", "polaris-a", "1"), sample("up", "polaris", "polaris-b", "0")]
        up += [sample("up", "polaris-pg", p, "1") for p in PODS["polaris-pg"][:2]]
        problems, _ = metrics.problems(answer(up=up), PODS)
        self.assertEqual(len(problems), 2)
        self.assertTrue(any("polaris-b" in p for p in problems))
        self.assertTrue(any("polaris-pg-3" in p for p in problems))

    def test_no_request_counted_is_a_problem(self):
        zero = [sample("http_server_requests_seconds_count", "polaris", "polaris-a", "0")]
        for http in ([], zero):
            with self.subTest(http=http):
                problems, _ = metrics.problems(answer(http=http), PODS)
                self.assertEqual(len(problems), 1)
                self.assertIn("http_server_requests_seconds_count", problems[0])

    def test_an_instance_without_its_collector_up_is_a_problem(self):
        cnpg = [sample("cnpg_collector_up", "polaris-pg", "polaris-pg-1", "1"),
                sample("cnpg_collector_up", "polaris-pg", "polaris-pg-2", "0")]
        problems, _ = metrics.problems(answer(cnpg=cnpg), PODS)
        self.assertEqual(len(problems), 2)
        self.assertTrue(all("cnpg_collector_up" in p for p in problems))

    def test_a_polaris_pod_on_another_collector_is_a_problem(self):
        heap = pools("polaris-a", ZGC) + pools("polaris-b", PARALLEL)
        problems, _ = metrics.problems(answer(heap=heap), PODS)
        self.assertEqual(len(problems), 1)
        self.assertIn("polaris-b", problems[0])
        self.assertIn("PS Old Gen", problems[0])

    def test_a_polaris_pod_without_heap_pools_is_a_problem(self):
        problems, _ = metrics.problems(answer(heap=pools("polaris-a", ZGC)), PODS)
        self.assertEqual(len(problems), 1)
        self.assertIn("polaris-b", problems[0])

    def test_the_expected_pools_are_the_collector_in_values_lakehouse(self):
        self.assertEqual(metrics.java_opts(), "-XX:MaxRAMPercentage=70.0 -XX:+UseZGC "
                         "-XX:+ZGenerational -XX:+ExitOnOutOfMemoryError")
        self.assertEqual(metrics.heap_pools(metrics.java_opts()), set(ZGC))

    def test_a_collector_without_known_pools_is_refused(self):
        for opts in ("-XX:+UseParallelGC", "-XX:+UseZGC", ""):
            with self.subTest(opts=opts), self.assertRaises(ValueError):
                metrics.heap_pools(opts)

    def test_values_without_java_opts_are_refused(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write("extraEnv:\n  - name: TZ\n    value: UTC\n")
        try:
            with self.assertRaises(ValueError):
                metrics.java_opts(f.name)
        finally:
            os.unlink(f.name)

    def test_no_running_pod_for_a_job_is_a_problem(self):
        problems, _ = metrics.problems(answer(), {"polaris": PODS["polaris"], "polaris-pg": []})
        self.assertEqual(problems, ["no running polaris-pg pod to scrape"])

    def test_no_answer_is_a_problem(self):
        problems, _ = metrics.problems(None, PODS)
        self.assertEqual(len(problems), 1)

    def test_on_a_problem_the_targets_are_listed_with_their_last_error(self):
        targets = [{"job": "polaris", "pod": "polaris-b", "health": "down",
                    "lastError": "connection refused"}]
        _, notes = metrics.problems(answer(up=[], targets=targets), PODS)
        self.assertIn("target polaris polaris-b down: connection refused", notes)


class CheckMetrics(unittest.TestCase):
    """gate.check_metrics: the running pods of each job, then the query for all of them."""

    def test_asks_for_every_running_pod_and_judges_the_answer(self):
        from unittest import mock

        def pods(*names, phase="Running"):
            return json.dumps({"items": [{"metadata": {"name": n}, "status": {"phase": phase}}
                                         for n in names]})

        listed = {metrics.JOBS["polaris"]: pods("polaris-a", "polaris-b"),
                  metrics.JOBS["polaris-pg"]: pods(*PODS["polaris-pg"])}
        ran = []

        def run(argv, **kwargs):
            ran.append((argv, kwargs.get("input")))
            return subprocess.CompletedProcess(argv, 0, json.dumps(answer()) + "\n", "")

        def output(argv):
            return listed[argv[argv.index("-l") + 1]]

        with mock.patch.object(gate, "output", side_effect=output), \
                mock.patch.object(gate.subprocess, "run", side_effect=run):
            problems, notes = gate.check_metrics({**ENV, "KUBE_CONTEXT": gate.CONTEXT},
                                                 gate.load_pins())
        self.assertEqual(problems, [])
        ((argv, stdin),) = ran
        self.assertEqual(stdin, metrics.QUERY)
        self.assertEqual(argv[-1], "5")
        self.assertIn("--labels=lakehouse-gate/check=true", argv)


class Pins(unittest.TestCase):
    def test_prometheus_is_pinned_by_digest(self):
        image = gate.load_pins()["standin_images"]["prometheus"]["image"]
        self.assertRegex(image, r"^prom/prometheus:v\d+\.\d+\.\d+@sha256:[0-9a-f]{64}$")


class Script(unittest.TestCase):
    def test_the_script_against_a_fake_prometheus(self):
        """QUERY run for real against a local HTTP server that answers as Prometheus does."""
        import http.server
        import threading

        class Fake(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path.startswith("/api/v1/targets"):
                    body = {"status": "success", "data": {"activeTargets": [
                        {"labels": {"job": "polaris", "pod": "polaris-a"}, "health": "up",
                         "lastError": ""}]}}
                else:
                    body = {"status": "success", "data": {"resultType": "vector", "result": [
                        sample("up", "polaris", "polaris-a", "1")]}}
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Fake)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            url = f"http://127.0.0.1:{server.server_port}"
            out = subprocess.run([sys.executable, "-", url, "2", "1"], input=metrics.QUERY,
                                 capture_output=True, text=True, timeout=30, check=True).stdout
        finally:
            server.shutdown()
            server.server_close()
        got = metrics.parse_answer(out)
        self.assertEqual(set(got["answers"]), set(metrics.QUERIES))
        self.assertEqual(got["targets"], [{"job": "polaris", "pod": "polaris-a",
                                           "health": "up", "lastError": ""}])


if __name__ == "__main__":
    unittest.main()
