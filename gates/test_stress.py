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


def cadvisor(rows, memory=None):
    """cAdvisor's text: rows of (namespace, pod, container, usage, periods, throttled), and
    each pod's working set from `memory`."""
    lines = ["# HELP container_cpu_usage_seconds_total x", "# TYPE x counter"]
    for ns, pod, container, usage, periods, throttled in rows:
        labels = f'container="{container}",id="/x",namespace="{ns}",pod="{pod}"'
        lines += [f"container_cpu_usage_seconds_total{{{labels}}} {usage} 1700000000000",
                  f"container_cpu_cfs_periods_total{{{labels}}} {periods} 1700000000000",
                  f"container_cpu_cfs_throttled_periods_total{{{labels}}} {throttled}"]
        if memory and pod in memory:
            lines.append(f"container_memory_working_set_bytes{{{labels}}} {memory[pod]}")
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

    def test_the_hold_is_sampled_while_it_runs(self):
        self.assertGreater(self.config["hold"]["sample_seconds"], 0)

    def test_each_ramp_and_the_hold_wait_for_a_settled_polaris(self):
        self.assertGreater(self.config["settle"]["seconds"], 0)
        self.assertGreater(self.config["settle"]["timeout_seconds"],
                           self.config["settle"]["seconds"])

    def test_the_scenario_logs_each_commit_before_its_post(self):
        with open(os.path.join(HERE, "stress", "scenario.js"), encoding="utf-8") as f:
            script = f.read()
        sent, post = script.index("console.log(`C sent "), script.index("send(kind, 'POST', url")
        self.assertLess(sent, post)

    def test_tier_ds_overlay_holds_the_mixed_profile_at_80_percent_for_2_hours(self):
        # ADR-053 §7: the four ramps with longer steps, then the mixed profile at 80% of its
        # knee for 2 hours; k6 keeps its 2 CPUs.
        tier_d = driver.load_config(environ={"STRESS_OVERLAY": driver.CAPACITY})
        self.assertEqual(driver.profiles(tier_d), driver.profiles(self.config))
        self.assertGreater(tier_d["step_seconds"], self.config["step_seconds"])
        self.assertEqual(tier_d["hold"], {**self.config["hold"], "fraction": 0.8,
                                          "seconds": 7200,
                                          "sample_seconds": tier_d["hold"]["sample_seconds"]})
        self.assertEqual(tier_d["k6"]["cpu"], "2")
        for name in driver.profiles(tier_d):
            with self.subTest(profile=name):
                self.assertEqual(tier_d["profiles"][name]["shares"],
                                 self.config["profiles"][name]["shares"])

    def test_an_overlay_is_named_from_the_repository_root_or_absolutely(self):
        relative = driver.load_config(environ={"STRESS_OVERLAY": "gates/stress/capacity.json"})
        self.assertEqual(relative, driver.load_config(environ={"STRESS_OVERLAY":
                                                               driver.CAPACITY}))

    def test_the_scenario_knows_every_kind_and_reads_the_config(self):
        with open(os.path.join(HERE, "stress", "scenario.js"), encoding="utf-8") as f:
            script = f.read()
        for kind in driver.KINDS:
            self.assertIn(f"'{kind}'", script)
        self.assertIn("/stress/config.json", script)
        self.assertIn("responseType", script)
        self.assertIn("null, h, 'none')", script, "loads read no body")

    def test_each_client_fetches_a_new_token_before_polaris_s_hour_is_up(self):
        # Attempt 6's hold on 2026-10-16 (#174): one token from setup() for 2 hours, and
        # every request after its first hour was a 401.
        with open(os.path.join(HERE, "stress", "scenario.js"), encoding="utf-8") as f:
            script = f.read()
        self.assertFalse("Bearer ${data.token}" in script, "no request uses setup()'s token")
        self.assertTrue("config.token_refresh_s" in script)
        config = driver.load_config(environ={})
        self.assertLess(config["token_refresh_s"], 3600, "Polaris's tokens last an hour")


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
        self.assertEqual(cpu["opa"], {"usage": 12.0, "periods": 150, "throttled": 5,
                                      "memory": 0})
        self.assertEqual(cpu["seaweedfs"], {"usage": 0, "periods": 0, "throttled": 0,
                                            "memory": 0})

    def test_the_working_set_is_summed_per_container(self):
        text = cadvisor([("standins", "opa-1", "opa", 1, 1, 0),
                         ("standins", "opa-2", "opa", 1, 1, 0)],
                        memory={"opa-1": 100, "opa-2": 50})
        self.assertEqual(driver.container_cpu([text], self.TARGETS)["opa"]["memory"], 150)

    def test_each_polaris_pg_instance_is_counted_on_its_own(self):
        # ADR-053 §7: polaris-pg's cores for 12,000 req/s are its primary's, since every
        # Polaris replica shares one primary.
        text = cadvisor([("catalog-polaris", "polaris-pg-1", "postgres", 7, 0, 0),
                         ("catalog-polaris", "polaris-pg-2", "postgres", 2, 0, 0),
                         ("catalog-polaris", "polaris-pg-2", "plugin-barman-cloud", 9, 0, 0),
                         ("standins", "polaris-pg-3", "postgres", 9, 0, 0)])
        self.assertEqual(driver.pod_cpu([text], "catalog-polaris", "postgres"),
                         {"polaris-pg-1": 7.0, "polaris-pg-2": 2.0})

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

    def test_a_commit_sent_with_no_outcome_logged_may_be_there_as_an_unknown_one(self):
        # k6 cuts an iteration short when a step ends: the scenario logs each commit before
        # its POST, so one with no outcome after it may have happened (#174).
        console = ('time="x" level=info msg="C sent ns01.t02 126" source=console\n'
                   'time="x" level=info msg="C sent ns01.t02 127" source=console\n'
                   'time="x" level=info msg="C ok ns01.t02 127" source=console\n')
        records = driver.commit_records(console)
        self.assertEqual(records, [("sent", "ns01.t02", "126"), ("sent", "ns01.t02", "127"),
                                   ("ok", "ns01.t02", "127")])
        problems, tally = driver.accounting(records, self.found(ns01_t02=(["126", "127"], [])))
        self.assertEqual(problems, [])
        self.assertEqual(tally, {"ok": 1, "unknown": 1, "found": 2})
        problems, _ = driver.accounting(records[1:], self.found(ns01_t02=(["126", "127"], [])))
        self.assertEqual(problems, ["ns01.t02: snapshot 126 was not answered 200 nor left "
                                    "unknown"])

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
    and not judged (ADR-054 §2): a failover under load restarts no container."""

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


def jvm_text(heap=(100, 50), live=70, pauses=(3, 0.5, 0.2), queue=4, rejected=1):
    """Polaris's /q/metrics with the JVM and worker-pool series a hold sample reads."""
    a = 'application="Polaris"'
    old, eden = heap
    count, total, longest = pauses
    pool = f'{a},pool_name="vert.x-worker-thread",pool_type="worker"'
    other = f'{a},pool_name="vert.x-internal-blocking",pool_type="worker"'
    gc = f'action="end of minor GC",{a},cause="x",gc="PS Scavenge"'
    return "\n".join([
        f'jvm_memory_used_bytes{{{a},area="heap",id="PS Old Gen"}} {old}',
        f'jvm_memory_used_bytes{{{a},area="heap",id="PS Eden Space"}} {eden}',
        f'jvm_memory_used_bytes{{{a},area="nonheap",id="Metaspace"}} 999',
        f'jvm_memory_max_bytes{{{a},area="heap",id="PS Old Gen"}} 4000',
        f'jvm_memory_max_bytes{{{a},area="heap",id="PS Eden Space"}} 2000',
        f'jvm_memory_max_bytes{{{a},area="nonheap",id="Metaspace"}} -1.0',
        f'jvm_gc_live_data_size_bytes{{{a}}} {live}',
        f'jvm_gc_pause_seconds_count{{{gc}}} {count}',
        f'jvm_gc_pause_seconds_sum{{{gc}}} {total}',
        f'jvm_gc_pause_seconds_max{{{gc}}} {longest}',
        f'worker_pool_queue_size{{{pool}}} {queue}', f'worker_pool_queue_size{{{other}}} 50',
        f'worker_pool_rejected_total{{{pool}}} {rejected}',
    ]) + "\n"


