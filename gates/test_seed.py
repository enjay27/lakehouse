"""Tests for seed.py (ADR-051 §2, §3), with Polaris's APIs and PyIceberg faked: PyIceberg is
installed only in the seed pod. Run: python3 -m unittest discover -s gates"""

import io
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import seed  # noqa: E402

POLARIS = "http://polaris:8181"
ENV = {"ROOT_CLIENT_ID": "rootid0123456789", "ROOT_CLIENT_SECRET": "RootSecret0123456789",
       "S3_ACCESS_KEY_ID": "smokekey01234567", "S3_SECRET_ACCESS_KEY": "SmokeSecret0123456789"}
ARGS = [POLARIS, "POLARIS", "http://recorder.standins.svc.cluster.local:8333", "us-east-1",
        "polaris-warehouse"]
M, C, P = seed.MANAGEMENT, seed.ICEBERG, seed.POLICIES


def secret(name):
    return f"{name}-secret0123456789"


class FakePolaris:
    """Polaris's token endpoint and APIs: every write answers 2xx, every read the answer set
    for its path, or one made from the path."""

    def __init__(self, answers=None, fail=None, tokens_refused=()):
        self.calls, self.answers = [], dict(answers or {})
        self.fail, self.tokens_refused = fail, set(tokens_refused)

    def __call__(self, method, url, headers, body=None, form=None):
        path = url[len(POLARIS):]
        self.calls.append((method, path, headers, body, form))
        if path.endswith("/oauth/tokens"):
            if form["client_id"] in self.tokens_refused:
                return 401, {}
            return 200, {"access_token": f"token-{form['client_id']}"}
        if self.fail and self.fail == (method, path):
            return 500, {}
        if method == "GET":
            if path in self.answers:
                return self.answers[path]
            answer = {"name": path.rsplit("/", 1)[-1], "entityVersion": 1, "path": path}
            if "/tables/" in path or "/views/" in path:
                answer = {"metadata-location": f"s3://b{path}.json",
                          "metadata": {"location": path, "snapshots": [{"id": 1}, {"id": 2}]},
                          "config": {"token": "ignored"}}
            return 200, answer
        if method == "POST" and path == f"{M}/principals":
            name = body["principal"]["name"]
            return 201, {"principal": {"name": name},
                         "credentials": {"clientId": f"{name}-id", "clientSecret": secret(name)}}
        if method == "POST" and path == f"{M}/catalogs":
            return 201, {}
        return (200 if method == "POST" else 201), {}

    def writes(self):
        return [(m, p, b) for m, p, _, b, _ in self.calls
                if m in ("POST", "PUT") and not p.endswith("/oauth/tokens")]


class FakeTable:
    def __init__(self, name):
        self.name, self.columns, self.snapshots = name, ["id", "name"], []


class FakeCatalog:
    def __init__(self, iceberg, properties):
        self.iceberg, self.properties = iceberg, properties
        self.principal = properties["credential"].split("-id:")[0]

    def create_table(self, identifier, schema):
        self.iceberg.log.append((self.principal, "create_table", identifier))
        table = self.iceberg.tables[identifier] = FakeTable(identifier)
        return table

    def load_table(self, identifier):
        self.iceberg.log.append((self.principal, "load_table", identifier))
        return self.iceberg.tables[identifier]


class FakeIceberg:
    """Each append makes a snapshot holding every row so far, as Iceberg's appends do."""

    schema = "schema"

    def __init__(self, tables=None, append_fails=False):
        self.tables, self.log, self.catalogs = tables or {}, [], []
        self.append_fails = append_fails

    def catalog(self, properties):
        self.catalogs.append(properties)
        return FakeCatalog(self, properties)

    def append(self, table, rows):
        if self.append_fails:
            raise RuntimeError("append refused")
        for row in rows:
            self.assertable_columns(table, row)
        previous = table.snapshots[-1][1] if table.snapshots else []
        table.snapshots.append((100 + len(table.snapshots), previous + list(rows)))
        self.log.append(("append", table.name, len(rows)))

    @staticmethod
    def assertable_columns(table, row):
        if set(row) - set(table.columns):
            raise ValueError(f"{sorted(set(row) - set(table.columns))} not in the schema")

    def add_column(self, table, name):
        table.columns.append(name)
        self.log.append(("add_column", table.name, name))
        return table

    def snapshot_ids(self, table):
        return [s for s, _ in table.snapshots]

    def rows_at(self, table, snapshot_id):
        return next(rows for s, rows in table.snapshots if s == snapshot_id)

    def rows(self, table):
        return table.snapshots[-1][1]

    def create_view(self, catalog, identifier, sql):
        self.log.append((catalog.principal, "create_view", identifier, sql))


