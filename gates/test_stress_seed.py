"""Tests for stress/seed.py, with Polaris's REST API and PyIceberg faked: PyIceberg is installed
only in the seed pod. Run: python3 -m unittest discover -s gates"""

import importlib.util
import io
import json
import os
import sys
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


seed = load("stress_seed", os.path.join(HERE, "stress", "seed.py"))

ENV = {"ROOT_CLIENT_ID": "rootid0123456789", "ROOT_CLIENT_SECRET": "RootSecret0123456789",
       "S3_ACCESS_KEY_ID": "smokekey01234567", "S3_SECRET_ACCESS_KEY": "SmokeSecret0123456789"}
POLARIS = "http://polaris:8181"
SEED_ARGS = [POLARIS, "POLARIS", "http://seaweedfs.standins.svc.cluster.local:8333",
             "us-east-1", "polaris-warehouse"]
CONFIG = {"seed": {"namespaces": 2, "tables_per_namespace": 2, "metadata_bytes": 3000,
                   "batch": 4, "max_batches": 20, "previous_versions_max": 3},
          "hot": {"tables": 1, "writers_per_table": 4}}
STRESS_SECRET = "gate_stress-secret0123456789"
TABLE_URL = f"{POLARIS}/api/catalog/v1/{seed.CATALOG}/namespaces/"


class FakePolaris:
    """Polaris's token endpoint, management API and table endpoints. Each table's metadata
    grows by SNAPSHOT_BYTES per snapshot, so a load's size follows its snapshot count."""

    SNAPSHOT_BYTES = 100

    def __init__(self, tables=None, catalog=201, commit=200):
        self.calls, self.catalog, self.commit = [], catalog, commit
        self.tables = tables or {}

    def add(self, name, snapshots):
        self.tables[name] = [{"snapshot-id": 1000 + i, "sequence-number": i + 1,
                              "manifest-list": f"s3://b/{name}/snap-1000.avro"}
                             for i in range(snapshots)]

    def body(self, name):
        snaps = self.tables[name]
        meta = {"format-version": 2, "last-sequence-number": snaps[-1]["sequence-number"],
                "current-snapshot-id": snaps[-1]["snapshot-id"], "snapshots": snaps,
                "pad": "x" * (self.SNAPSHOT_BYTES * len(snaps))}
        return json.dumps({"metadata-location": "s3://b/m.json", "metadata": meta}).encode()

    def __call__(self, method, url, headers, body=None, form=None):
        self.calls.append((method, url, headers, body, form))
        if url.endswith("/oauth/tokens"):
            who = "root" if form["client_id"] == ENV["ROOT_CLIENT_ID"] else "stress"
            return 200, json.dumps({"access_token": f"{who}-token"}).encode()
        if url.endswith("/management/v1/catalogs"):
            return self.catalog, b"{}"
        if "/management/v1/principals" in url:
            status = 200 if url.endswith("/rotate") else 201
            return status, json.dumps({"credentials": {
                "clientId": "gate_stress-id", "clientSecret": STRESS_SECRET}}).encode()
        name = ".".join(url[len(TABLE_URL):].split("/tables/"))
        if method == "GET":
            return (200, self.body(name)) if name in self.tables else (404, b"{}")
        if method == "POST":
            if self.commit != 200:
                return self.commit, b"{}"
            snaps = self.tables[name]
            (req,) = body["requirements"]
            if req["snapshot-id"] != snaps[-1]["snapshot-id"]:
                return 409, b"{}"
            snaps += [u["snapshot"] for u in body["updates"] if u["action"] == "add-snapshot"]
            return 200, self.body(name)
        raise AssertionError(f"unexpected {method} {url}")


class FakeTable:
    def __init__(self, polaris, name):
        self.polaris, self.name = polaris, name

    def append(self, rows):
        self.polaris.add(self.name, 1)


class FakeCatalog:
    def __init__(self, polaris, properties):
        self.polaris, self.properties = polaris, properties
        self.namespaces, self.made = set(), []

    def list_namespaces(self):
        return [(n,) for n in sorted(self.namespaces)]

    def create_namespace(self, name):
        self.namespaces.add(name)

    def create_table(self, name, schema, properties):
        self.made.append((name, properties))
        return FakeTable(self.polaris, name)


