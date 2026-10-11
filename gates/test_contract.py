"""gates/contract.py: checks lines against the log contract's JSON schemas (ADR-024, ADR-036).

The processed lines below are lines Polaris 1.6.0 wrote (apache/polaris:1.6.0 at the digest
in charts/polaris/values-lakehouse.yaml, run locally with JSON file logging on 2026-10-11),
each with the event_id and log_hour the batch adds.
"""

import io
import json
import os
import pathlib
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "images", "polaris-log-batch"))
import contract  # noqa: E402
import polaris_log_batch as plb  # noqa: E402

ACCESS = ('{"timestamp":"2026-10-11T12:40:47.753600155Z","sequence":1770,'
          '"loggerClassName":"org.jboss.logging.Logger","loggerName":"io.quarkus.http.access-log",'
          '"level":"INFO","message":"172.17.0.1 - root [11/Oct/2026:12:40:47 +0000] '
          '\\"GET /api/management/v1/catalogs HTTP/1.1\\" 200 15","threadName":"executor-thread-2",'
          '"threadId":31,"mdc":{"requestId":"gate-req-0001","realmId":"POLARIS"},"ndc":"",'
          '"hostName":"polaris-7f4d69c67c-aaaaa","processName":"/usr/lib/jvm/java/bin/java",'
          '"processId":1}')
STARTUP = ('{"timestamp":"2026-10-11T12:40:38.429922859Z","sequence":1754,'
           '"loggerClassName":"org.slf4j.impl.Slf4jLogger",'
           '"loggerName":"org.apache.polaris.persistence.nosql.impl.cache.CaffeineCacheBackend",'
           '"level":"INFO","message":"Initialized persistence cache","threadName":"main",'
           '"threadId":1,"mdc":{},"ndc":"","hostName":"polaris-7f4d69c67c-aaaaa",'
           '"processName":"/usr/lib/jvm/java/bin/java","processId":1}')


def processed(raw, hour="20261011-12"):
    return plb.processed_doc(json.loads(raw), raw.encode(), hour)


class Processed(unittest.TestCase):
    def setUp(self):
        self.schema = contract.load("processed")

    def test_lines_polaris_wrote_pass(self):
        for raw in (ACCESS, STARTUP):
            self.assertEqual(contract.problems(processed(raw), self.schema), [])

    def test_a_field_polaris_writes_must_be_there(self):
        doc = processed(ACCESS)
        del doc["mdc"]
        self.assertEqual(contract.problems(doc, self.schema), ["mdc: required"])

    def test_types_are_checked(self):
        doc = processed(ACCESS)
        doc["sequence"] = "1770"
        doc["mdc"] = []
        self.assertEqual(sorted(contract.problems(doc, self.schema)),
                         ["mdc: expected object", "sequence: expected integer"])

    def test_event_id_and_log_hour_have_their_shape(self):
        doc = processed(ACCESS)
        doc["event_id"] = "not-a-sha1"
        doc["log_hour"] = "2026-10-11-12"
        self.assertEqual(sorted(contract.problems(doc, self.schema)),
                         ["event_id: does not match ^[0-9a-f]{40}$",
                          "log_hour: does not match ^[0-9]{8}-[0-9]{2}$"])

    def test_the_timestamp_is_utc_with_a_z(self):
        doc = processed(ACCESS)
        doc["timestamp"] = "2026-10-11T21:40:47.753600155+09:00"
        self.assertEqual(len(contract.problems(doc, self.schema)), 1)

    def test_fields_polaris_adds_later_are_allowed(self):
        doc = processed(ACCESS)
        doc["exception"] = {"exceptionType": "java.lang.RuntimeException"}
        self.assertEqual(contract.problems(doc, self.schema), [])

    def test_a_boolean_is_not_an_integer(self):
        doc = processed(ACCESS)
        doc["processId"] = True
        self.assertEqual(contract.problems(doc, self.schema), ["processId: expected integer"])


class Summary(unittest.TestCase):
    def test_the_batch_summary_row_passes_and_nothing_else_does(self):
        schema = contract.load("summary")
        with tempfile.TemporaryDirectory() as tmp:
            d = pathlib.Path(tmp)
            (d / "polaris-polaris-7f4d69c67c-aaaaa.log").write_text(
                ACCESS + "\n" + "garbage\n")
            when = plb.parse_ts("2026-10-11T12:41:00Z").timestamp()
            os.utime(d / "polaris-polaris-7f4d69c67c-aaaaa.log", (when, when))
            plb.run(plb.Config(tmp, pod_lister=lambda: {"polaris-7f4d69c67c-aaaaa"}),
                    now=plb.parse_ts("2026-10-11T13:03:00Z"), out=io.StringIO())
            [row] = [json.loads(x) for x in
                     (d / "aggregated-logs/20261011-12.jsonl").read_text().splitlines()]
            [doc] = [json.loads(x) for x in
                     (d / "processed-logs/20261011-12.jsonl").read_text().splitlines()]
        self.assertEqual(contract.problems(row, schema), [])
        self.assertEqual(contract.problems(doc, contract.load("processed")), [])
        row["extra"] = 1
        self.assertEqual(contract.problems(row, schema), ["extra: not allowed"])

    def test_the_contract_version_is_fixed(self):
        schema = contract.load("summary")
        self.assertEqual(schema["properties"]["contract_version"], {"const": 1})


class Validator(unittest.TestCase):
    def test_nested_objects_arrays_and_nullable_fields(self):
        schema = {"type": "object", "required": ["a"], "additionalProperties": False,
                  "properties": {"a": {"type": "array",
                                       "items": {"type": "object", "required": ["n"],
                                                 "properties": {"n": {"type": "integer",
                                                                      "minimum": 0}}}},
                                 "b": {"type": ["string", "null"]}}}
        self.assertEqual(contract.problems({"a": [{"n": 1}], "b": None}, schema), [])
        self.assertEqual(contract.problems({"a": [{"n": -1}, {}], "b": 2}, schema),
                         ["a[0].n: below 0", "a[1].n: required", "b: expected string or null"])

    def test_an_unknown_keyword_is_refused_rather_than_ignored(self):
        with self.assertRaises(ValueError):
            contract.problems("x", {"type": "string", "maxLength": 3})

    def test_lines_reports_the_line_number_and_bad_json(self):
        schema = contract.load("processed")
        text = json.dumps(processed(ACCESS)) + "\nnot json\n"
        self.assertEqual(contract.check_lines(text, schema),
                         (1, ["line 2: not JSON"]))


if __name__ == "__main__":
    unittest.main()
