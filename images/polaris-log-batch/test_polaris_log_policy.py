"""The audit policy in polaris_log_batch.py: contract v2 (docs/contracts/polaris-logs.md, ADR-036).

Ported from the archive's tests/test_polaris_log_policy.py (enjay27/lakehouse-k8s at 09a4773),
moved to UTC and to unittest; the archive proved the policy identical to the Fluent Bit Lua filter
it was ported from (logging/scripts/step16-batch-lua-parity.py there). Each test writes Polaris-
shaped JSON lines (Quarkus access-log messages, allow-listed and other application loggers,
mdc.requestId) into a pod's current file, runs the batch, and reads both outputs back.

Run: python3 -m unittest discover -s images/polaris-log-batch
"""

import datetime as dt
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

POD = "polaris-7f4d69c67c-aaaaa"
ACCESS = plb.ACCESS_LOGGER
MAPPER = "org.apache.polaris.service.exception.IcebergExceptionMapper"
ADMIN = "org.apache.polaris.service.admin.PolarisServiceImpl"
CAT = "/api/catalog/v1/cat"
MGMT = "/api/management/v1"
BASE = ("contract_version", "report_type", "window_start", "window_end", "window_seconds")


def utc(text):
    return dt.datetime.fromisoformat(text).replace(tzinfo=dt.timezone.utc)


def rec(when, logger, message, ms=0, level="INFO", rid=None):
    r = {"timestamp": f"{utc(when):%Y-%m-%dT%H:%M:%S}.{ms:03d}000000Z", "sequence": 1,
         "loggerClassName": "org.jboss.logging.Logger", "loggerName": logger, "level": level,
         "message": message, "threadName": "executor-thread-1", "threadId": 42,
         "mdc": {"requestId": rid, "realmId": "POLARIS"} if rid else {}, "ndc": "",
         "hostName": POD, "processName": "/usr/lib/jvm/java-21/bin/java", "processId": 1}
    return json.dumps(r)


def acc(when, method, path, status, size=10, user="root", ms=0, rid=None, level="INFO"):
    msg = (f'10.0.0.7 - {user} [28/Sep/2026:10:00:00 +0000] "{method} {path} HTTP/1.1" '
           f"{status} {size}")
    return rec(when, ACCESS, msg, ms=ms, rid=rid, level=level)


def app(when, logger, message, ms=0, rid=None, level="INFO"):
    return rec(when, logger, message, ms=ms, rid=rid, level=level)


