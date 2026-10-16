"""Tests for stress/driver.py: the stress run's arithmetic and the commands it sends, with no
cluster. Run: python3 -m unittest discover -s gates"""

import contextlib
import importlib.util
import io
import json
import os
import re
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))


def load(name, path):
    """The module at path, under name: gates/ holds another driver.py (failover/) and seed.py
    (ADR-051, imported by gate.py), and unittest discover imports every test module into one
    process, so a plain `import driver` or `import seed` may get the other one."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


driver = load("stress_driver", os.path.join(HERE, "stress", "driver.py"))

ENV = {"NS_CATALOG_POLARIS": "catalog-polaris", "NS_STANDINS": "standins"}
PYTHON = "python:3@sha256:" + "b" * 64
CONFIG = {
    "profiles": {"mixed": {"shares": {"conditional_load": 0.9, "commit": 0.1},
                           "start": 100, "step": 50, "max_steps": 4}},
    "pass": {"answered_min": 0.95, "error_max": 0.001,
             "p99_ms": {"conditional_load": 200, "commit": 1000}},
    "throttle_max": 0.05,
    "hold": {"profile": "mixed", "fraction": 0.7, "seconds": 600},
    "k6": {"cpu": "2"},
}


def counts(**by_class):
    return {c: by_class.get(c, 0) for c in driver.CLASSES}


def summary(rate=100, seconds=10, dropped=0, loads=None, commits=None, hot=None, lat=None,
            ops=None):
    lat = lat or {}
    return {
        "profile": "mixed", "rate": rate, "seconds": seconds, "offered": rate * seconds,
        "dropped": dropped,
        "requests": {"conditional_load": loads or counts(**{"304": 800, "2xx": 100}),
                     "commit": commits or counts(**{"2xx": 100}),
                     "commit_hot": hot or counts(**{"2xx": 20, "409": 30})},
        "latency_ms": {"conditional_load": {"p50": 5, "p95": 20, "p99": lat.get("load", 50),
                                            "max": 90, "count": 900},
                       "commit": {"p50": 50, "p95": 200, "p99": lat.get("commit", 400),
                                  "max": 900, "count": 100}},
        "commits": ops or {"commit": {"ok": 100, "conflict": 0, "unknown": 0, "failed": 0},
                           "commit_hot": {"ok": 20, "conflict": 3, "unknown": 0,
                                          "failed": 0}},
    }


def cadvisor(rows):
    """cAdvisor's text: rows of (namespace, pod, container, usage, periods, throttled)."""
    lines = ["# HELP container_cpu_usage_seconds_total x", "# TYPE x counter"]
    for ns, pod, container, usage, periods, throttled in rows:
        labels = f'container="{container}",id="/x",namespace="{ns}",pod="{pod}"'
        lines += [f"container_cpu_usage_seconds_total{{{labels}}} {usage} 1700000000000",
                  f"container_cpu_cfs_periods_total{{{labels}}} {periods} 1700000000000",
                  f"container_cpu_cfs_throttled_periods_total{{{labels}}} {throttled}"]
    lines.append('container_cpu_usage_seconds_total{container="",namespace="standins",'
                 'pod="opa-1"} 999')
    return "\n".join(lines) + "\n"