class HoldSamples(unittest.TestCase):
    def test_a_sample_reads_the_heap_after_gc_the_pauses_and_the_worker_queue(self):
        self.assertEqual(driver.jvm_sample([jvm_text()]), {
            "heap_used": 150, "heap_max": 6000, "heap_after_gc": 70, "gc_pauses": 3,
            "gc_seconds": 0.5, "gc_longest": 0.2, "worker_queue": 4, "worker_rejected": 1})

    def test_zgcs_generations_share_one_heap_so_its_maximum_counts_once(self):
        # Both of ZGC's pools report the whole heap's maximum (ADR-062): 5,736 MiB read 11,472.
        a = 'application="Polaris"'
        text = "\n".join([
            f'jvm_memory_max_bytes{{{a},area="heap",id="ZGC Old Generation"}} 6000',
            f'jvm_memory_max_bytes{{{a},area="heap",id="ZGC Young Generation"}} 6000',
            f'jvm_memory_used_bytes{{{a},area="heap",id="ZGC Old Generation"}} 100',
            f'jvm_memory_used_bytes{{{a},area="heap",id="ZGC Young Generation"}} 50']) + "\n"
        sample = driver.jvm_sample([text, text])
        self.assertEqual((sample["heap_max"], sample["heap_used"]), (12000, 300),
                         "summed over the two pods, each pod's heap once")

    def test_the_hold_is_sampled_every_interval_until_k6_ends(self):
        clock = {"now": 0.0}

        class K6:
            returncode = 0

            def poll(self):
                return 0 if clock["now"] >= 250 else None

        def sleep(seconds):
            clock["now"] += seconds

        taken = []
        code = driver.watch(K6, 100, taken.append, clock=lambda: clock["now"], sleep=sleep)
        self.assertEqual(code, 0)
        self.assertEqual(taken, [100, 200])

    def test_without_an_interval_the_run_is_only_waited_for(self):
        class K6:
            def wait(self):
                return 3

        taken = []
        self.assertEqual(driver.watch(K6, None, taken.append), 3)
        self.assertEqual(taken, [])

    def test_the_series_gives_the_heaps_drift_the_pauses_and_the_working_sets(self):
        def at(t, live, used, pauses, gc, longest, queue, rejected, memory):
            return {"t": t, "jvm": {"heap_used": used, "heap_max": 6000, "heap_after_gc": live,
                                    "gc_pauses": pauses, "gc_seconds": gc,
                                    "gc_longest": longest, "worker_queue": queue,
                                    "worker_rejected": rejected},
                    "memory": {"polaris": memory, "polaris-pg": 10}}
        series = [at(60, 100, 300, 10, 1.0, 0.1, 0, 2, 1000),
                  {"t": 120, "error": "kubectl exec failed"},
                  at(180, 140, 500, 30, 4.0, 0.3, 7, 2, 1500),
                  at(240, 120, 400, 40, 5.5, 0.2, 1, 5, 1400)]
        self.assertEqual(driver.series_summary(series), {
            "samples": 3, "failed_samples": 1,
            "heap_after_gc": {"first": 100, "last": 120, "max": 140},
            "heap_used_max": 500, "heap_max": 6000,
            "gc": {"pauses": 30, "seconds": 4.5, "longest": 0.3},
            "worker_queue_max": 7, "worker_rejected": 3,
            "memory": {"polaris": {"first": 1000, "last": 1400, "max": 1500},
                       "polaris-pg": {"first": 10, "last": 10, "max": 10}}})

    def test_a_series_with_no_sample_gives_none(self):
        self.assertIsNone(driver.series_summary([]))
        self.assertIsNone(driver.series_summary([{"t": 60, "error": "x"}]))


