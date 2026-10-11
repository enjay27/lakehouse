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


if __name__ == "__main__":
    unittest.main()
