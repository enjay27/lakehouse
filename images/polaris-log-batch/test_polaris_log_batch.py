"""polaris_log_batch.py, the hourly batch over Polaris's per-pod log files (ADR-036).

Every test builds a log directory with the names Polaris writes (polaris-<pod>.log and
polaris-<pod>.log.<YYYY-MM-DD-HH>[.N].gz), sets mtimes explicitly, and runs the batch at a chosen
"now". No cluster and no network: the pod list is a function. Ported from the archive's
tests/test_polaris_log_batch.py at 09a4773, moved to UTC (ADR-036) and to unittest.

Run: python3 -m unittest discover -s images/polaris-log-batch
"""

import datetime as dt
import fcntl
import gzip
import hashlib
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import polaris_log_batch as plb  # noqa: E402

A = "polaris-7f4d69c67c-aaaaa"
B = "polaris-7f4d69c67c-bbbbb"


def T(text):
    """'2026-09-28 10:15:00' -> an aware datetime in UTC."""
    return dt.datetime.fromisoformat(text).replace(tzinfo=dt.timezone.utc)


def E(text):
    return T(text).timestamp()


def line(when, pod, msg="m", level="INFO", ns=0):
    t = T(when)
    stamp = f"{t:%Y-%m-%dT%H:%M:%S}.{ns:09d}Z"
    return json.dumps({"timestamp": stamp, "level": level, "loggerName": "x",
                       "message": msg, "hostName": pod}) + "\n"


def put(path, data, mtime):
    path.write_bytes(data if isinstance(data, bytes) else data.encode())
    os.utime(path, (E(mtime), E(mtime)))
    return path


def current(d, pod, lines, mtime):
    return put(d / f"polaris-{pod}.log", "".join(lines), mtime)


def roll(d, pod, hour, lines, mtime, n=None):
    name = f"polaris-{pod}.log.{hour}" + (f".{n}" if n else "") + ".gz"
    return put(d / name, gzip.compress("".join(lines).encode()), mtime)


def cfg(d, pods=(A, B), **kw):
    kw.setdefault("policy", plb.PassthroughPolicy())  # these tests: every line kept, as the
    # archive's framework tests ran; the policy has its own tests (test_polaris_log_policy.py)
    lister = kw.pop("lister", None)
    if lister is None and pods is not None:
        lister = lambda: set(pods)  # noqa: E731
    return plb.Config(str(d), pod_lister=lister, **kw)


def run(c, now):
    out = io.StringIO()
    res = plb.run(c, now=T(now), out=out)
    res["log"] = [json.loads(x) for x in out.getvalue().splitlines()]
    return res


def jl(path):
    return [json.loads(x) for x in pathlib.Path(path).read_text().splitlines()]


def summary(d, key):
    return jl(d / "aggregated-logs" / f"{key}.jsonl")[0]