class Capacity(unittest.TestCase):
    def step(self, rate, passed, polaris=None, pg=None, by_pod=None, primary=None,
             requests=1000):
        step = {"rate": rate, "passed": passed, "requests": requests}
        if polaris is not None:
            step["cpu_seconds"] = {"polaris": polaris, "polaris-pg": pg}
            step["cpu_seconds_by_pod"] = by_pod or {}
            step["primary"] = primary
        return step

    def test_the_knee_step_is_the_last_that_passed(self):
        steps = [self.step(100, True), self.step(200, True), self.step(300, False)]
        self.assertEqual(driver.knee_step(steps)["rate"], 200)
        self.assertEqual(driver.knee_step(steps[:2])["rate"], 200, "the ramp ended first")
        self.assertIsNone(driver.knee_step([self.step(100, False)]))
        self.assertIsNone(driver.knee_step([]))

    def test_cpu_per_request_at_the_knee_counts_polaris_and_polaris_pgs_primary(self):
        step = self.step(200, True, polaris=8.0, pg=3.0, primary="polaris-pg-2",
                         by_pod={"polaris-pg-1": 0.5, "polaris-pg-2": 2.0, "polaris-pg-3": 0.5},
                         requests=4000)
        self.assertEqual(driver.cpu_per_request(step), {
            "polaris": 0.002, "polaris-pg": 0.00075, "polaris-pg primary": 0.0005})

    def test_with_the_collectors_cpu_the_cost_is_also_given_without_it(self):
        # ADR-061 §4: the collector's share answers whether a step's cost is the requests'.
        step = self.step(200, True, polaris=8.0, pg=3.0, requests=4000)
        step["collector_cpu_seconds"] = 2.0
        cost = driver.cpu_per_request(step)
        self.assertEqual((cost["polaris"], cost["polaris collector"],
                          cost["polaris without collector"]), (0.002, 0.0005, 0.0015))

    def test_a_step_without_cpu_or_requests_has_no_cost(self):
        self.assertIsNone(driver.cpu_per_request(None))
        self.assertIsNone(driver.cpu_per_request(self.step(1, True)))
        self.assertIsNone(driver.cpu_per_request(self.step(1, True, 1.0, 1.0, requests=0)))

    def test_the_sizing_for_12000_req_s_divides_by_the_mixed_knee(self):
        # ADR-053 §7: replicas = 12,000 / the mixed knee; polaris-pg's cores = 12,000 x its
        # primary's CPU-seconds per request, all three instances' when the primary is unknown.
        self.assertEqual(driver.TARGET_RPS, 12000)
        self.assertEqual(driver.sizing(2500, {"polaris": 0.002, "polaris-pg": 0.0009,
                                              "polaris-pg primary": 0.0005}),
                         {"target_rps": 12000, "polaris_replicas": 5,
                          "polaris_pg_primary_cores": 6.0})
        self.assertEqual(driver.sizing(3000, {"polaris": 0.002, "polaris-pg": 0.0009})
                         ["polaris_pg_primary_cores"], 10.8)
        self.assertIsNone(driver.sizing(0, {"polaris": 1}))
        self.assertIsNone(driver.sizing(100, None))

    def test_polaris_size_reads_the_deployments_resources_and_the_heap_maximum(self):
        resources = {"limits": {"cpu": "4", "memory": "8Gi"},
                     "requests": {"cpu": "4", "memory": "8Gi"}}
        deployment = {"spec": {"replicas": 1, "template": {"spec": {"containers": [
            {"name": "polaris", "resources": resources}]}}}}
        self.assertEqual(driver.polaris_size(deployment, [jvm_text()]),
                         {"replicas": 1, "resources": resources, "heap_max_bytes": 6000})

    def test_metadata_growth_compares_the_seed_with_verify_for_hot_and_ordinary_tables(self):
        seeded = [{"name": "a", "hot": True, "metadata_bytes": 1000},
                  {"name": "b", "hot": False, "metadata_bytes": 1100},
                  {"name": "c", "hot": False, "metadata_bytes": 1050}]
        found = {"a": {"metadata_bytes": 5000}, "b": {"metadata_bytes": 1500},
                 "c": {"metadata_bytes": 1200}}
        self.assertEqual(driver.metadata_growth(seeded, found), {
            "seeded": {"min": 1000, "max": 1100},
            "after": {"hot": {"min": 5000, "max": 5000},
                      "ordinary": {"min": 1200, "max": 1500}}})
        self.assertIsNone(driver.metadata_growth(seeded, {"a": {"new": []}}))


