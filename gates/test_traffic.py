"""Tests for traffic.py. Run: python3 -m unittest discover -s gates"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import traffic  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
OPA = {"proto": "http", "listener": "opa", "method": "POST", "status": 200,
       "request_headers": {"content-type": "application/json"},
       "request_body": {"json": {"input": {"actor": {"principal": "root"},
                                           "action": "CREATE_CATALOG"}}},
       "response_body": {"json": {"result": {"allow": True}}}}
S3 = {"proto": "http", "listener": "s3", "method": "PUT", "path": "/polaris-warehouse/k",
      "status": 403, "request_headers": {"authorization": "<redacted>"}}
LOG = "\n".join([json.dumps({"event": "listening"}), json.dumps(OPA), "not json",
                 json.dumps(S3)])


def expectation(must, **match):
    return {"name": f"{must}: {sorted(match)}", "must": must, "match": match}


class Records(unittest.TestCase):
    def test_json_lines_are_records_and_other_lines_are_skipped(self):
        self.assertEqual(traffic.records(LOG), [{"event": "listening"}, OPA, S3])


class Match(unittest.TestCase):
    def test_a_dotted_path_reaches_into_bodies(self):
        self.assertTrue(traffic.matches(OPA, {
            "request_body.json.input.actor.principal": "root",
            "response_body.json.result.allow": True}))

    def test_every_condition_must_hold(self):
        self.assertFalse(traffic.matches(OPA, {"listener": "opa", "status": 500}))

    def test_a_regex_matches_strings_only(self):
        self.assertTrue(traffic.matches(S3, {"path": {"regex": "^/polaris-warehouse/"}}))
        self.assertFalse(traffic.matches(S3, {"status": {"regex": "403"}}))

    def test_present_tells_a_missing_field_from_a_redacted_one(self):
        self.assertTrue(traffic.matches(S3, {"request_headers.authorization": {"present": True}}))
        self.assertTrue(traffic.matches(OPA, {"request_headers.authorization":
                                              {"present": False}}))

    def test_true_is_not_one(self):
        self.assertFalse(traffic.matches({"rows": 1}, {"rows": True}))


class Check(unittest.TestCase):
    def test_an_exchange_that_must_exist_and_does_passes_with_its_count(self):
        problems, lines = traffic.check(traffic.records(LOG), [
            expectation("exist", listener="opa")])
        self.assertEqual(problems, [])
        self.assertIn("ok   exist", lines[0])
        self.assertIn("1 matching", lines[0])

    def test_an_exchange_that_must_exist_and_does_not_fails(self):
        problems, lines = traffic.check(traffic.records(LOG), [
            expectation("exist", listener="pg")])
        self.assertEqual(len(problems), 1)
        self.assertTrue(lines[0].startswith("FAIL"))

    def test_an_exchange_that_must_not_exist_and_does_fails(self):
        problems, _ = traffic.check(traffic.records(LOG), [
            expectation("not_exist", listener="s3", status=403,
                        **{"request_headers.authorization": {"present": True}})])
        self.assertEqual(len(problems), 1)

    def test_the_output_never_holds_a_body(self):
        _, lines = traffic.check(traffic.records(LOG), [expectation("exist", listener="opa")])
        self.assertNotIn("CREATE_CATALOG", "\n".join(lines))

    # Smoke 38217979245 found 2 failed statements and could say nothing more: a failed
    # must-not now names what matched, by fields that hold no body, statement or message.
    def test_a_failed_must_not_names_each_matching_record_by_its_safe_fields(self):
        failed = {"proto": "pg", "listener": "pg", "event": "statement",
                  "time": "2026-10-12T07:33:01.5+00:00", "user": "polaris",
                  "database": "polaris", "sql": "SELECT secret_column FROM t",
                  "error": {"severity": "FATAL", "sqlstate": "57P01",
                            "message": "terminating connection due to administrator command"}}
        problems, lines = traffic.check([failed, OPA], [
            expectation("not_exist", event="statement",
                        **{"error.sqlstate": {"regex": "^(?!42P01$)"}})])
        self.assertEqual(len(problems), 1)
        self.assertEqual(lines[1], "     time=2026-10-12T07:33:01.5+00:00 proto=pg listener=pg "
                                   "event=statement user=polaris database=polaris "
                                   "error.severity=FATAL error.sqlstate=57P01")
        text = "\n".join(lines)
        for unsafe in ("secret_column", "terminating", "SELECT"):
            self.assertNotIn(unsafe, text)

    def test_an_http_record_is_named_without_its_path_or_bodies(self):
        _, lines = traffic.check(traffic.records(LOG), [
            expectation("not_exist", listener="s3")])
        self.assertEqual(lines[1], "     proto=http listener=s3 method=PUT status=403")

    def test_at_most_five_records_are_named_then_how_many_more(self):
        many = [{"proto": "pg", "event": "statement", "time": str(i)} for i in range(7)]
        _, lines = traffic.check(many, [expectation("not_exist", event="statement")])
        self.assertEqual(len(lines), 1 + 5 + 1)
        self.assertEqual(lines[-1], "     and 2 more")

    def test_a_passing_or_must_exist_expectation_names_no_record(self):
        _, lines = traffic.check(traffic.records(LOG), [
            expectation("exist", listener="opa"), expectation("exist", listener="pg")])
        self.assertEqual(len(lines), 2)

    # Kade, 2026-10-12: the upgrade that adds the plugin restarts polaris-pg; inside that window
    # only, a must-not may let through the records its `allow_during_restart` names.
    RESTART = ("2026-10-12T10:34:00+00:00", "2026-10-12T10:37:58+00:00")

    def restart_rule(self):
        return {"name": "no failed statement", "must": "not_exist",
                "match": {"event": "statement", "error.sqlstate": {"regex": "^(?!42P01$)"}},
                "allow_during_restart": {"error.sqlstate": "57P01", "error.severity": "FATAL"}}

    def statement(self, time, sqlstate="57P01", severity="FATAL"):
        return {"proto": "pg", "event": "statement", "time": time,
                "error": {"sqlstate": sqlstate, "severity": severity}}

    def test_an_allowed_record_inside_the_restart_passes_and_is_named_as_allowed(self):
        problems, lines = traffic.check([self.statement("2026-10-12T10:37:06.861+00:00")],
                                        [self.restart_rule()], restart=self.RESTART)
        self.assertEqual(problems, [])
        self.assertEqual(lines[0], "ok   no failed statement: 0 matching records, "
                                   "1 allowed during the restart")
        self.assertIn("error.sqlstate=57P01", lines[1])

    def test_the_same_record_outside_the_restart_fails(self):
        for time in ("2026-10-12T10:33:59.999+00:00", "2026-10-12T10:38:00+00:00"):
            with self.subTest(time=time):
                problems, _ = traffic.check([self.statement(time)], [self.restart_rule()],
                                            restart=self.RESTART)
                self.assertEqual(len(problems), 1)

    def test_without_a_restart_nothing_is_allowed(self):
        problems, _ = traffic.check([self.statement("2026-10-12T10:37:06.861+00:00")],
                                    [self.restart_rule()])
        self.assertEqual(len(problems), 1)

    def test_a_record_the_allowance_does_not_name_fails_inside_the_restart(self):
        problems, _ = traffic.check(
            [self.statement("2026-10-12T10:37:06+00:00", sqlstate="25006", severity="ERROR")],
            [self.restart_rule()], restart=self.RESTART)
        self.assertEqual(len(problems), 1)

    def test_only_a_must_not_may_carry_an_allowance(self):
        rule = {**self.restart_rule(), "must": "exist"}
        with self.assertRaises(ValueError):
            traffic.check([], [rule])

    def test_an_unknown_must_is_refused(self):
        with self.assertRaises(ValueError):
            traffic.check([], [expectation("maybe", listener="opa")])


class Committed(unittest.TestCase):
    def test_each_expectation_file_is_well_formed(self):
        directory = os.path.join(HERE, "expectations")
        files = sorted(f for f in os.listdir(directory) if f.endswith(".json"))
        self.assertIn("standins.json", files)
        for name in files:
            for e in traffic.load(os.path.join(directory, name)):
                with self.subTest(file=name, expectation=e.get("name")):
                    self.assertTrue(e["name"])
                    self.assertIn(e["must"], ("exist", "not_exist"))
                    self.assertTrue(e["match"])


class BootstrapProbes(unittest.TestCase):
    """standins.json's must-nots hold for every record, Polaris's bootstrap included."""

    def statement(self, sql, sqlstate):
        return {"proto": "pg", "event": "statement", "sql": sql,
                "error": {"severity": "ERROR", "sqlstate": sqlstate, "message": "m"}}

    def failed(self, *recorded):
        expectations = traffic.load(os.path.join(HERE, "expectations", "standins.json"))
        must_nots = [e for e in expectations if e["must"] == "not_exist"]
        problems, _ = traffic.check(list(recorded), must_nots)
        return problems

    def test_the_bootstraps_two_probes_of_an_empty_database_pass(self):
        self.assertEqual(self.failed(
            self.statement("SELECT version_value FROM POLARIS_SCHEMA.VERSION", "42P01"),
            self.statement("SELECT * FROM POLARIS_SCHEMA.ENTITIES LIMIT 1", "42P01")), [])

    def test_any_other_missing_table_fails(self):
        self.assertTrue(self.failed(
            self.statement("SELECT * FROM POLARIS_SCHEMA.GRANT_RECORDS", "42P01")))

    def test_any_other_error_fails_even_on_a_probe(self):
        self.assertTrue(self.failed(
            self.statement("SELECT version_value FROM POLARIS_SCHEMA.VERSION", "42501")))
        self.assertTrue(self.failed(self.statement("INSERT INTO entities VALUES (1)", "23505")))


class VendedCredentials(unittest.TestCase):
    """iceberg.json's must-nots hold for Polaris's policy requests, not check-standins' curl."""

    def delegation(self, user_agent):
        return {"proto": "http", "listener": "opa", "method": "POST", "status": 200,
                "request_headers": {"user-agent": user_agent},
                "request_body": {"json": {"input": {
                    "actor": {"principal": "gate_smoke"},
                    "action": "LOAD_TABLE_WITH_READ_DELEGATION"}}},
                "response_body": {"json": {"result": {"allow": False}}}}

    def failed(self, *recorded):
        expectations = traffic.load(os.path.join(HERE, "expectations", "iceberg.json"))
        must_nots = [e for e in expectations if e["must"] == "not_exist"]
        problems, _ = traffic.check(list(recorded), must_nots)
        return problems

    def test_check_standins_asking_with_curl_passes(self):
        self.assertEqual(self.failed(self.delegation("curl/8.14.1")), [])

    def test_polaris_asking_fails(self):
        self.assertTrue(self.failed(self.delegation("Apache-HttpClient/5.5 (Java/21.0.8)")))