class Config(unittest.TestCase):
    def setUp(self):
        self.config = driver.load_config()

    def test_the_four_profiles_of_adr_053_are_there_and_each_mix_sums_to_one(self):
        self.assertEqual(driver.profiles(self.config),
                         ["commits", "mixed", "queries-conditional", "queries-full"])
        for name in driver.profiles(self.config):
            profile = self.config["profiles"][name]
            with self.subTest(profile=name):
                self.assertAlmostEqual(sum(profile["shares"].values()), 1.0)
                self.assertLessEqual(set(profile["shares"]), set(driver.KINDS))
                self.assertNotIn("commit_hot", profile["shares"])

    def test_the_mixed_profile_is_adr_053s_assumed_shape(self):
        shares = self.config["profiles"]["mixed"]["shares"]
        self.assertAlmostEqual(shares["conditional_load"], 0.85)
        self.assertAlmostEqual(shares["commit"], 0.05)
        others = sum(v for k, v in shares.items() if k not in ("conditional_load", "commit"))
        self.assertAlmostEqual(others, 0.10)

    def test_commits_and_queries_profiles_send_only_their_kind_and_small_requests(self):
        profiles = self.config["profiles"]
        self.assertEqual(profiles["commits"]["shares"], {"commit": 1.0})
        self.assertNotIn("full_load", profiles["queries-conditional"]["shares"])
        self.assertNotIn("conditional_load", profiles["queries-full"]["shares"])
        for name in ("queries-conditional", "queries-full"):
            self.assertNotIn("commit", profiles[name]["shares"])

    def test_the_hot_set_is_10_tables_with_4_writers_and_a_commit_retries_4_times(self):
        self.assertEqual(self.config["hot"]["tables"], 10)
        self.assertEqual(self.config["hot"]["writers_per_table"], 4)
        self.assertEqual(self.config["commit_retries"], 4)

    def test_the_step_rules_and_the_hold_are_adr_053s(self):
        self.assertEqual(self.config["pass"]["answered_min"], 0.95)
        self.assertEqual(self.config["pass"]["error_max"], 0.001)
        self.assertEqual(self.config["hold"], {**self.config["hold"], "profile": "mixed",
                                               "fraction": 0.7, "seconds": 600})
        self.assertEqual(self.config["k6"]["cpu"], "2")

    def test_the_scenario_knows_every_kind_and_reads_the_config(self):
        with open(os.path.join(HERE, "stress", "scenario.js"), encoding="utf-8") as f:
            script = f.read()
        for kind in driver.KINDS:
            self.assertIn(f"'{kind}'", script)
        self.assertIn("/stress/config.json", script)
        self.assertIn("responseType", script)
        self.assertIn("null, h, 'none')", script, "loads read no body")


class Ramp(unittest.TestCase):
    def test_rates_rise_by_the_step_up_to_max_steps(self):
        self.assertEqual(driver.rates(CONFIG, "mixed"), [100, 150, 200, 250])

    def test_an_unknown_profile_is_refused(self):
        with self.assertRaises(ValueError):
            driver.rates(CONFIG, "nope")


class StepResult(unittest.TestCase):
    def test_a_clean_step_passes_and_409s_on_the_hot_set_are_not_errors(self):
        result = driver.step_result(summary(), CONFIG)
        self.assertTrue(result["passed"], result["reasons"])
        self.assertEqual((result["errors"], result["server_errors"]), (0, 0))
        self.assertEqual(result["answered"], 1.0)
        self.assertEqual(result["p99_ms"], {"conditional_load": 50, "commit": 400})

    def test_more_than_5_percent_dropped_fails(self):
        result = driver.step_result(summary(dropped=51), CONFIG)
        self.assertFalse(result["passed"])
        self.assertIn("answered 94.9%", " ".join(result["reasons"]))

    def test_more_than_a_tenth_of_a_percent_errors_fails_and_5xx_are_counted(self):
        result = driver.step_result(summary(loads=counts(**{"304": 800, "5xx": 2})), CONFIG)
        self.assertFalse(result["passed"])
        self.assertEqual((result["errors"], result["server_errors"]), (2, 2))

    def test_an_ordinary_commit_that_ran_out_of_retries_is_an_error(self):
        ops = {"commit": {"ok": 99, "conflict": 1, "unknown": 0, "failed": 0},
               "commit_hot": {"ok": 1, "conflict": 9, "unknown": 0, "failed": 0}}
        result = driver.step_result(summary(ops=ops), CONFIG)
        self.assertEqual(result["errors"], 1)

    def test_a_p99_over_its_cap_fails(self):
        result = driver.step_result(summary(lat={"commit": 1500}), CONFIG)
        self.assertFalse(result["passed"])
        self.assertIn("commit p99 1500 ms > 1000 ms", result["reasons"])