class FakeIceberg:
    schema = "schema"

    def __init__(self, polaris):
        self.polaris, self.catalogs = polaris, []

    def catalog(self, properties):
        self.catalogs.append(FakeCatalog(self.polaris, properties))
        return self.catalogs[-1]

    def arrow(self, rows):
        return rows


def ids():
    n = iter(range(5000, 10**6))
    return lambda: next(n)


def result(text):
    (line,) = [x for x in text.splitlines() if x.startswith(seed.RESULT)]
    return json.loads(line[len(seed.RESULT):])


class Names(unittest.TestCase):
    def test_tables_are_named_by_namespace_and_the_first_ones_are_hot(self):
        self.assertEqual(seed.tables(CONFIG), ["ns00.t00", "ns00.t01", "ns01.t00", "ns01.t01"])
        self.assertEqual(seed.hot(CONFIG), ["ns00.t00"])

    def test_the_committed_config_makes_100_tables_in_10_namespaces_and_10_hot(self):
        with open(os.path.join(HERE, "stress", "config.json"), encoding="utf-8") as f:
            config = json.load(f)
        names = seed.tables(config)
        self.assertEqual(len(names), 100)
        self.assertEqual(len({n.split(".")[0] for n in names}), 10)
        self.assertEqual(len(seed.hot(config)), 10)

    def test_tables_drop_old_metadata_files_so_the_runner_disk_stays_bounded(self):
        self.assertEqual(seed.table_properties(CONFIG), {
            "format-version": "2", "write.metadata.delete-after-commit.enabled": "true",
            "write.metadata.previous-versions-max": "3"})


class Snapshots(unittest.TestCase):
    STATE = {"snapshot": 77, "sequence": 9, "manifest_list": "s3://b/t/snap.avro"}

    def test_a_batch_chains_snapshots_with_rising_sequence_numbers_then_moves_main(self):
        updates, last = seed.snapshot_updates(self.STATE, 3, 1_000_000, ids())
        snaps = [u["snapshot"] for u in updates[:3]]
        self.assertEqual([u["action"] for u in updates], ["add-snapshot"] * 3
                         + ["set-snapshot-ref"])
        self.assertEqual([s["sequence-number"] for s in snaps], [10, 11, 12])
        self.assertEqual([s["parent-snapshot-id"] for s in snaps], [77, 5000, 5001])
        self.assertEqual({s["manifest-list"] for s in snaps}, {"s3://b/t/snap.avro"})
        self.assertEqual({s["summary"]["operation"] for s in snaps}, {"append"})
        stamps = [s["timestamp-ms"] for s in snaps]
        self.assertEqual(stamps, sorted(stamps))
        self.assertLessEqual(stamps[-1], 1_000_000)
        self.assertEqual(updates[-1], {"action": "set-snapshot-ref", "ref-name": "main",
                                       "type": "branch", "snapshot-id": 5002})
        self.assertEqual(last, {"snapshot": 5002, "sequence": 12,
                                "manifest_list": "s3://b/t/snap.avro"})

    def test_a_commit_asserts_main_is_still_the_snapshot_it_was_built_on(self):
        body = seed.commit_body(self.STATE, [{"action": "x"}])
        self.assertEqual(body, {"requirements": [{"type": "assert-ref-snapshot-id",
                                                  "ref": "main", "snapshot-id": 77}],
                                "updates": [{"action": "x"}]})

    def test_the_state_is_the_current_snapshot_its_manifest_list_and_the_last_sequence(self):
        meta = {"last-sequence-number": 4, "current-snapshot-id": 2,
                "snapshots": [{"snapshot-id": 1, "manifest-list": "a"},
                              {"snapshot-id": 2, "manifest-list": "b"}]}
        self.assertEqual(seed.table_state(meta),
                         {"snapshot": 2, "sequence": 4, "manifest_list": "b"})


def run_seed(polaris=None, config=CONFIG):
    polaris = polaris or FakePolaris()
    iceberg, out = FakeIceberg(polaris), io.StringIO()
    code = seed.seed(SEED_ARGS, config, ENV, http=polaris, iceberg=iceberg, out=out,
                     now=lambda: 1_700_000_000.0, new_id=ids())
    return code, out.getvalue(), polaris, iceberg