class Maintainer(unittest.TestCase):
    """console.json's must-nots: gate_maintainer is never allowed anything but a read."""

    def decision(self, action, allow):
        return {"proto": "http", "listener": "opa", "method": "POST", "status": 200,
                "request_headers": {"user-agent": "Apache-HttpClient/5.5 (Java/21.0.8)"},
                "request_body": {"json": {"input": {"actor": {"principal": "gate_maintainer"},
                                                    "action": action}}},
                "response_body": {"json": {"result": {"allow": allow}}}}

    def failed(self, *recorded):
        expectations = traffic.load(os.path.join(HERE, "expectations", "console.json"))
        must_nots = [e for e in expectations if e["must"] == "not_exist"]
        problems, _ = traffic.check(list(recorded), must_nots)
        return problems

    def test_reads_allowed_and_a_write_refused_pass(self):
        self.assertEqual(self.failed(self.decision("LIST_CATALOGS", True),
                                     self.decision("LOAD_TABLE", True),
                                     self.decision("CREATE_CATALOG", False)), [])

    def test_a_write_allowed_fails(self):
        for action in ("CREATE_CATALOG", "DROP_TABLE_WITHOUT_PURGE",
                       "LOAD_TABLE_WITH_READ_DELEGATION", "LIST_CATALOGS_AND_MORE"):
            with self.subTest(action=action):
                self.assertTrue(self.failed(self.decision(action, True)))

if __name__ == "__main__":
    unittest.main()