class Collector(unittest.TestCase):
    """ADR-061 §4: the CPU of the threads the JVM names for its collector, from /proc."""

    @staticmethod
    def stat(tid, comm, utime, stime):
        rest = ["S"] + ["0"] * 10 + [str(utime), str(stime)] + ["0"] * 37
        return f"{tid} ({comm}) " + " ".join(rest)

    def test_the_collector_threads_cpu_in_seconds(self):
        text = "\n".join(["100", self.stat(1, "java", 5, 5),
                          self.stat(20, "GC Thread#0", 150, 50),
                          self.stat(21, "ZWorkerYoung#0", 30, 10),
                          self.stat(22, "ZDirector", 1, 1),
                          self.stat(23, "RuntimeWorker#0", 2, 0),
                          self.stat(24, "G1 Conc#0", 4, 0),
                          self.stat(30, "executor-thread", 900, 100),
                          self.stat(31, "Zoo (keeper) x", 900, 100),
                          self.stat(32, "VM Thread", 7, 0)]) + "\n"
        self.assertEqual(driver.collector_seconds(text), 2.48)

    def test_another_clock_rate_is_used_as_read(self):
        text = "250\n" + self.stat(20, "GC Thread#1", 400, 100) + "\n"
        self.assertEqual(driver.collector_seconds(text), 2.0)

    def test_the_command_reads_every_thread_of_polaris_java_process(self):
        argv = driver.collector_command(ENV, "polaris-abc")
        self.assertEqual(argv[:6], ["kubectl", "--context", driver.gate.CONTEXT, "-n",
                                    "catalog-polaris", "exec"])
        self.assertIn("polaris-abc", argv)
        self.assertIn("/proc/1/task/*/stat", argv[-1])

    def test_a_step_counts_the_collectors_cpu_between_its_samples(self):
        before, after = Settle.sample(None, 10.0), Settle.sample(None, 12.5)
        before["collector"], after["collector"] = 3.0, 3.75
        measured = driver.measure({"passed": True, "requests": 100, "reasons": []},
                                  before, after, 0.05)
        self.assertEqual(measured["collector_cpu_seconds"], 0.75)
        del before["collector"]
        self.assertNotIn("collector_cpu_seconds", driver.measure(
            {"passed": True, "requests": 100, "reasons": []}, before, after, 0.05))