class Seed(unittest.TestCase):
    def test_root_makes_the_catalog_and_gate_stress_which_makes_and_appends_every_table(self):
        code, out, polaris, iceberg = run_seed()
        self.assertEqual(code, 0, out)
        catalog = [c for c in polaris.calls if c[1].endswith("/management/v1/catalogs")]
        self.assertEqual(catalog[0][3]["catalog"]["name"], seed.CATALOG)
        self.assertEqual(catalog[0][2]["Authorization"], "Bearer root-token")
        (made,) = iceberg.catalogs
        self.assertTrue(made.properties["credential"].startswith("gate_stress-id:"))
        self.assertEqual(made.properties["warehouse"], seed.CATALOG)
        self.assertEqual(made.namespaces, {"ns00", "ns01"})
        self.assertEqual([n for n, _ in made.made], seed.tables(CONFIG))
        self.assertEqual({json.dumps(p) for _, p in made.made},
                         {json.dumps(seed.table_properties(CONFIG))})

    def test_batches_grow_each_table_past_the_target_size_as_gate_stress(self):
        code, out, polaris, _ = run_seed()
        self.assertEqual(code, 0, out)
        tables = result(out)["tables"]
        self.assertEqual([t["name"] for t in tables], seed.tables(CONFIG))
        for t in tables:
            with self.subTest(table=t["name"]):
                self.assertGreaterEqual(t["metadata_bytes"], 3000)
                self.assertEqual(t["snapshots"], len(polaris.tables[t["name"]]))
                self.assertEqual(t["sequence"], t["snapshots"])
                self.assertEqual(t["manifest_list"], f"s3://b/{t['name']}/snap-1000.avro")
                self.assertEqual(t["hot"], t["name"] == "ns00.t00")
        posts = [c for c in polaris.calls if c[0] == "POST" and "/tables/" in c[1]]
        self.assertTrue(posts)
        self.assertEqual({c[2]["Authorization"] for c in posts}, {"Bearer stress-token"})
        self.assertEqual({len(c[3]["updates"]) for c in posts}, {CONFIG["seed"]["batch"] + 1})

    def test_no_credential_is_printed(self):
        _, out, _, _ = run_seed()
        for secret in [*ENV.values(), STRESS_SECRET]:
            self.assertNotIn(secret, out)

    def test_a_table_that_never_reaches_the_size_fails_the_seed(self):
        config = json.loads(json.dumps(CONFIG))
        config["seed"]["max_batches"] = 1
        code, out, _, _ = run_seed(config=config)
        self.assertEqual(code, 1)
        self.assertIn("FAIL", out)
        self.assertNotIn(seed.RESULT, out)

    def test_a_refused_commit_fails_the_seed(self):
        code, out, _, _ = run_seed(FakePolaris(commit=403))
        self.assertEqual(code, 1)
        self.assertIn("HTTP 403", out)

    def test_a_catalog_already_there_is_kept(self):
        code, out, _, _ = run_seed(FakePolaris(catalog=409))
        self.assertEqual(code, 0, out)


class Verify(unittest.TestCase):
    SEEDED = {"tables": [{"name": "ns00.t00", "sequence": 2}, {"name": "ns00.t01",
                                                               "sequence": 1}]}

    def run_verify(self, polaris):
        out = io.StringIO()
        code = seed.verify([POLARIS, "POLARIS"], self.SEEDED, ENV, http=polaris, out=out)
        return code, out.getvalue()

    def test_it_lists_each_tables_snapshots_newer_than_the_seed_and_any_duplicate(self):
        polaris = FakePolaris()
        polaris.add("ns00.t00", 4)
        polaris.add("ns00.t01", 1)
        polaris.tables["ns00.t01"].append(dict(polaris.tables["ns00.t01"][0]))
        code, out = self.run_verify(polaris)
        self.assertEqual(code, 0, out)
        found = result(out)["tables"]
        self.assertEqual(found["ns00.t00"], {"snapshots": 4, "new": ["1002", "1003"],
                                             "duplicates": [],
                                             "metadata_bytes": len(polaris.body("ns00.t00"))})
        self.assertEqual(found["ns00.t01"]["duplicates"], ["1000"])
        for secret in [*ENV.values(), STRESS_SECRET]:
            self.assertNotIn(secret, out)

    def test_a_table_that_does_not_load_fails(self):
        polaris = FakePolaris()
        polaris.add("ns00.t00", 2)
        code, out = self.run_verify(polaris)
        self.assertEqual(code, 1)
        self.assertIn("ns00.t01", out)


if __name__ == "__main__":
    unittest.main()