def run_seed(http=None, iceberg=None):
    out = io.StringIO()
    http, iceberg = http or FakePolaris(), iceberg or FakeIceberg()
    code, record = seed.run(["seed", *ARGS], ENV, http=http, iceberg=iceberg, out=out)
    return code, record, out.getvalue(), http, iceberg


def run_check(record, http=None, iceberg=None):
    out = io.StringIO()
    code, _ = seed.run(["check", *ARGS], ENV, http=http or FakePolaris(),
                       iceberg=iceberg, out=out, stdin=io.StringIO(json.dumps(record)))
    return code, out.getvalue()


class Compare(unittest.TestCase):
    def test_equal_objects_have_no_problem_and_nothing_added(self):
        obj = {"a": 1, "b": {"c": [1, 2], "d": None}}
        self.assertEqual(seed.compare(obj, json.loads(json.dumps(obj))), ([], []))

    def test_a_missing_field_and_a_changed_value_are_problems(self):
        problems, added = seed.compare({"a": 1, "b": {"c": "x", "d": 2}},
                                       {"a": 2, "b": {"c": "x"}}, "/p")
        self.assertEqual(problems, ["/p.a: 1 recorded, 2 now", "/p.b.d: missing"])
        self.assertEqual(added, [])

    def test_a_field_the_new_version_adds_is_allowed_and_listed(self):
        problems, added = seed.compare({"a": 1}, {"a": 1, "b": {"c": 2}}, "/p")
        self.assertEqual((problems, added), ([], ["/p.b"]))

    def test_lists_match_in_any_order_and_their_items_may_gain_fields(self):
        problems, added = seed.compare([{"id": 1}, {"id": 2}],
                                       [{"id": 2, "new": True}, {"id": 1}], "/p")
        self.assertEqual(problems, [])
        self.assertEqual(added, ["/p[1].new"])

    def test_a_recorded_list_item_with_no_match_is_a_problem_and_a_new_item_is_listed(self):
        problems, added = seed.compare([{"id": 1}, {"id": 2}], [{"id": 1}, {"id": 3}], "/p")
        self.assertEqual(problems, ['/p[1]: no item matches {"id": 2}'])
        self.assertEqual(added, ["/p[+1 item]"])

    def test_a_type_change_is_a_problem(self):
        problems, _ = seed.compare({"a": {"b": 1}}, {"a": [1]}, "/p")
        self.assertEqual(problems, ['/p.a: {"b": 1} recorded, [1] now'])