class Settle(unittest.TestCase):
    """ADR-053 §4 asks each step to measure Polaris: a ramp that starts while Polaris is still
    restarting from the last ramp's overload measures the restart (#174)."""

    def settle(self, reads, seconds=60, timeout=600):
        clock = {"now": 0.0}
        reads = iter(reads)

        def sleep(s):
            clock["now"] += s

        done = driver.wait_settled(lambda: next(reads), seconds, timeout,
                                   clock=lambda: clock["now"], sleep=sleep, every=10)
        return done, clock["now"]

    def test_a_ready_polaris_with_no_restart_settles_after_the_settle_time(self):
        self.assertEqual(self.settle([(True, 0)] * 7), (True, 60))

    def test_a_restart_or_a_pod_not_ready_starts_the_settle_time_again(self):
        reads = [(True, 0)] * 3 + [(False, 1)] + [(True, 1)] * 7
        self.assertEqual(self.settle(reads), (True, 100))

    def test_a_polaris_that_never_settles_times_out(self):
        self.assertEqual(self.settle([(False, 0)] * 100, timeout=120), (False, 120))

    def pods(self, *pods):
        return {"items": [{"metadata": {"name": name},
                           "status": {"phase": "Running",
                                      "containerStatuses": [{"ready": ready,
                                                             "restartCount": n}]}}
                          for name, ready, n in pods]}

    def test_polaris_is_ready_when_every_pod_is_and_a_new_pod_counts_as_a_change(self):
        self.assertEqual(driver.polaris_readiness(self.pods(("p-1", True, 2))),
                         (True, (("p-1", 2),)))
        self.assertEqual(driver.polaris_readiness(self.pods(("p-1", True, 0),
                                                            ("p-2", False, 0)))[0], False)
        self.assertEqual(driver.polaris_readiness(self.pods())[0], False)
        self.assertNotEqual(driver.polaris_readiness(self.pods(("p-1", True, 0))),
                            driver.polaris_readiness(self.pods(("p-2", True, 0))))

    def test_a_step_polaris_restarted_in_has_no_cost_per_request(self):
        step = {"rate": 1, "passed": True, "requests": 100, "primary": None,
                "cpu_seconds": {"polaris": -900.0, "polaris-pg": 1.0},
                "restarts": ["polaris-x restarted 1 time(s) during the run"]}
        self.assertIsNone(driver.cpu_per_request(step))

    def sample(self, usage):
        return {"cpu": {name: {"usage": usage, "periods": 100, "throttled": 0, "memory": 1}
                        for name in driver.CONTAINERS},
                "polaris": [""], "pg_pods": {"polaris-pg-1": usage}}

    def test_a_polaris_unread_after_its_step_waits_to_settle_and_gives_no_sample(self):
        calls = []

        def unreadable():
            raise SystemExit("wget: can't connect to remote host: Connection refused")

        with contextlib.redirect_stdout(io.StringIO()):
            self.assertIsNone(driver.read_after(unreadable, lambda: calls.append("settle")))
        self.assertEqual(calls, ["settle"])
        self.assertEqual(driver.read_after(lambda: "sample", lambda: calls.append("x")),
                         "sample")
        self.assertEqual(calls, ["settle"])

    def test_a_step_with_no_sample_after_it_fails_with_no_cost(self):
        result = {"passed": True, "requests": 100, "reasons": []}
        measured = driver.measure(dict(result, reasons=[]), self.sample(10.0), None, 0.05)
        self.assertFalse(measured["passed"])
        self.assertEqual(measured["reasons"], [driver.UNREAD])
        self.assertEqual((measured["throttle"], measured["throttled"]), ({}, []))
        self.assertNotIn("cpu_seconds", measured)
        self.assertIsNone(driver.cpu_per_request(dict(measured, primary=None)))
        read = driver.measure(dict(result, reasons=[]), self.sample(10.0), self.sample(12.5),
                              0.05)
        self.assertTrue(read["passed"])
        self.assertEqual(read["cpu_seconds"]["polaris"], 2.5)
        self.assertEqual(read["cpu_seconds_by_pod"], {"polaris-pg-1": 2.5})