class Knee(unittest.TestCase):
    def test_the_knee_is_the_last_step_that_passed(self):
        steps = [{"rate": 100, "passed": True}, {"rate": 150, "passed": True},
                 {"rate": 200, "passed": False}]
        self.assertEqual(driver.knee(steps), {"knee": 150, "deciding": 2, "reached": True})

    def test_a_first_step_that_fails_gives_no_knee(self):
        self.assertEqual(driver.knee([{"rate": 100, "passed": False}]),
                         {"knee": 0, "deciding": 0, "reached": True})

    def test_a_ramp_that_never_failed_did_not_reach_its_knee(self):
        steps = [{"rate": 100, "passed": True}, {"rate": 150, "passed": True}]
        self.assertEqual(driver.knee(steps), {"knee": 150, "deciding": 1, "reached": False})


class Throttle(unittest.TestCase):
    TARGETS = {"k6": ("catalog-polaris", "stress-k6"), "opa": ("standins", "opa"),
               "seaweedfs": ("standins", "seaweedfs")}

    def test_cpu_is_summed_per_container_and_the_pod_cgroup_is_left_out(self):
        text = cadvisor([("standins", "opa-1", "opa", 10.5, 100, 4),
                         ("standins", "opa-2", "opa", 1.5, 50, 1),
                         ("catalog-polaris", "stress-k6", "stress-k6", 3, 10, 0)])
        cpu = driver.container_cpu([text], self.TARGETS)
        self.assertEqual(cpu["opa"], {"usage": 12.0, "periods": 150, "throttled": 5})
        self.assertEqual(cpu["seaweedfs"], {"usage": 0, "periods": 0, "throttled": 0})

    def test_a_component_throttled_past_the_maximum_over_a_step_is_named(self):
        before = driver.container_cpu([cadvisor([
            ("standins", "opa-1", "opa", 0, 100, 0),
            ("catalog-polaris", "stress-k6", "stress-k6", 0, 100, 0)])], self.TARGETS)
        after = driver.container_cpu([cadvisor([
            ("standins", "opa-1", "opa", 5, 200, 20),
            ("catalog-polaris", "stress-k6", "stress-k6", 5, 200, 2)])], self.TARGETS)
        ratios, over = driver.throttle(before, after, 0.05)
        self.assertEqual(ratios, {"k6": 0.02, "opa": 0.2, "seaweedfs": 0.0})
        self.assertEqual(over, ["opa"])

    def test_a_knee_decided_while_a_component_was_throttled_is_that_components(self):
        steps = [{"rate": 100, "passed": True, "throttled": []},
                 {"rate": 150, "passed": False, "throttled": ["k6"]}]
        verdict = driver.verdict(steps)
        self.assertEqual((verdict["knee"], verdict["limited_by"], verdict["valid"]),
                         (100, ["k6"], False))
        steps[1]["throttled"] = []
        self.assertTrue(driver.verdict(steps)["valid"])


class Metrics(unittest.TestCase):
    def test_labels_with_escapes_and_timestamps_are_read(self):
        text = ('# TYPE m counter\nm{a="x\\"y",b="2"} 3.5 1700000000000\nn 4\n'
                'm{a="z"} NaN\n')
        series = driver.parse_metrics(text)
        self.assertEqual(series[("m", (("a", 'x"y'), ("b", "2")))], 3.5)
        self.assertEqual(series[("n", ())], 4.0)

    def test_polaris_requests_over_a_step_are_counted_per_endpoint_and_status(self):
        def text(count, total):
            return ('http_server_requests_seconds_count{method="GET",outcome="SUCCESS",'
                    f'status="304",uri="/api/catalog/v1/{{prefix}}/t"}} {count}\n'
                    'http_server_requests_seconds_sum{method="GET",outcome="SUCCESS",'
                    f'status="304",uri="/api/catalog/v1/{{prefix}}/t"}} {total}\n')
        delta = driver.server_requests([text(10, 1.0), text(5, 0.5)],
                                       [text(110, 2.0), text(5, 0.5)])
        self.assertEqual(delta, {"GET /api/catalog/v1/{prefix}/t 304":
                                 {"count": 100, "mean_ms": 10.0}})