class Policy(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = pathlib.Path(self.tmp.name)

    def drive(self, lines, now="2026-09-28 11:03:00", mtime="2026-09-28 10:59:00"):
        path = self.d / f"polaris-{POD}.log"
        path.write_text("".join(x + "\n" for x in lines))
        os.utime(path, (utc(mtime).timestamp(), utc(mtime).timestamp()))
        c = plb.Config(str(self.d), pod_lister=lambda: {POD})
        return plb.run(c, now=utc(now), out=io.StringIO())

    def processed(self, key="20260928-10"):
        p = self.d / "processed-logs" / f"{key}.jsonl"
        return [json.loads(x) for x in p.read_text().splitlines()]

    def report(self, key="20260928-10"):
        rows = [json.loads(x) for x in
                (self.d / "aggregated-logs" / f"{key}.jsonl").read_text().splitlines()]
        s = rows[0]
        self.assertEqual((s["report_type"], s["contract_version"]), ("summary", 2))
        res = {r["resource"]: r for r in rows if r["report_type"] == "resource"}
        pri = {r["user_principal_name"]: r for r in rows if r["report_type"] == "principal"}
        drop = {r["logger_name"]: r for r in rows if r["report_type"] == "app_dropped"}
        return s, res, pri, drop

    # ------------------------------------------------------------------ rules 1-7

    def test_access_rules_keep_writes_and_failures_count_reads_and_catalog_posts(self):
        t = "2026-09-28 10:10:00"
        self.drive([
            acc(t, "GET", f"{CAT}/namespaces/ns/tables/t", 200, 900, ms=1),  # 6 counted
            acc(t, "GET", f"{CAT}/namespaces/ns/tables/t/metrics", 200, 5, ms=2),  # same row
            acc(t, "POST", f"{CAT}/namespaces/ns/tables/t", 200, 700, ms=3),  # 5 catalog: counted
            acc(t, "POST", f"{MGMT}/principals", 201, 300, ms=4),  # 5 management: kept
            acc(t, "DELETE", f"{MGMT}/principals/p1", 204, 0, ms=5),  # 4 kept
            acc(t, "PUT", f"{MGMT}/catalogs/cat/catalog-roles/cr/grants", 201, 0, ms=6),  # 4
            acc(t, "GET", f"{MGMT}/catalogs", 401, "-", user="-", ms=7),  # 3 kept
            acc(t, "GET", f"{CAT}/namespaces/ns/tables/missing", 404, 80, ms=8),  # 3' counted
            acc(t, "GET", f"{CAT}/namespaces/ns/tables/t", 200, 900, ms=9, level="WARN"),  # 1
            rec(t, ACCESS, "this is not the access-log pattern", ms=10),  # parse error: kept
        ])
        kept = self.processed()
        self.assertEqual([d.get("http_method") for d in kept],
                         ["POST", "DELETE", "PUT", "GET", "GET", None])
        self.assertIs(kept[-1]["access_log_parse_error"], True)
        self.assertEqual((kept[3]["http_status"], kept[3]["user_principal_name"],
                          kept[3]["response_size"]), (401, "-", 0))
        s, res, pri, _ = self.report()
        self.assertEqual((s["access_seen"], s["access_kept"], s["access_counted"]), (9, 5, 4))
        self.assertEqual((s["counted_read"], s["counted_post"], s["counted_404"]), (2, 1, 1))
        self.assertEqual((s["errors_kept"], s["parse_errors"], s["auth_denied"]), (2, 1, 1))
        row = res[f"{CAT}/namespaces/ns/tables/t"]
        self.assertEqual((row["requests"], row["reads"], row["writes"], row["resource_kind"]),
                         (3, 2, 1, "table"))
        self.assertEqual((row["last_read_bytes"], row["last_write_bytes"]), (5, 700))
        role = res[f"{MGMT}/catalogs/cat/catalog-roles/cr"]
        self.assertEqual((role["resource_kind"], role["writes"]), ("catalog-role", 1))
        # the 404 and the 401 had no successful row to join this hour
        self.assertEqual((res["__errors__"]["requests"], res["__errors__"]["auth_denied"]), (2, 1))
        self.assertEqual(set(pri), {"root", "-"})
        self.assertEqual(s["lines_in"], s["processed"] + s["dropped"] + s["malformed"])

    def test_kept_lines_lose_four_fields_and_carry_an_event_id(self):
        self.drive([acc("2026-09-28 10:10:00", "DELETE", f"{MGMT}/principals/p", 204)])
        (d,) = self.processed()
        for gone in ("processName", "loggerClassName", "processId", "ndc"):
            self.assertNotIn(gone, d)
        for kept in ("timestamp", "threadName", "threadId", "hostName", "message", "mdc"):
            self.assertIn(kept, d)
        self.assertEqual((len(d["event_id"]), d["log_hour"]), (40, "20260928-10"))

    # ------------------------------------------------------------------ allow-list and request IDs

    def test_a_404_drops_its_requests_app_lines_and_a_500_keeps_them(self):
        t = "2026-09-28 10:20:00"
        self.drive([
            app(t, MAPPER, "Handling runtimeException NoSuchTable", ms=1, rid="r404"),
            acc(t, "GET", f"{CAT}/namespaces/ns/tables/x", 404, ms=5, rid="r404"),
            app(t, MAPPER, "Handling runtimeException boom", ms=10, rid="r500"),
            acc(t, "POST", f"{CAT}/namespaces/ns/tables", 500, ms=15, rid="r500"),
        ])
        msgs = [d["message"] for d in self.processed()]
        self.assertIn("Handling runtimeException boom", msgs)
        self.assertNotIn("Handling runtimeException NoSuchTable", msgs)
        s, *_ = self.report()
        self.assertEqual((s["app_dropped_404"], s["counted_404"], s["errors_5xx"]), (1, 1, 1))

    def test_an_app_line_after_its_access_line_is_decided_by_it(self):
        t = "2026-09-28 10:20:00"
        self.drive([acc(t, "GET", f"{CAT}/namespaces/ns/tables/x", 404, ms=5, rid="late"),
                    app(t, ADMIN, "late reason", ms=9, rid="late")])
        self.assertEqual(self.processed(), [])
        self.assertEqual(self.report()[0]["app_dropped_404"], 1)

    def test_an_allow_listed_line_without_its_access_line_is_kept_as_a_held_orphan(self):
        self.drive([app("2026-09-28 10:30:00", ADMIN, "Granted X", rid="nobody-answered")])
        (d,) = self.processed()
        self.assertIs(d["held_orphan"], True)
        self.assertEqual(self.report()[0]["held_orphans"], 1)

    def test_an_allow_listed_line_without_a_request_id_is_kept(self):
        self.drive([app("2026-09-28 10:30:00", ADMIN, "no id")])
        (d,) = self.processed()
        self.assertNotIn("held_orphan", d)

    def test_the_request_id_match_crosses_the_hour_boundary(self):
        self.drive([
            app("2026-09-28 10:59:59", MAPPER, "reason at the edge", ms=995, rid="edge"),
            acc("2026-09-28 11:00:00", "GET", f"{CAT}/namespaces/ns/tables/x", 404, ms=5,
                rid="edge"),
        ], now="2026-09-28 12:03:00", mtime="2026-09-28 11:00:01")
        self.assertEqual(self.processed("20260928-10"), [])
        self.assertEqual(self.report("20260928-10")[0]["app_dropped_404"], 1)
        self.assertEqual(self.report("20260928-11")[0]["counted_404"], 1)

    def test_warn_and_error_are_always_kept_whatever_the_logger(self):
        self.drive([app("2026-09-28 10:30:00", "some.Other", "disk full", level="ERROR")])
        self.assertEqual([d["message"] for d in self.processed()], ["disk full"])

    # ------------------------------------------------------------------ app_dropped and commits

    def test_other_loggers_are_counted_per_logger_and_commits_harvested_first(self):
        t = "2026-09-28 10:40:00"
        self.drive([
            app(t, plb.COMMIT_LOGGER, "Successfully committed to table cat.ns.t in 20 ms", ms=1),
            app(t, plb.COMMIT_LOGGER, "Successfully committed to table cat.ns.t in 30 ms", ms=2),
            app(t, plb.COMMIT_LOGGER, "Successfully committed to view cat.a.b.v in 7 ms", ms=3),
            app(t, "org.apache.iceberg.CatalogUtil", "Loading custom FileIO", ms=4),
        ])
        self.assertEqual(self.processed(), [])
        s, res, _, drop = self.report()
        row = res[f"{CAT}/namespaces/ns/tables/t"]
        self.assertEqual((row["commit_count"], row["commit_ms_sum"], row["commit_ms_min"],
                          row["commit_ms_max"]), (2, 50, 20, 30))
        self.assertEqual(row["requests"], 0)  # commits never touch the access counters
        self.assertIn(f"{CAT}/namespaces/a%1Fb/views/v", res)  # nested namespace, Iceberg's %1F
        self.assertEqual((drop[plb.COMMIT_LOGGER]["dropped"],
                          drop["org.apache.iceberg.CatalogUtil"]["dropped"]), (3, 1))
        self.assertEqual((s["app_dropped_total"], s["distinct_resources"]), (4, 0))

    def test_polaris_1_6_0s_commit_lines_are_harvested(self):
        # The logger and messages Polaris 1.6.0 writes for a table create, then a commit to it
        # (measured on 2026-10-11, the image pinned in charts/polaris/values-lakehouse.yaml).
        t, logger = "2026-09-28 10:40:00", \
            "org.apache.polaris.service.catalog.iceberg.LocalIcebergCatalog"
        self.drive([
            app(t, logger, "Successfully committed to table gate_logs.logs.t1 in 311 ms", ms=1),
            app(t, logger, "Refreshing table metadata from new version: file:/wh/gate_logs/logs/"
                           "t1/metadata/00000-9cab8529.metadata.json", ms=2),
            app(t, logger, "Successfully committed to table gate_logs.logs.t1 in 25 ms", ms=3),
        ])
        s, res, _, drop = self.report()
        row = res["/api/catalog/v1/gate_logs/namespaces/logs/tables/t1"]
        self.assertEqual((row["commit_count"], row["commit_ms_sum"], row["commit_ms_min"],
                          row["commit_ms_max"]), (2, 336, 25, 311))
        self.assertEqual(drop[logger]["dropped"], 3)
        self.assertEqual(s["app_dropped_total"], 3)

    # ------------------------------------------------------------------ rows

    def test_an_error_joins_its_row_if_the_resource_succeeds_anywhere_in_the_hour(self):
        self.drive([acc("2026-09-28 10:05:00", "GET", f"{CAT}/namespaces/ns/tables/t", 403),
                    acc("2026-09-28 10:10:00", "GET", f"{CAT}/namespaces/ns/tables/t", 200)])
        _, res, *_ = self.report()
        self.assertNotIn("__errors__", res)
        row = res[f"{CAT}/namespaces/ns/tables/t"]
        self.assertEqual((row["requests"], row["errors"], row["auth_denied"]), (2, 1, 1))

    def test_a_denied_grant_creates_its_role_row(self):
        self.drive([acc("2026-09-28 10:05:00", "PUT",
                        f"{MGMT}/catalogs/c/catalog-roles/crX/grants", 403)])
        s, res, *_ = self.report()
        self.assertEqual(res[f"{MGMT}/catalogs/c/catalog-roles/crX"]["auth_denied"], 1)
        self.assertEqual(s["role_keys_forced"], 1)

    def test_principal_rows_are_capped_into_other(self):
        with mock.patch.object(plb, "REPORT_MAX_PRINCIPALS", 2):
            self.drive([acc("2026-09-28 10:05:00", "GET", f"{CAT}/namespaces", 200, user=u, ms=i)
                        for i, u in enumerate("abc")])
        s, _, pri, _ = self.report()
        self.assertEqual((set(pri), s["principals_other"]), ({"a", "b", "__other__"}, 1))

    # ------------------------------------------------------------------ credential guard

    def test_an_unmasked_client_secret_is_redacted_before_it_is_written(self):
        t = "2026-09-28 10:05:00"
        # Polaris's generated models print as "class X {\n    field: value\n}"; the value is the
        # next run of non-space characters
        self.drive([
            app(t, ADMIN, "Created new principal class C {\n    clientId: abc\n"
                          "    clientSecret: s3cr3t\n}", ms=1),
            app(t, ADMIN, "Created new principal class C {\n    clientId: def\n"
                          "    clientSecret: *\n}", ms=2),
        ])
        a, b = self.processed()
        self.assertNotIn("s3cr3t", a["message"])
        self.assertIn("<redacted>", a["message"])
        self.assertIs(a["secret_redacted"], True)
        self.assertIn("clientSecret: *", b["message"])
        self.assertNotIn("secret_redacted", b)
        self.assertNotIn("s3cr3t", (self.d / "processed-logs/20260928-10.jsonl").read_text())

    # ------------------------------------------------------------------ accounting

    def test_every_line_is_kept_dropped_or_malformed_exactly_once(self):
        t = "2026-09-28 10:10:00"
        self.drive([
            acc(t, "GET", f"{CAT}/namespaces", 200, ms=1),
            acc(t, "POST", f"{MGMT}/principals", 201, ms=2),
            app(t, "x.Y", "noise", ms=3),
            app(t, MAPPER, "reason", ms=4, rid="q"),
            acc(t, "GET", f"{CAT}/namespaces/n", 404, ms=5, rid="q"),
            "not json at all",
        ])
        s, *_ = self.report()
        self.assertEqual(s["lines_in"], 6)
        self.assertEqual((s["processed"], s["dropped"], s["malformed"]), (1, 4, 1))
        self.assertEqual(s["dropped"],
                         s["access_counted"] + s["app_dropped_total"] + s["app_dropped_404"])

    def test_an_hour_whose_books_do_not_balance_is_not_published(self):
        lines = [acc("2026-09-28 10:10:00", "DELETE", f"{MGMT}/principals/p", 204)]
        with mock.patch.object(plb.AuditPolicy, "_decide", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "accounting"):
                self.drive(lines)
        self.assertFalse((self.d / "aggregated-logs/20260928-10.jsonl").exists())
        self.assertFalse((self.d / "processed-logs/20260928-10.jsonl").exists())

    # ------------------------------------------------------------------ contract v2 rows

    def test_every_row_carries_the_contract_and_the_hour_window_and_no_time_field(self):
        t = "2026-09-28 10:10:00"
        self.drive([acc(t, "GET", f"{MGMT}/catalogs", 200, ms=1),
                    app(t, "x.Y", "noise", ms=2)])
        rows = [json.loads(x) for x in
                (self.d / "aggregated-logs/20260928-10.jsonl").read_text().splitlines()]
        self.assertEqual([r["report_type"] for r in rows],
                         ["summary", "resource", "principal", "app_dropped"])
        for r in rows:
            self.assertEqual([r[k] for k in BASE],
                             [2, r["report_type"], "2026-09-28T10:00:00+00:00",
                              "2026-09-28T11:00:00+00:00", 3600])
            self.assertNotIn("_time", r)
            self.assertNotIn("schema_version", r)

    def test_the_summary_keeps_the_batch_fields_of_v1_and_adds_the_policys(self):
        self.drive([acc("2026-09-28 10:10:00", "GET", f"{MGMT}/catalogs", 200)])
        s, *_ = self.report()
        for k in ("hour", "lines_in", "processed", "malformed", "pods", "by_pod", "sources",
                  "corrupt_files", "orphans_moved", "idle_log_files", "pod_list_error",
                  "published_at", "dropped", "access_seen", "message", "min_record_time"):
            self.assertIn(k, s)
        self.assertEqual((s["hour"], s["policy"], s["pods"]), ("20260928-10", "policy-v5", [POD]))


if __name__ == "__main__":
    unittest.main()