class Seed(unittest.TestCase):
    def test_root_makes_the_catalog_the_principals_their_roles_and_the_grants(self):
        code, _, out, http, _ = run_seed()
        self.assertEqual(code, 0, out)
        writes = http.writes()
        catalog = next(b for m, p, b in writes if p == f"{M}/catalogs")
        self.assertEqual(catalog["catalog"]["name"], "gate_seed_catalog")
        self.assertEqual(catalog["catalog"]["storageConfigInfo"]["allowedLocations"],
                         ["s3://polaris-warehouse/gate_seed_catalog"])
        self.assertEqual([b["principal"]["name"] for m, p, b in writes
                          if p == f"{M}/principals"], ["gate_seed", "gate_seed_reader"])
        self.assertIn(("PUT", f"{M}/principals/gate_seed/principal-roles",
                       {"principalRole": {"name": "seed_writer"}}), writes)
        self.assertIn(("PUT", f"{M}/principals/gate_seed_reader/principal-roles",
                       {"principalRole": {"name": "seed_reader"}}), writes)
        self.assertIn(("PUT", f"{M}/principal-roles/seed_reader/catalog-roles/gate_seed_catalog",
                       {"catalogRole": {"name": "seed_viewer"}}), writes)
        grants = [b["grant"] for m, p, b in writes if p.endswith("/grants")]
        self.assertEqual({g["type"] for g in grants}, {"catalog", "namespace", "table"})
        # Every call to Polaris's APIs is root's.
        for _, path, headers, _, _ in http.calls:
            if not path.endswith("/oauth/tokens"):
                self.assertEqual(headers["Authorization"], "Bearer token-rootid0123456789")

    def test_root_makes_nested_namespaces_with_properties_and_attaches_a_policy_three_ways(self):
        _, _, _, http, _ = run_seed()
        writes = http.writes()
        namespaces = [b for m, p, b in writes if p == f"{C}/gate_seed_catalog/namespaces"]
        self.assertEqual([n["namespace"] for n in namespaces], [["seed"], ["seed", "inner"]])
        self.assertTrue(all(n["properties"] for n in namespaces))
        (policy,) = [b for m, p, b in writes
                     if p == f"{P}/gate_seed_catalog/namespaces/seed/policies"]
        self.assertEqual(policy["type"], "system.data-compaction")
        self.assertEqual(json.loads(policy["content"])["version"], "2025-02-03")
        targets = [b["target"] for m, p, b in writes if p.endswith("/mappings")]
        self.assertEqual(targets, [{"type": "catalog"},
                                   {"type": "namespace", "path": ["seed", "inner"]},
                                   {"type": "table-like", "path": ["seed", "orders"]}])

    def test_the_writer_makes_two_tables_over_two_snapshots_a_schema_change_and_a_view(self):
        _, _, _, _, iceberg = run_seed()
        (properties,) = iceberg.catalogs
        self.assertEqual(properties["credential"], f"gate_seed-id:{secret('gate_seed')}")
        self.assertEqual(properties["warehouse"], "gate_seed_catalog")
        tables = iceberg.tables
        self.assertEqual(sorted(tables), ["seed.inner.events", "seed.orders"])
        for table in tables.values():
            self.assertEqual(len(table.snapshots), 2, table.name)
        self.assertEqual(tables["seed.inner.events"].columns, ["id", "name", "note"])
        # The schema changes between the two appends.
        events = [e for e in iceberg.log if "seed.inner.events" in e and e[0] != "gate_seed"]
        self.assertEqual([e[0] for e in events], ["append", "add_column", "append"])
        views = [e for e in iceberg.log if e[1:2] == ("create_view",)]
        self.assertEqual([(v[0], v[2]) for v in views], [("gate_seed", "seed.inner.events_view")])

    def test_the_record_holds_each_object_as_the_api_returns_it_and_every_snapshots_rows(self):
        _, record, _, _, iceberg = run_seed()
        objects = record["objects"]
        self.assertEqual(set(objects), set(seed.record_paths()))
        table = objects[f"{C}/gate_seed_catalog/namespaces/seed/tables/orders"]
        # The table's metadata and where it lives; the answer's config is not data.
        self.assertEqual(set(table), {"metadata-location", "metadata"})
        self.assertIn(f"{C}/gate_seed_catalog/namespaces/seed%1Finner/views/events_view", objects)
        rows = record["rows"]["seed.inner.events"]
        self.assertEqual(sorted(rows), ["100", "101"])
        self.assertEqual(len(rows["100"]), 2)
        self.assertEqual(rows["101"][-1]["note"], "after the schema change")
        self.assertEqual(record["principals"],
                         {"gate_seed": f"gate_seed-id:{secret('gate_seed')}",
                          "gate_seed_reader": f"gate_seed_reader-id:{secret('gate_seed_reader')}"})

    def test_what_it_prints_names_no_credential(self):
        _, _, out, _, _ = run_seed()
        for value in [*ENV.values(), secret("gate_seed"), secret("gate_seed_reader")]:
            self.assertNotIn(value, out)
        self.assertIn("ok   ", out)

    def test_a_write_polaris_refuses_stops_the_seed_without_a_record(self):
        http = FakePolaris(fail=("POST", f"{M}/principal-roles"))
        code, record, out, _, _ = run_seed(http=http)
        self.assertEqual((code, record), (1, None))
        self.assertIn("FAIL", out)

    def test_main_prints_the_record_on_its_own_line_after_the_checks(self):
        lines = seed.record_line({"objects": {}, "rows": {}, "principals": {}})
        self.assertTrue(lines.startswith(seed.RECORD_PREFIX))
        self.assertEqual(seed.read_record(f"ok   x\n{lines}\n"),
                         {"objects": {}, "rows": {}, "principals": {}})
        self.assertIsNone(seed.read_record("ok   x\n"))