class Commits(unittest.TestCase):
    CONSOLE = ('time="2026-10-14T10:00:00Z" level=info msg="C ok ns01.t02 123" '
               'source=console\n'
               'time="2026-10-14T10:00:00Z" level=info msg="C unknown ns01.t02 124" '
               'source=console\n'
               'time="2026-10-14T10:00:00Z" level=info msg="C conflict ns00.t00 125" '
               'source=console\n'
               'time="2026-10-14T10:00:00Z" level=warning msg="something else"\n')

    def test_the_scenarios_commit_lines_are_read_from_its_console(self):
        self.assertEqual(driver.commit_records(self.CONSOLE),
                         [("ok", "ns01.t02", "123"), ("unknown", "ns01.t02", "124"),
                          ("conflict", "ns00.t00", "125")])

    def found(self, **tables):
        return {name.replace("_", "."): {"snapshots": 9, "new": new, "duplicates": dup}
                for name, (new, dup) in tables.items()}

    def test_every_commit_answered_200_is_there_once_and_an_unknown_one_may_be(self):
        records = driver.commit_records(self.CONSOLE)
        problems, tally = driver.accounting(records, self.found(ns01_t02=(["123"], []),
                                                                ns00_t00=([], [])))
        self.assertEqual(problems, [])
        self.assertEqual(tally, {"ok": 1, "unknown": 1, "found": 1})
        problems, _ = driver.accounting(records, self.found(ns01_t02=(["123", "124"], []),
                                                            ns00_t00=([], [])))
        self.assertEqual(problems, [])

    def test_a_lost_commit_a_stray_snapshot_and_a_duplicate_each_fail(self):
        records = driver.commit_records(self.CONSOLE)
        problems, _ = driver.accounting(records, self.found(ns01_t02=([], ["7"]),
                                                            ns00_t00=(["125"], [])))
        text = "\n".join(problems)
        self.assertIn("ns01.t02: commit 123 answered 200 but is missing", text)
        self.assertIn("ns00.t00: snapshot 125 was not answered 200", text)
        self.assertIn("ns01.t02: snapshot 7 is listed twice", text)


class Correctness(unittest.TestCase):
    def step(self, rate, passed, errors=0, server=0):
        return {"rate": rate, "passed": passed, "errors": errors, "server_errors": server}

    def test_errors_at_or_below_the_knee_and_in_the_hold_fail_the_run(self):
        profiles = {"mixed": [self.step(100, True, errors=1), self.step(150, False)]}
        problems = driver.correctness(profiles, {"errors": 1, "server_errors": 1})
        self.assertEqual(len(problems), 2)
        self.assertIn("mixed at 100 req/s: 1 failed request", problems[0])
        self.assertIn("hold: 1 failed request(s), 1 of them 5xx", problems[1])

    def test_the_step_past_the_knee_may_fail_it_is_what_ends_the_ramp(self):
        profiles = {"mixed": [self.step(100, True), self.step(150, False, 40, 40)]}
        self.assertEqual(driver.correctness(profiles, None), [])

    def test_a_restart_of_polaris_or_polaris_pg_fails_the_run(self):
        pods = {"items": [{"metadata": {"name": "polaris-1"}, "status": {
            "containerStatuses": [{"name": "polaris", "restartCount": 1}]}}]}
        after = driver.restarts(pods)
        self.assertEqual(after, {"polaris-1": 1})
        self.assertEqual(driver.restart_problems({"polaris-1": 1}, after), [])
        self.assertEqual(driver.restart_problems({"polaris-1": 0}, after),
                         ["polaris-1 restarted 1 time(s) during the run"])
        self.assertEqual(driver.restart_problems({"polaris-0": 0}, after),
                         ["polaris-0 is gone, polaris-1 is new: a pod was replaced"])


SINCE = "2026-10-16T12:00:00Z"


def event(pod, message, last, count=1, reason="Unhealthy"):
    return {"involvedObject": {"kind": "Pod", "name": pod}, "reason": reason,
            "message": message, "count": count, "lastTimestamp": last}