class Case(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.d = pathlib.Path(tmp.name)


class SelectionByTimestamp(Case):
    def test_the_hour_boundary_is_by_timestamp_to_the_nanosecond(self):
        d = self.d
        current(d, A, [line("2026-09-28 10:59:59", A, "last of 10", ns=999_999_999),
                       line("2026-09-28 11:00:00", A, "first of 11", ns=0),
                       line("2026-09-28 11:02:00", A, "later")], "2026-09-28 11:02:00")
        r = run(cfg(d), "2026-09-28 11:03:00")
        self.assertEqual(r["published"], ["20260928-10"])
        docs = jl(d / "processed-logs/20260928-10.jsonl")
        self.assertEqual([x["message"] for x in docs], ["last of 10"])
        raw = line("2026-09-28 10:59:59", A, "last of 10", ns=999_999_999).rstrip("\n")
        self.assertEqual(docs[0]["event_id"], hashlib.sha1(raw.encode()).hexdigest())
        self.assertEqual(docs[0]["log_hour"], "20260928-10")
        r = run(cfg(d), "2026-09-28 12:03:00")
        self.assertEqual(r["published"], ["20260928-11"])
        self.assertEqual([x["message"] for x in jl(d / "processed-logs/20260928-11.jsonl")],
                         ["first of 11", "later"])

    def test_an_offset_timestamp_is_placed_in_its_utc_hour(self):
        raw = json.dumps({"timestamp": "2026-09-28T19:30:00.5+09:00", "level": "INFO",
                          "message": "offset"})
        current(self.d, A, [raw + "\n"], "2026-09-28 10:30:00")
        run(cfg(self.d), "2026-09-28 11:03:00")
        self.assertEqual([x["message"] for x in jl(self.d / "processed-logs/20260928-10.jsonl")],
                         ["offset"])

    def test_the_processed_line_is_polaris_object_as_written_plus_two_fields(self):
        rec = {"timestamp": "2026-09-28T10:05:00.123456Z", "sequence": 7, "level": "INFO",
               "loggerName": "x", "message": "m", "mdc": {"requestId": "r1"}}
        current(self.d, A, [json.dumps(rec) + "\n"], "2026-09-28 10:05:00")
        run(cfg(self.d), "2026-09-28 11:03:00")
        [doc] = jl(self.d / "processed-logs/20260928-10.jsonl")
        self.assertEqual({k: v for k, v in doc.items() if k not in ("event_id", "log_hour")},
                         rec)

    def test_rolls_and_current_files_of_two_pods_merge_into_one_hour(self):
        d = self.d
        roll(d, A, "2026-09-28-10", [line("2026-09-28 10:10:00", A),
                                     line("2026-09-28 10:20:00", A)], "2026-09-28 11:00:01")
        current(d, A, [line("2026-09-28 11:00:01", A)], "2026-09-28 11:00:01")
        current(d, B, [line("2026-09-28 10:30:00", B), line("2026-09-28 10:50:00", B)],
                "2026-09-28 10:50:00")
        run(cfg(d), "2026-09-28 11:03:00")
        s = summary(d, "20260928-10")
        self.assertEqual(s["processed"], 4)
        self.assertEqual(s["by_pod"], {A: 2, B: 2})
        self.assertEqual(s["lines_in"], s["processed"] + s["malformed"])
        self.assertTrue((d / "done/20260928" / f"polaris-{A}.log.2026-09-28-10.gz").exists())
        self.assertTrue((d / f"polaris-{A}.log").exists())
        self.assertTrue((d / f"polaris-{B}.log").exists())

    def test_a_roll_of_the_next_hour_gives_its_boundary_skew_lines_to_their_hour(self):
        d = self.d
        roll(d, A, "2026-09-28-11", [line("2026-09-28 11:00:00", A, "rotated it"),
                                     line("2026-09-28 10:59:59", A, "skew")],
             "2026-09-28 12:00:00")
        run(cfg(d), "2026-09-28 12:03:00")
        self.assertEqual([x["message"] for x in jl(d / "processed-logs/20260928-10.jsonl")],
                         ["skew"])
        self.assertEqual([x["message"] for x in jl(d / "processed-logs/20260928-11.jsonl")],
                         ["rotated it"])

    def test_files_last_written_before_the_hour_are_not_opened(self):
        d = self.d
        plb.ensure_dirs(str(d))
        plb.save_checkpoint(str(d), {"schema": plb.SCHEMA, "last_published": "20260928-09",
                                     "hours": {}})
        put(d / f"polaris-{A}.log.2026-09-28-08.gz", b"not gzip at all", "2026-09-28 09:00:00")
        current(d, B, [line("2026-09-28 10:05:00", B)], "2026-09-28 10:05:00")
        run(cfg(d), "2026-09-28 11:03:00")
        s = summary(d, "20260928-10")
        self.assertEqual(s["corrupt_files"], [])
        self.assertEqual(s["processed"], 1)

    def test_other_prefixes_and_subdirectories_are_ignored(self):
        d = self.d
        put(d / "other-shared.log", line("2026-09-28 10:05:00", "x"), "2026-09-28 10:05:00")
        (d / "legacy").mkdir()
        put(d / "legacy/polaris-old.log", line("2026-09-28 10:05:00", "x"), "2026-09-28 10:05:00")
        current(d, A, [line("2026-09-28 10:06:00", A)], "2026-09-28 10:06:00")
        run(cfg(d), "2026-09-28 11:03:00")
        self.assertEqual(summary(d, "20260928-10")["processed"], 1)
        self.assertTrue((d / "other-shared.log").exists())

    def test_an_empty_hour_is_published_empty_not_skipped(self):
        d = self.d
        current(d, A, [line("2026-09-28 10:05:00", A), line("2026-09-28 12:10:00", A)],
                "2026-09-28 12:10:00")
        r = run(cfg(d), "2026-09-28 13:03:00")
        self.assertEqual(r["published"], ["20260928-10", "20260928-11", "20260928-12"])
        self.assertEqual((d / "processed-logs/20260928-11.jsonl").read_text(), "")
        self.assertEqual(summary(d, "20260928-11")["lines_in"], 0)


class FilesBeingWritten(Case):
    def test_an_unterminated_last_line_is_not_read(self):
        data = line("2026-09-28 10:10:00", A) + '{"timestamp":"2026-09-28T10:59:5'
        put(self.d / f"polaris-{A}.log", data, "2026-09-28 11:02:50")
        run(cfg(self.d), "2026-09-28 11:03:00")
        s = summary(self.d, "20260928-10")
        self.assertEqual((s["processed"], s["malformed"]), (1, 0))

    def test_a_malformed_line_is_booked_once_to_the_previous_valid_hour(self):
        d = self.d
        current(d, A, [line("2026-09-28 10:50:00", A), "garbage, not json\n",
                       line("2026-09-28 11:10:00", A)], "2026-09-28 11:10:00")
        run(cfg(d), "2026-09-28 12:03:00")
        bad = jl(d / "malformed/20260928-10.jsonl")
        self.assertEqual(len(bad), 1)
        self.assertEqual((bad[0]["line_no"], bad[0]["raw"]), (2, "garbage, not json"))
        self.assertFalse((d / "malformed/20260928-11.jsonl").exists())
        self.assertEqual(summary(d, "20260928-10")["malformed"], 1)
        self.assertEqual(summary(d, "20260928-11")["malformed"], 0)

    def test_a_fresh_undecodable_roll_defers_the_hour(self):
        d = self.d
        put(d / f"polaris-{A}.log.2026-09-28-10.gz", gzip.compress(b"x" * 100)[:20],
            "2026-09-28 11:02:50")
        current(d, A, [line("2026-09-28 10:00:01", A)], "2026-09-28 10:00:01")
        r = run(cfg(d), "2026-09-28 11:03:00")
        self.assertEqual((r["status"], r["published"]), ("deferred", []))
        self.assertFalse((d / "processed-logs/20260928-10.jsonl").exists())

    def test_a_stale_undecodable_roll_is_quarantined_and_the_hour_published(self):
        d = self.d
        name = f"polaris-{A}.log.2026-09-28-10.gz"
        put(d / name, gzip.compress(b"x" * 100)[:20], "2026-09-28 11:00:00")
        current(d, A, [line("2026-09-28 10:00:01", A)], "2026-09-28 10:00:01")
        r = run(cfg(d), "2026-09-28 11:03:00")
        self.assertEqual(r["published"], ["20260928-10"])
        s = summary(d, "20260928-10")
        self.assertEqual([c["file"] for c in s["corrupt_files"]], [name])
        self.assertTrue((d / "malformed/files" / name).exists())


class Orphans(Case):
    def test_an_orphan_moves_only_when_its_pod_is_gone_and_it_is_complete(self):
        d = self.d
        current(d, A, [line("2026-09-28 10:37:00", A), line("2026-09-28 10:48:00", A)],
                "2026-09-28 10:48:00")
        current(d, B, [line("2026-09-28 10:40:00", B)], "2026-09-28 10:40:00")
        run(cfg(d, pods=(B,)), "2026-09-28 11:03:00")
        s = summary(d, "20260928-10")
        self.assertEqual(s["processed"], 3)
        self.assertEqual([o["pod"] for o in s["orphans_moved"]], [A])
        self.assertTrue((d / "done/20260928" / f"polaris-{A}.log.2026-09-28-10.orphan").exists())
        self.assertFalse((d / f"polaris-{A}.log").exists())
        self.assertTrue((d / f"polaris-{B}.log").exists())

    def test_an_orphan_still_in_the_current_hour_is_left_alone(self):
        current(self.d, A, [line("2026-09-28 10:30:00", A), line("2026-09-28 11:00:30", A)],
                "2026-09-28 11:00:30")
        run(cfg(self.d, pods=()), "2026-09-28 11:03:00")
        self.assertTrue((self.d / f"polaris-{A}.log").exists())
        self.assertEqual(summary(self.d, "20260928-10")["orphans_moved"], [])

    def test_an_orphan_quiet_less_than_120_s_is_left_alone(self):
        current(self.d, A, [line("2026-09-28 10:59:00", A)], "2026-09-28 11:02:30")
        run(cfg(self.d, pods=()), "2026-09-28 11:03:00")
        self.assertTrue((self.d / f"polaris-{A}.log").exists())

    def test_a_failed_pod_list_moves_no_file_but_still_publishes(self):
        current(self.d, A, [line("2026-09-28 10:48:00", A)], "2026-09-28 10:48:00")

        def broken():
            raise ConnectionError("apiserver unreachable")

        run(cfg(self.d, pods=None, lister=broken), "2026-09-28 11:03:00")
        s = summary(self.d, "20260928-10")
        self.assertEqual(s["processed"], 1)
        self.assertIsNone(s["orphans_moved"])
        self.assertIn("apiserver unreachable", s["pod_list_error"])
        self.assertTrue((self.d / f"polaris-{A}.log").exists())

    def test_an_orphan_cut_mid_write_sends_its_tail_to_malformed(self):
        data = line("2026-09-28 10:40:00", A) + '{"timestamp":"2026-09-28T10:41:0'
        put(self.d / f"polaris-{A}.log", data, "2026-09-28 10:41:00")
        run(cfg(self.d, pods=()), "2026-09-28 11:03:00")
        bad = jl(self.d / "malformed/20260928-10.jsonl")
        self.assertEqual(len(bad), 1)
        self.assertIn("orphaned", bad[0]["reason"])
        s = summary(self.d, "20260928-10")
        self.assertEqual((s["processed"], s["malformed"]), (1, 1))
        self.assertTrue(s["orphans_moved"][0]["unterminated_tail"])

    def test_an_idle_live_pod_rolling_an_old_hour_later_is_not_counted_twice(self):
        d = self.d
        current(d, A, [line("2026-09-28 10:40:00", A, "ten")], "2026-09-28 10:40:00")
        run(cfg(d), "2026-09-28 11:03:00")
        run(cfg(d), "2026-09-28 12:03:00")
        os.remove(d / f"polaris-{A}.log")
        roll(d, A, "2026-09-28-10", [line("2026-09-28 10:40:00", A, "ten")],
             "2026-09-28 12:20:00")
        current(d, A, [line("2026-09-28 12:20:00", A, "twelve")], "2026-09-28 12:20:00")
        run(cfg(d), "2026-09-28 13:03:00")
        self.assertEqual([x["message"] for x in jl(d / "processed-logs/20260928-12.jsonl")],
                         ["twelve"])
        self.assertEqual([x["message"] for x in jl(d / "processed-logs/20260928-10.jsonl")],
                         ["ten"])
        self.assertTrue((d / "done/20260928" / f"polaris-{A}.log.2026-09-28-10.gz").exists())

    def test_an_idle_listed_pod_is_reported_not_moved(self):
        current(self.d, A, [line("2026-09-28 09:40:00", A)], "2026-09-28 09:40:00")
        current(self.d, B, [line("2026-09-28 10:10:00", B)], "2026-09-28 10:10:00")
        run(cfg(self.d), "2026-09-28 11:03:00")
        s = summary(self.d, "20260928-10")
        self.assertEqual([i["pod"] for i in s["idle_log_files"]], [A])
        self.assertTrue((self.d / f"polaris-{A}.log").exists())


class ExactlyOnce(Case):
    def test_a_crash_before_the_checkpoint_redoes_the_hour_to_the_same_bytes(self):
        d = self.d
        current(d, A, [line("2026-09-28 10:10:00", A), line("2026-09-28 10:20:00", A)],
                "2026-09-28 10:20:00")
        with mock.patch.object(plb, "save_checkpoint", side_effect=RuntimeError("killed")):
            with self.assertRaises(RuntimeError):
                run(cfg(d), "2026-09-28 11:03:00")
        first = (d / "processed-logs/20260928-10.jsonl").read_bytes()
        first_summary = (d / "aggregated-logs/20260928-10.jsonl").read_bytes()
        self.assertIsNone(plb.load_checkpoint(str(d))["last_published"])
        r = run(cfg(d), "2026-09-28 11:05:00")
        self.assertEqual(r["published"], ["20260928-10"])
        self.assertEqual((d / "processed-logs/20260928-10.jsonl").read_bytes(), first)
        self.assertNotEqual(first_summary, b"")
        self.assertEqual(plb.load_checkpoint(str(d))["last_published"], "20260928-10")

    def test_a_published_hour_is_never_redone(self):
        current(self.d, A, [line("2026-09-28 10:10:00", A)], "2026-09-28 10:10:00")
        run(cfg(self.d), "2026-09-28 11:03:00")
        self.assertEqual(run(cfg(self.d), "2026-09-28 11:30:00")["published"], [])

    def test_catch_up_is_bounded_per_run(self):
        current(self.d, A, [line("2026-09-28 01:10:00", A)], "2026-09-28 01:10:00")
        r = run(cfg(self.d, max_hours=3), "2026-09-28 11:03:00")
        self.assertEqual(r["published"], ["20260928-01", "20260928-02", "20260928-03"])
        r = run(cfg(self.d, max_hours=100), "2026-09-28 11:03:00")
        self.assertEqual(r["published"][-1], "20260928-10")
        self.assertEqual(len(r["published"]), 7)

    def test_an_hour_is_not_published_before_the_grace(self):
        current(self.d, A, [line("2026-09-28 10:10:00", A)], "2026-09-28 10:10:00")
        self.assertEqual(run(cfg(self.d), "2026-09-28 11:00:30")["published"], [])
        self.assertEqual(run(cfg(self.d), "2026-09-28 11:01:30")["published"], ["20260928-10"])

    def test_a_held_lock_skips_the_run(self):
        plb.ensure_dirs(str(self.d))
        with open(self.d / ".state/lock", "w") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            r = run(cfg(self.d), "2026-09-28 11:03:00")
        self.assertEqual(r["status"], "locked")

    def test_a_dry_run_writes_nothing(self):
        current(self.d, A, [line("2026-09-28 10:10:00", A)], "2026-09-28 10:10:00")
        r = run(cfg(self.d, dry_run=True), "2026-09-28 11:03:00")
        self.assertEqual(r["published"], ["20260928-10"])
        self.assertEqual(list((self.d / "processed-logs").iterdir()), [])
        self.assertIsNone(plb.load_checkpoint(str(self.d))["last_published"])

    def test_the_aggregated_file_is_written_last(self):
        current(self.d, A, [line("2026-09-28 10:10:00", A), "bad\n"], "2026-09-28 10:10:00")
        order = []
        real = plb.atomic_write

        def spy(log_dir, dest, data):
            order.append(os.path.basename(os.path.dirname(dest)))
            return real(log_dir, dest, data)

        with mock.patch.object(plb, "atomic_write", side_effect=spy):
            run(cfg(self.d), "2026-09-28 11:03:00")
        hour_files = [o for o in order if o != ".state"]
        self.assertEqual(hour_files[-1], "aggregated-logs")
        self.assertEqual(sorted(hour_files),
                         ["aggregated-logs", "malformed", "processed-logs"])


class TheSummaryRow(Case):
    def test_contract_v2_summary_fields(self):
        current(self.d, A, [line("2026-09-28 10:10:00", A)], "2026-09-28 10:10:00")
        run(cfg(self.d), "2026-09-28 11:03:00")
        s = summary(self.d, "20260928-10")
        self.assertEqual(s["contract_version"], 2)
        self.assertEqual(s["report_type"], "summary")
        self.assertEqual((s["window_start"], s["window_end"], s["window_seconds"]),
                         ("2026-09-28T10:00:00+00:00", "2026-09-28T11:00:00+00:00", 3600))
        self.assertEqual(s["hour"], "20260928-10")
        self.assertEqual(s["pods"], [A])
        self.assertEqual(s["published_at"], "2026-09-28T11:03:00+00:00")


class Retention(Case):
    def test_outputs_and_done_older_than_three_days_are_deleted(self):
        d = self.d
        plb.ensure_dirs(str(d))
        old = put(d / "processed-logs/20260924-10.jsonl", "{}\n", "2026-09-24 11:03:00")
        (d / "done/20260924").mkdir(parents=True)
        old_done = put(d / "done/20260924/x.gz", b"x", "2026-09-24 11:03:00")
        current(d, A, [line("2026-09-28 10:10:00", A)], "2026-09-28 10:10:00")
        run(cfg(d), "2026-09-28 11:03:00")
        self.assertFalse(old.exists())
        self.assertFalse(old_done.exists())
        self.assertFalse((d / "done/20260924").exists())
        self.assertTrue((d / "processed-logs/20260928-10.jsonl").exists())


class CommandLine(unittest.TestCase):
    def test_now_and_the_pod_selector_come_from_arguments_or_the_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = pathlib.Path(tmp)
            current(d, A, [line("2026-09-28 10:10:00", A)], "2026-09-28 10:10:00")
            out = io.StringIO()
            with mock.patch.object(sys, "stdout", out):
                code = plb.main(["--log-dir", tmp, "--no-pod-list",
                                 "--now", "2026-09-28T11:03:00Z"])
            self.assertEqual(code, 0)
            self.assertTrue((d / "processed-logs/20260928-10.jsonl").exists())
            events = [json.loads(x)["event"] for x in out.getvalue().splitlines()]
            self.assertIn("published", events)

    def test_an_unparseable_now_is_refused(self):
        with self.assertRaises(SystemExit):
            plb.main(["--log-dir", "/nonexistent", "--no-pod-list", "--now", "yesterday"])


if __name__ == "__main__":
    unittest.main()