class Check(unittest.TestCase):
    def seeded(self):
        _, record, _, _, iceberg = run_seed()
        return record, iceberg

    def test_everything_read_back_as_recorded_passes(self):
        record, iceberg = self.seeded()
        code, out = run_check(record, iceberg=iceberg)
        self.assertEqual(code, 0, out)
        self.assertNotIn("FAIL", out)
        self.assertIn("gate_seed got a token", out)
        self.assertIn("gate_seed_reader got a token", out)

    def test_a_changed_or_missing_field_fails_naming_its_path(self):
        record, iceberg = self.seeded()
        path = f"{M}/principals/gate_seed"
        changed = {**record["objects"][path], "entityVersion": 2}
        del changed["name"]
        code, out = run_check(record, http=FakePolaris({path: (200, changed)}), iceberg=iceberg)
        self.assertEqual(code, 1)
        self.assertIn(f"FAIL {path}.entityVersion: 1 recorded, 2 now", out)
        self.assertIn(f"FAIL {path}.name: missing", out)

    def test_a_field_the_new_version_adds_is_listed_and_passes(self):
        record, iceberg = self.seeded()
        path = f"{M}/catalogs/gate_seed_catalog"
        grown = {**record["objects"][path], "newField": "x"}
        code, out = run_check(record, http=FakePolaris({path: (200, grown)}), iceberg=iceberg)
        self.assertEqual(code, 0, out)
        self.assertIn(f"new field {path}.newField", out)

    def test_an_object_that_no_longer_reads_fails(self):
        record, iceberg = self.seeded()
        path = f"{C}/gate_seed_catalog/namespaces/seed%1Finner/views/events_view"
        code, out = run_check(record, http=FakePolaris({path: (404, {})}), iceberg=iceberg)
        self.assertEqual(code, 1)
        self.assertIn(f"FAIL {path}: HTTP 404", out)

    def test_a_seeded_principal_refused_a_token_fails(self):
        record, iceberg = self.seeded()
        code, out = run_check(record, http=FakePolaris(tokens_refused={"gate_seed_reader-id"}),
                              iceberg=iceberg)
        self.assertEqual(code, 1)
        self.assertIn("FAIL gate_seed_reader got no token with its recorded credentials", out)

    def test_rows_that_differ_at_a_recorded_snapshot_fail(self):
        record, iceberg = self.seeded()
        iceberg.tables["seed.orders"].snapshots[0] = (100, [{"id": 9, "name": "other"}])
        code, out = run_check(record, iceberg=iceberg)
        self.assertEqual(code, 1)
        self.assertIn("FAIL seed.orders at snapshot 100", out)

    def test_a_new_append_after_the_upgrade_is_read_back(self):
        record, iceberg = self.seeded()
        code, out = run_check(record, iceberg=iceberg)
        self.assertEqual(code, 0, out)
        self.assertEqual(len(iceberg.tables["seed.orders"].snapshots), 3)
        self.assertIn("ok   gate_seed appended to seed.orders after the upgrade", out)

    def test_a_failed_append_fails(self):
        record, iceberg = self.seeded()
        iceberg.append_fails = True
        code, out = run_check(record, iceberg=iceberg)
        self.assertEqual(code, 1)
        self.assertIn("FAIL gate_seed appending to seed.orders after the upgrade", out)

    def test_what_it_prints_names_no_credential(self):
        record, iceberg = self.seeded()
        _, out = run_check(record, http=FakePolaris(tokens_refused={"gate_seed-id"}),
                           iceberg=iceberg)
        for value in [*ENV.values(), secret("gate_seed"), secret("gate_seed_reader")]:
            self.assertNotIn(value, out)


class Main(unittest.TestCase):
    def test_a_wrong_command_line_exits_2(self):
        err = io.StringIO()
        sys_stderr, sys.stderr = sys.stderr, err
        try:
            self.assertEqual(seed.main(["seed.py", "plant", *ARGS]), 2)
        finally:
            sys.stderr = sys_stderr


if __name__ == "__main__":
    unittest.main()