class PolarisPg(unittest.TestCase):
    """What polaris-pg did under load: its primary and its readiness probe's failures, reported
    and not judged (ADR-054 §2), for the readiness probe's tuning (ADR-047 §8)."""

    def test_the_seed_records_the_primary_and_when(self):
        cluster = {"status": {"currentPrimary": "polaris-pg-1"}}
        self.assertEqual(driver.pg_before(cluster, SINCE),
                         {"primary": "polaris-pg-1", "since": SINCE})

    def test_readiness_failures_of_instances_since_the_seed_are_counted(self):
        events = {"items": [
            event("polaris-pg-1", "Readiness probe failed: HTTP probe failed",
                  "2026-10-16T12:05:00Z", 3),
            event("polaris-pg-2", "Readiness probe failed: timeout", "2026-10-16T12:30:00Z"),
            event("polaris-pg-2", "Readiness probe failed: timeout", "2026-10-16T11:59:59Z", 7),
            event("polaris-pg-3", "Liveness probe failed: timeout", "2026-10-16T12:05:00Z"),
            event("polaris-pg-1-initdb-x", "Readiness probe failed", "2026-10-16T12:05:00Z"),
            event("polaris-7d9f-abcde", "Readiness probe failed", "2026-10-16T12:05:00Z"),
            event("polaris-pg-3", "Started container postgres", "2026-10-16T12:05:00Z",
                  reason="Started")]}
        watch = driver.pg_watch({"primary": "polaris-pg-1", "since": SINCE},
                                {"status": {"currentPrimary": "polaris-pg-1"}}, events)
        self.assertEqual(watch, {"primary_before": "polaris-pg-1",
                                 "primary_after": "polaris-pg-1",
                                 "readiness_failures": {"polaris-pg-1": 3, "polaris-pg-2": 1}})

    def test_a_new_primary_is_reported(self):
        watch = driver.pg_watch({"primary": "polaris-pg-1", "since": SINCE},
                                {"status": {"currentPrimary": "polaris-pg-2"}}, {"items": []})
        self.assertEqual(driver.pg_line(watch),
                         "polaris-pg: the primary changed from polaris-pg-1 to polaris-pg-2; "
                         "no readiness probe failure")

    def test_a_steady_primary_and_its_failures_are_one_line(self):
        watch = {"primary_before": "polaris-pg-1", "primary_after": "polaris-pg-1",
                 "readiness_failures": {"polaris-pg-1": 3, "polaris-pg-2": 1}}
        self.assertEqual(driver.pg_line(watch),
                         "polaris-pg: polaris-pg-1 primary throughout; 4 readiness probe "
                         "failure(s): polaris-pg-1 3, polaris-pg-2 1")

    def test_a_run_without_the_record_says_so(self):
        self.assertEqual(driver.pg_line(None), "polaris-pg: not recorded")

    def test_the_events_are_read_in_polaris_namespace_for_pods(self):
        argv = driver.pg_events_command(ENV)
        self.assertEqual(argv[1:5], ["--context", "kind-lakehouse-gate", "-n",
                                     "catalog-polaris"])
        self.assertIn("--field-selector=involvedObject.kind=Pod,reason=Unhealthy", argv)


class Hold(unittest.TestCase):
    def test_the_hold_runs_the_mixed_profile_at_70_percent_of_its_knee(self):
        self.assertEqual(driver.hold_rate(CONFIG, 250), 175)

    def test_without_a_mixed_knee_there_is_no_hold(self):
        self.assertIsNone(driver.hold_rate(CONFIG, None))
        self.assertIsNone(driver.hold_rate(CONFIG, 0))


class Baseline(unittest.TestCase):
    def test_each_knee_and_the_holds_p99_are_compared_with_the_baseline_run(self):
        now = {"profiles": {"mixed": {"knee": 300}, "commits": {"knee": 50}},
               "hold": {"latency_ms": {"conditional_load": {"p99": 30}}}}
        base = {"profiles": {"mixed": {"knee": 200}},
                "hold": {"latency_ms": {"conditional_load": {"p99": 40}}}}
        self.assertEqual(driver.compare(now, base), [
            "commits knee: 50 req/s, not in the baseline",
            "mixed knee: 300 req/s against 200 (+50.0%)",
            "hold conditional_load p99: 30 ms against 40 (-25.0%)"])


