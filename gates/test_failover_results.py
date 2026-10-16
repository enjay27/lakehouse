"""Tests for failover/results.py: what a run's records say (ADR-047 §4, §7).
Run: python3 -m unittest discover -s gates"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "failover"))
import results  # noqa: E402

T0 = 1_760_000_000_000


def write(mode, seq, sent, done, ok=True, error=None):
    record = {"mode": mode, "seq": seq, "sent": sent, "done": done, "ok": ok}
    if error:
        record["error"] = error
    return record


def steady(mode, start, end, period=100, latency=3, ok=True, first_seq=0):
    """One write every `period` ms from `start` to before `end`."""
    return [write(mode, first_seq + i, t, t + latency, ok)
            for i, t in enumerate(range(start, end, period))]


def obs(t, phase=results.HEALTHY, primary="polaris-pg-1", stamp="", endpoints=("polaris-pg-1",)):
    return {"t": t, "phase": phase, "current_primary": primary,
            "current_primary_timestamp": stamp, "endpoints": list(endpoints)}


class ParseRecords(unittest.TestCase):
    def test_keeps_only_the_probes_json_lines(self):
        text = "\n".join([
            "Picked up JAVA_TOOL_OPTIONS: -Xmx64m",
            json.dumps(write("pooled", 0, T0, T0 + 2)),
            '{"event": "ready"}',
            "not json {",
            json.dumps(write("per_write", 0, T0, T0 + 9, False, "08006 connection")),
        ])
        records = results.parse_records(text)
        self.assertEqual([r["mode"] for r in records], ["pooled", "per_write"])


class Outage(unittest.TestCase):
    def test_the_longest_gap_between_good_writes_from_the_failure_on(self):
        before = steady("pooled", T0 - 5000, T0 + 300)
        failing = steady("pooled", T0 + 300, T0 + 4000, ok=False, first_seq=len(before))
        after = steady("pooled", T0 + 4000, T0 + 6000, first_seq=len(before) + len(failing))
        got = results.outage(before + failing + after, "pooled", T0)
        # The last good write was sent at T0 + 200, the first after at T0 + 4000.
        self.assertEqual(got["outage_ms"], 3800)
        self.assertTrue(got["recovered"])
        self.assertEqual(got["last_good"], T0 + 203)
        self.assertEqual(got["first_good"], T0 + 4003)

    def test_a_write_that_hangs_and_then_succeeds_is_an_outage_without_a_failure(self):
        records = steady("pooled", T0 - 1000, T0) + [write("pooled", 10, T0, T0 + 9000)]
        records += steady("pooled", T0 + 9000, T0 + 10000, first_seq=11)
        got = results.outage(records, "pooled", T0)
        # From the last good write before (done at T0 - 97) to the hung one (done at T0 + 9000).
        self.assertEqual(got["outage_ms"], 9097)
        self.assertTrue(got["recovered"])

    def test_not_recovered_gives_a_lower_bound_from_the_last_attempt(self):
        records = steady("pooled", T0 - 1000, T0) + steady("pooled", T0, T0 + 2000, ok=False,
                                                            first_seq=10)
        got = results.outage(records, "pooled", T0)
        self.assertFalse(got["recovered"])
        self.assertEqual(got["outage_ms"], (T0 + 1903) - (T0 - 97))

    def test_modes_are_apart(self):
        records = steady("pooled", T0 - 1000, T0 + 1000)
        records += steady("per_write", T0 - 1000, T0) + steady("per_write", T0 + 2000, T0 + 2500)
        self.assertEqual(results.outage(records, "pooled", T0)["outage_ms"], 100)
        self.assertEqual(results.outage(records, "per_write", T0)["outage_ms"], 2100)


class Counts(unittest.TestCase):
    def test_failed_writes_are_counted_from_the_failure_on(self):
        records = [write("pooled", 0, T0 - 100, T0 - 90, False),
                   write("pooled", 1, T0 + 100, T0 + 110, False),
                   write("pooled", 2, T0 + 200, T0 + 210, True),
                   write("per_write", 0, T0 + 100, T0 + 110, False)]
        self.assertEqual(results.failed(records, "pooled", T0), 1)

    def test_lost_are_confirmed_writes_missing_from_the_table(self):
        records = [write("pooled", 0, 1, 2), write("pooled", 1, 3, 4),
                   write("pooled", 2, 5, 6, False), write("per_write", 0, 1, 2)]
        rows = {("pooled", 0), ("pooled", 2), ("per_write", 0)}
        self.assertEqual(results.lost(records, "pooled", rows), [1])
        self.assertEqual(results.lost(records, "per_write", rows), [])

    def test_commit_latency_over_the_window_before_the_failure(self):
        records = [write("pooled", i, T0 - 60_000 + i * 600, T0 - 60_000 + i * 600 + i + 1)
                   for i in range(100)]
        records.append(write("pooled", 100, T0 - 61_000, T0 - 50_000))  # before the window
        records.append(write("pooled", 101, T0 + 10, T0 + 5_000))  # after the failure
        got = results.latency(records, "pooled", T0)
        self.assertEqual(got, {"p50_ms": 50, "p99_ms": 99, "samples": 100})

    def test_percentiles_are_nearest_rank(self):
        self.assertEqual(results.percentile([5, 1, 3], 50), 3)
        self.assertEqual(results.percentile([1, 2, 3, 4], 99), 4)
        self.assertIsNone(results.percentile([], 50))


class Events(unittest.TestCase):
    def test_detected_promoted_and_route_switched(self):
        stamp = "2025-10-09T08:53:24.500000Z"  # T0 + 4.5 s
        observations = [
            obs(T0 - 1000),
            obs(T0 + 1000),
            obs(T0 + 3000, phase=results.FAILING_OVER),
            obs(T0 + 5000, phase=results.FAILING_OVER, primary="polaris-pg-2", stamp=stamp,
                endpoints=()),
            obs(T0 + 6000, phase=results.FAILING_OVER, primary="polaris-pg-2", stamp=stamp,
                endpoints=("polaris-pg-2",)),
            obs(T0 + 9000, primary="polaris-pg-2", stamp=stamp, endpoints=("polaris-pg-2",)),
        ]
        got = results.events(observations, T0, "polaris-pg-1")
        self.assertEqual(got, {"detected": T0 + 3000, "promoted": T0 + 4500,
                               "route_switched": T0 + 6000, "healthy": T0 + 9000,
                               "new_primary": "polaris-pg-2"})

    def test_the_same_primary_back_is_no_promotion(self):
        observations = [obs(T0 + 1000, phase="Instance unhealthy", endpoints=()),
                        obs(T0 + 8000)]
        got = results.events(observations, T0, "polaris-pg-1")
        self.assertEqual(got, {"detected": T0 + 1000, "promoted": None,
                               "route_switched": None, "healthy": T0 + 8000,
                               "new_primary": "polaris-pg-1"})

    def test_a_promotion_without_its_timestamp_uses_the_observation(self):
        observations = [obs(T0 + 2000, phase=results.FAILING_OVER, primary="polaris-pg-3",
                            endpoints=("polaris-pg-3",))]
        got = results.events(observations, T0, "polaris-pg-1")
        self.assertEqual(got["promoted"], T0 + 2000)
        self.assertEqual(got["route_switched"], T0 + 2000)
        self.assertIsNone(got["healthy"])

    def test_timestamps_are_epoch_milliseconds(self):
        self.assertEqual(results.epoch_ms("2025-10-09T08:53:20.000000Z"), T0)
        self.assertEqual(results.epoch_ms("2025-10-09T10:53:20.250000+02:00"), T0 + 250)
        self.assertEqual(results.epoch_ms(1760000000.25), T0 + 250)
        self.assertIsNone(results.epoch_ms(""))

    def test_the_operators_first_failover_line_after_the_failure(self):
        lines = "\n".join([
            json.dumps({"ts": "2025-10-09T08:53:19.000000Z", "msg": "failover done"}),
            "not json",
            json.dumps({"ts": "2025-10-09T08:53:21.000000Z", "msg": "Reconciling"}),
            json.dumps({"ts": "2025-10-09T08:53:22.000000Z",
                        "msg": "Current primary isn't healthy, initiating a failover"}),
            json.dumps({"ts": "2025-10-09T08:53:23.000000Z", "msg": "Failing over"}),
        ])
        self.assertEqual(results.log_detected(lines, T0), T0 + 2000)
        self.assertIsNone(results.log_detected("", T0))


class Done(unittest.TestCase):
    def test_done_once_both_modes_write_again_for_the_settle_time(self):
        records = []
        for mode in results.MODES:
            records += steady(mode, T0 - 1000, T0) + steady(mode, T0 + 4000, T0 + 9500,
                                                            first_seq=10)
        self.assertTrue(results.done(records, T0, settle_ms=5000))
        self.assertFalse(results.done(records, T0, settle_ms=6000))

    def test_not_done_while_one_mode_is_still_failing(self):
        records = steady("pooled", T0 - 1000, T0) + steady("pooled", T0 + 1000, T0 + 9000,
                                                           first_seq=10)
        records += steady("per_write", T0 - 1000, T0) + steady("per_write", T0, T0 + 9000,
                                                               ok=False, first_seq=10)
        self.assertFalse(results.done(records, T0, settle_ms=5000))

    def test_not_done_before_the_failure(self):
        records = [r for mode in results.MODES for r in steady(mode, T0 - 9000, T0)]
        self.assertFalse(results.done(records, T0, settle_ms=5000))


def run_record(setup="C0", scenario="S1", outage_ms=3000, lost_seq=None):
    probe = []
    for mode in results.MODES:
        probe += steady(mode, T0 - 60_000, T0, latency=4)
        probe += steady(mode, T0 + outage_ms - 100, T0 + outage_ms + 1000, latency=4,
                        first_seq=600)
    rows = [[r["mode"], r["seq"]] for r in probe if r["seq"] != lost_seq]
    return {"setup": setup, "scenario": scenario, "run": 1, "t0": T0,
            "old_primary": "polaris-pg-1", "node_down_s": None,
            "observations": [obs(T0 + 2000, phase=results.FAILING_OVER, primary="polaris-pg-2",
                                 endpoints=("polaris-pg-2",)),
                             obs(T0 + 5000, primary="polaris-pg-2",
                                 endpoints=("polaris-pg-2",))],
            "operator_log": "", "probe": probe, "rows": rows, "settings": {"x": 1}}


class Summaries(unittest.TestCase):
    def test_a_run_in_seconds_from_the_failure(self):
        got = results.summarize(run_record(outage_ms=3000, lost_seq=3))
        self.assertEqual(got["setup"], "C0")
        self.assertEqual(got["events_s"], {"detected": 2.0, "detected_log": None,
                                           "promoted": 2.0, "route_switched": 2.0,
                                           "healthy": 5.0})
        pooled = got["modes"]["pooled"]
        self.assertEqual(pooled["outage_s"], 3.0)
        self.assertTrue(pooled["recovered"])
        self.assertEqual(pooled["lost"], 1)
        self.assertEqual(pooled["failed"], 0)
        self.assertEqual(pooled["latency"]["p50_ms"], 4)

    def test_aggregate_gives_median_and_worst_per_setup_failure_and_mode(self):
        runs = [results.summarize(run_record(outage_ms=ms)) for ms in (2000, 9000, 3000)]
        runs.append(results.summarize(run_record(scenario="S2", outage_ms=1000)))
        table = results.aggregate(runs)
        row = table[("C0", "S1", "pooled")]
        self.assertEqual(row["runs"], 3)
        self.assertEqual(row["outage_median_s"], 3.0)
        self.assertEqual(row["outage_worst_s"], 9.0)
        self.assertEqual(row["lost_total"], 0)
        self.assertTrue(row["all_recovered"])
        self.assertEqual(table[("C0", "S2", "per_write")]["runs"], 1)

    def test_markdown_has_a_row_per_setup_failure_and_mode(self):
        runs = [results.summarize(run_record()), results.summarize(run_record(scenario="S3"))]
        text = results.markdown(results.aggregate(runs))
        self.assertIn("| C0 | S1 | pooled | 1 |", text)
        self.assertIn("| C0 | S3 | per_write | 1 |", text)
        self.assertEqual(len([l for l in text.splitlines() if l.startswith("| C0")]), 4)



def s5_record(run=1, valid=True, lost_seq=None):
    """S5: writes confirmed between the cut, 10 s before the pause, and the pause at T0."""
    record = run_record(setup="C1", scenario="S5", outage_ms=130_000, lost_seq=lost_seq)
    record.update({"run": run, "run_id": f"C1-S5-{run}", "valid": valid,
                   "invalid_reason": None if valid else "the survivor did not fall behind",
                   "nodes": ["lakehouse-gate-worker", "lakehouse-gate-worker2"],
                   "survivor": "polaris-pg-1",
                   "cut": {"start": T0 - 10_000, "end": T0 + 50, "survivor_lsn": "0/3000000",
                           "standby_lsn": "0/3001000", "primary_lsn": "0/3001000"}})
    return record


class NodeLosses(unittest.TestCase):
    """S4 and S5 (ADR-058 §2)."""

    def test_the_writes_confirmed_after_the_cut_are_at_risk(self):
        got = results.summarize(s5_record(lost_seq=595))
        # steady() writes every 100 ms: 100 per mode in the 10 s before the pause.
        self.assertEqual(got["modes"]["pooled"]["at_risk"], 100)
        self.assertEqual(got["modes"]["pooled"]["lost"], 1)
        self.assertTrue(got["valid"])
        self.assertEqual(got["survivor"], "polaris-pg-1")
        self.assertEqual(got["nodes"], ["lakehouse-gate-worker", "lakehouse-gate-worker2"])
        self.assertEqual(got["cut"]["survivor_lsn"], "0/3000000")

    def test_without_a_cut_nothing_is_at_risk_and_the_run_is_valid(self):
        got = results.summarize(run_record())
        self.assertIsNone(got["modes"]["pooled"]["at_risk"])
        self.assertTrue(got["valid"])

    def test_an_invalid_run_is_reported_and_not_counted(self):
        runs = [results.summarize(s5_record(1)), results.summarize(s5_record(2, valid=False))]
        table = results.aggregate(runs)
        self.assertEqual(table[("C1", "S5", "pooled")]["runs"], 1)
        self.assertEqual(table[("C1", "S5", "pooled")]["at_risk_total"], 100)
        self.assertEqual(results.invalid(runs),
                         ["C1-S5-2: the survivor did not fall behind"])
        self.assertIsNone(results.aggregate([results.summarize(run_record())])[
            ("C0", "S1", "pooled")]["at_risk_total"])

    def test_standbys_that_did_not_stream_again_are_listed(self):
        record = s5_record()
        record["not_rejoined"] = ["polaris-pg-2"]
        runs = [results.summarize(record), results.summarize(run_record())]
        self.assertEqual(runs[0]["not_rejoined"], ["polaris-pg-2"])
        self.assertEqual(runs[1]["not_rejoined"], [])
        self.assertEqual(results.not_rejoined(runs), [
            "C1-S5-1: polaris-pg-2 did not stream from the primary again; rebuilt"])
        text = results.markdown(results.aggregate(runs), (), results.not_rejoined(runs))
        self.assertIn("After C1-S5-1: polaris-pg-2 did not stream", text)

    def test_markdown_lists_the_writes_at_risk_and_the_invalid_runs(self):
        runs = [results.summarize(s5_record(1, lost_seq=595)),
                results.summarize(s5_record(2, valid=False))]
        text = results.markdown(results.aggregate(runs), results.invalid(runs))
        self.assertIn("| C1 | S5 | pooled | 1 | 100 | 1 |", text)
        self.assertIn("Not counted: C1-S5-2: the survivor did not fall behind", text)
        plain = results.markdown(results.aggregate([results.summarize(run_record())]))
        self.assertNotIn("at risk", plain)
        self.assertNotIn("Not counted", plain)


if __name__ == "__main__":
    unittest.main()