class Timeline(unittest.TestCase):
    """The Prometheus stand-in's view of the run, kept as evidence (ADR-046, #174)."""

    def test_the_window_runs_from_the_first_step_to_the_last(self):
        steps = [{"started": "2026-10-16T15:12:06Z", "ended": "2026-10-16T15:14:16Z"},
                 {"started": "2026-10-16T15:20:35Z", "ended": "2026-10-16T15:22:45Z"},
                 {"skipped": "no mixed knee"}]
        self.assertEqual(driver.timeline_window(steps),
                         ("2026-10-16T15:12:06Z", "2026-10-16T15:22:45Z"))
        self.assertIsNone(driver.timeline_window([{"skipped": "x"}]))

    def test_a_range_query_goes_to_the_stand_in_through_the_api_servers_proxy(self):
        path = driver.range_path(ENV, 'up{job="polaris"}', "2026-10-16T15:00:00Z",
                                 "2026-10-16T16:00:00Z", 30)
        prefix = "/api/v1/namespaces/standins/services/prometheus:9090/proxy/api/v1/query_range?"
        self.assertTrue(path.startswith(prefix), path)
        query = dict(p.split("=", 1) for p in path[len(prefix):].split("&"))
        self.assertEqual(query["query"], "up%7Bjob%3D%22polaris%22%7D")
        self.assertEqual((query["step"], query["start"]), ("30", "2026-10-16T15%3A00%3A00Z"))

    def test_a_querys_series_are_summed_per_timestamp(self):
        answer = json.dumps({"status": "success", "data": {"resultType": "matrix", "result": [
            {"metric": {"pod": "a"}, "values": [[100, "1.5"], [130, "2"]]},
            {"metric": {"pod": "b"}, "values": [[100, "0.5"], [130, "NaN"]]}]}})
        self.assertEqual(driver.range_series(answer), [[100, 2.0], [130, 2.0]])

    def test_the_printed_timeline_has_one_row_a_minute_whatever_second_the_run_began(self):
        start = 1760634386  # 17:06:26 UTC: no sample falls on a whole minute
        times = [start + 15 * i for i in range(12)]
        self.assertEqual(driver.per_minute(times), [start, start + 45, start + 105, start + 165])

    def test_the_timeline_names_polaris_cpu_load_latency_heap_and_polaris_pg_waits(self):
        for name in ("polaris_cores", "requests_per_s", "mean_ms", "in_flight", "heap_after_gc",
                     "gc_seconds_per_s", "polaris_start", "pg_waiting", "pg_max_tx_s",
                     "pg_replication_lag_s"):
            self.assertIn(name, driver.TIMELINE)


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
        self.assertEqual({k: saved["profiles"]["mixed"]["steps"][0][k]
                          for k in ("requests", "errors")}, {"requests": 1050, "errors": 0})
        self.assertIn("| mixed | 100 | 1,050 | 0 | 0.00% | 100.0% | commit 400 ms | passed |",
                      text.getvalue(), "each step's error rate against its rate")
        self.assertIn("| mixed | 150 | 1,050 | 0 | 0.00% | 100.0% | commit 400 ms | failed |",
                      text.getvalue())
        self.assertIn("Hold at 70 req/s for 10 s", text.getvalue())
        self.assertIn("Correctness: no fault", text.getvalue())
        self.assertIn("polaris-pg: polaris-pg-1 primary throughout; no readiness probe failure",
                      text.getvalue())

    def test_tier_d_adds_the_cost_per_request_the_sizing_the_heap_and_the_metadata(self):
        with tempfile.TemporaryDirectory() as out:
            steps = [self.step(100, True), self.step(150, False)]
            steps[0].update({"cpu_seconds": {"polaris": 4.0, "polaris-pg": 1.2},
                             "cpu_seconds_by_pod": {"polaris-pg-1": 0.8},
                             "collector_cpu_seconds": 1.0,
                             "primary": "polaris-pg-1", "requests": 2000})
            self.write(out, "profiles/mixed.json", {"steps": steps, **driver.verdict(steps)})
            hold = self.step(80, True)
            hold["series"] = [{"t": 60, "jvm": driver.jvm_sample([jvm_text()]),
                               "memory": {"polaris": 900, "polaris-pg": 10}}]
            self.write(out, "hold.json", hold)
            self.write(out, "polaris.json", {"replicas": 1, "resources": {
                "limits": {"cpu": "4", "memory": "8Gi"}}, "heap_max_bytes": 6000})
            self.write(out, "config.json", {"overlay": "gates/stress/capacity.json",
                                            "config": CONFIG})
            self.write(out, "verify.json", {"problems": [], "commits": {"ok": 1}, "metadata": {
                "seeded": {"min": 1000, "max": 1100},
                "after": {"hot": {"min": 5000, "max": 6000},
                          "ordinary": {"min": 1200, "max": 1500}}}})
            text = io.StringIO()
            with contextlib.redirect_stdout(text):
                driver.report(out, driver.gate.load_pins())
            with open(os.path.join(out, "summary.json"), encoding="utf-8") as f:
                saved = json.load(f)
        self.assertEqual(saved["profiles"]["mixed"]["cpu_per_request"],
                         {"polaris": 0.002, "polaris-pg": 0.0006, "polaris-pg primary": 0.0004,
                          "polaris collector": 0.0005, "polaris without collector": 0.0015})
        self.assertEqual(saved["sizing"], {"target_rps": 12000, "polaris_replicas": 120,
                                           "polaris_pg_primary_cores": 4.8})
        self.assertEqual(saved["overlay"], "gates/stress/capacity.json")
        self.assertEqual(saved["hold_series"]["heap_after_gc"]["max"], 70)
        self.assertNotIn("series", saved["hold"], "the series stays in hold.json")
        self.assertEqual(saved["polaris_size"]["heap_max_bytes"], 6000)
        self.assertEqual(saved["profiles"]["mixed"]["steps"][0]["restarts"], [],
                         "each step says whether Polaris or polaris-pg restarted in it")
        self.assertEqual(saved["profiles"]["mixed"]["steps"][0]["cpu_per_request"]["polaris"],
                         0.002, "each step's cost, not only the knee's")
        self.assertIsNone(saved["profiles"]["mixed"]["steps"][1]["cpu_per_request"])
        out = text.getvalue()
        self.assertIn("| mixed | 2 | 0.4 | 0.6 |", out)
        self.assertIn("| mixed | 100 | 2 | 0.5 | 1.5 |", out)
        self.assertIn("For 12,000 req/s: 120 Polaris replicas, polaris-pg primary 4.8 cores",
                      out)
        self.assertIn("Heap after GC over the hold: first 70 B, last 70 B, max 70 B", out)
        self.assertIn("Metadata: seeded 1000 to 1100 B; after the run, hot tables 5000 to "
                      "6000 B, ordinary 1200 to 1500 B", out)

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
        cm = driver.configmap(ENV, {"tables": []}, CONFIG)
        self.assertEqual((cm["kind"], cm["metadata"]["name"], cm["metadata"]["namespace"]),
                         ("ConfigMap", driver.CONFIGMAP, "catalog-polaris"))
        self.assertEqual(set(cm["data"]), {"seed.py", "smoke.py", "requirements.txt",
                                           "scenario.js", "config.json", "tables.json"})
        self.assertEqual(json.loads(cm["data"]["tables.json"]), {"tables": []})
        self.assertEqual(json.loads(cm["data"]["config.json"]), CONFIG,
                         "the scenario reads the config the driver runs with, overlay and all")

    def test_k6_is_pinned_by_digest(self):
        pins = driver.gate.load_pins()
        self.assertRegex(pins["load_images"]["k6"]["image"],
                         r"^grafana/k6:\d+\.\d+\.\d+@sha256:[0-9a-f]{64}$")

    def test_every_kubectl_call_names_the_context_and_a_namespace(self):
        for argv in (driver.k6_run(ENV, "a", "mixed", 1, 1), driver.fetch(ENV, "x", "/tmp/a")):
            self.assertEqual(argv[1:3], ["--context", "kind-lakehouse-gate"])
            self.assertEqual(argv[3], "-n")


class Cli(unittest.TestCase):
    def test_the_timeline_is_a_command_of_its_own(self):
        self.assertIn("timeline OUT", driver.__doc__.strip().splitlines()[-1])
        self.assertIn("  timeline OUT ", driver.__doc__)

    def test_an_unknown_command_or_profile_prints_the_usage(self):
        for argv in (["driver.py"], ["driver.py", "ramp", "nope", "/tmp/x"],
                     ["driver.py", "dance", "/tmp/x"]):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(driver.main(argv), 2)
            self.assertRegex(err.getvalue(), re.compile("^Usage: ", re.M))


if __name__ == "__main__":
    unittest.main()