class Report(unittest.TestCase):
    def write(self, out, path, data):
        os.makedirs(os.path.dirname(os.path.join(out, path)), exist_ok=True)
        with open(os.path.join(out, path), "w", encoding="utf-8") as f:
            json.dump(data, f)

    def step(self, rate, passed, throttled=()):
        result = driver.step_result(summary(rate=rate), CONFIG)
        result.update({"passed": passed, "throttle": {"k6": 0.0}, "throttled": list(throttled),
                       "seconds": 10})
        return result

    def test_the_summary_holds_each_knee_the_hold_and_verifys_result(self):
        with tempfile.TemporaryDirectory() as out:
            steps = [self.step(100, True), self.step(150, False, ["opa"])]
            self.write(out, "profiles/mixed.json", {"steps": steps, **driver.verdict(steps)})
            self.write(out, "hold.json", self.step(70, True))
            self.write(out, "verify.json", {"problems": [], "commits": {"ok": 1},
                                            "polaris_pg": {
                                                "primary_before": "polaris-pg-1",
                                                "primary_after": "polaris-pg-1",
                                                "readiness_failures": {}}})
            text = io.StringIO()
            with contextlib.redirect_stdout(text):
                self.assertEqual(driver.report(out, driver.gate.load_pins()), 0)
            with open(os.path.join(out, "summary.json"), encoding="utf-8") as f:
                saved = json.load(f)
        self.assertEqual(saved["profiles"]["mixed"]["knee"], 100)
        self.assertEqual(saved["profiles"]["mixed"]["limited_by"], ["opa"])
        self.assertIn("| mixed | 100 | 2 | opa |", text.getvalue())
        self.assertIn("Hold at 70 req/s for 10 s", text.getvalue())
        self.assertIn("Correctness: no fault", text.getvalue())
        self.assertIn("polaris-pg: polaris-pg-1 primary throughout; no readiness probe failure",
                      text.getvalue())

    def test_a_run_that_was_never_verified_says_so(self):
        with tempfile.TemporaryDirectory() as out:
            text = io.StringIO()
            with contextlib.redirect_stdout(text):
                driver.report(out, driver.gate.load_pins())
        self.assertIn("Hold: skipped (not run)", text.getvalue())
        self.assertIn("Correctness: not verified", text.getvalue())


class Commands(unittest.TestCase):
    def test_the_k6_pod_is_pinned_limited_labelled_and_mounts_the_scenario(self):
        pod = driver.k6_pod(ENV, "grafana/k6:1@sha256:" + "a" * 64, CONFIG)
        self.assertEqual(pod["metadata"]["namespace"], "catalog-polaris")
        self.assertEqual(pod["metadata"]["labels"], {"lakehouse-gate/check": "true",
                                                     "app.kubernetes.io/name": driver.K6_POD})
        (container,) = pod["spec"]["containers"]
        self.assertEqual(container["resources"], {"limits": {"cpu": "2"}})
        self.assertEqual(container["command"], ["sleep", "infinity"])
        self.assertEqual(container["volumeMounts"], [{"name": "stress",
                                                      "mountPath": "/stress",
                                                      "readOnly": True}])
        env = {e["name"]: e["valueFrom"]["secretKeyRef"] for e in container["env"]}
        self.assertEqual(env, {"ROOT_CLIENT_ID": {"name": "polaris-root", "key": "clientId"},
                               "ROOT_CLIENT_SECRET": {"name": "polaris-root",
                                                      "key": "clientSecret"}})
        self.assertEqual(pod["spec"]["volumes"],
                         [{"name": "stress", "configMap": {"name": driver.CONFIGMAP}}])

    def test_a_step_is_one_k6_run_in_that_pod_at_one_rate(self):
        argv = driver.k6_run(ENV, "mixed-02", "mixed", 200, 30)
        self.assertEqual(argv[:6], ["kubectl", "--context", "kind-lakehouse-gate", "-n",
                                    "catalog-polaris", "exec"])
        self.assertIn(driver.K6_POD, argv)
        self.assertIn("--console-output=/tmp/mixed-02.log", argv)
        settings = dict(argv[i + 1].split("=", 1) for i, a in enumerate(argv) if a == "-e")
        self.assertEqual(settings, {"PROFILE": "mixed", "RATE": "200", "SECONDS": "30",
                                    "NAME": "mixed-02", "POLARIS": "http://polaris:8181",
                                    "REALM": "POLARIS", "CATALOG": "gate_stress_catalog"})
        self.assertEqual(argv[-1], "/stress/scenario.js")

    def test_the_seed_pod_installs_pyiceberg_by_hash_then_seeds_as_root_with_the_smoke_key(self):
        env = {**ENV, "S3_ENDPOINT": "http://seaweedfs.standins.svc.cluster.local:8333",
               "S3_REGION": "us-east-1", "POLARIS_WAREHOUSE_BUCKET": "polaris-warehouse"}
        argv = driver.seed_pod(env, PYTHON, "stress-seed-1", "seed")
        self.assertEqual(argv[:8], ["kubectl", "--context", "kind-lakehouse-gate", "-n",
                                    "catalog-polaris", "run", "stress-seed-1",
                                    "--labels=lakehouse-gate/check=true"])
        self.assertIn(f"--image={PYTHON}", argv)
        overrides = json.loads(argv[argv.index("--overrides") + 1])
        (container,) = overrides["spec"]["containers"]
        refs = {e["name"]: e["valueFrom"]["secretKeyRef"]["name"] for e in container["env"]
                if "valueFrom" in e}
        self.assertEqual(refs, {"ROOT_CLIENT_ID": "polaris-root",
                                "ROOT_CLIENT_SECRET": "polaris-root",
                                "S3_ACCESS_KEY_ID": "smoke-s3", "S3_SECRET_ACCESS_KEY": "smoke-s3"})
        self.assertEqual(overrides["spec"]["volumes"],
                         [{"name": "stress", "configMap": {"name": driver.CONFIGMAP}}])
        script = argv[argv.index("-c") + 1]
        self.assertIn("pip install --quiet --require-hashes --only-binary :all: "
                      "-r /stress/requirements.txt", script)
        self.assertEqual(argv[argv.index("-c") + 2:],
                         ["seed", "seed", "http://polaris:8181", "POLARIS",
                          env["S3_ENDPOINT"], "us-east-1", "polaris-warehouse"])

    def test_verify_needs_no_pip_and_no_s3(self):
        argv = driver.seed_pod(ENV, PYTHON, "stress-verify-1", "verify")
        script = argv[argv.index("-c") + 1]
        self.assertNotIn("pip", script)
        self.assertEqual(argv[argv.index("-c") + 2:],
                         ["seed", "verify", "http://polaris:8181", "POLARIS"])

    def test_the_configmap_carries_the_scripts_the_config_and_the_seeds_tables(self):
        cm = driver.configmap(ENV, {"tables": []})
        self.assertEqual((cm["kind"], cm["metadata"]["name"], cm["metadata"]["namespace"]),
                         ("ConfigMap", driver.CONFIGMAP, "catalog-polaris"))
        self.assertEqual(set(cm["data"]), {"seed.py", "smoke.py", "requirements.txt",
                                           "scenario.js", "config.json", "tables.json"})
        self.assertEqual(json.loads(cm["data"]["tables.json"]), {"tables": []})

    def test_k6_is_pinned_by_digest(self):
        pins = driver.gate.load_pins()
        self.assertRegex(pins["load_images"]["k6"]["image"],
                         r"^grafana/k6:\d+\.\d+\.\d+@sha256:[0-9a-f]{64}$")

    def test_every_kubectl_call_names_the_context_and_a_namespace(self):
        for argv in (driver.k6_run(ENV, "a", "mixed", 1, 1), driver.fetch(ENV, "x", "/tmp/a")):
            self.assertEqual(argv[1:3], ["--context", "kind-lakehouse-gate"])
            self.assertEqual(argv[3], "-n")


class Cli(unittest.TestCase):
    def test_an_unknown_command_or_profile_prints_the_usage(self):
        for argv in (["driver.py"], ["driver.py", "ramp", "nope", "/tmp/x"],
                     ["driver.py", "dance", "/tmp/x"]):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(driver.main(argv), 2)
            self.assertRegex(err.getvalue(), re.compile("^Usage: ", re.M))


if __name__ == "__main__":
    unittest.main()
