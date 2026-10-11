"""Tests for iceberg/smoke.py, with Polaris's management API and PyIceberg faked: PyIceberg is
installed only in the smoke pod. Run: python3 -m unittest discover -s gates"""

import contextlib
import io
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "iceberg"))
import smoke  # noqa: E402

ENV = {"ROOT_CLIENT_ID": "rootid0123456789", "ROOT_CLIENT_SECRET": "RootSecret0123456789",
       "S3_ACCESS_KEY_ID": "smokekey01234567", "S3_SECRET_ACCESS_KEY": "SmokeSecret0123456789"}
ARGS = ["http://polaris:8181", "POLARIS", "gate_catalog",
        "http://recorder.standins.svc.cluster.local:8333", "us-east-1", "polaris-warehouse"]


class FakeHttp:
    """Polaris's token endpoint and management API, answering as configured."""

    def __init__(self, catalog=201, principals=None, token=200):
        self.calls, self.catalog, self.token = [], catalog, token
        self.principals = principals or {}

    def __call__(self, method, url, headers, body=None, form=None):
        self.calls.append((method, url, headers, body, form))
        if url.endswith("/oauth/tokens"):
            return (200, {"access_token": "root-token"}) if self.token == 200 else (401, {})
        if url.endswith("/management/v1/catalogs"):
            return self.catalog, {}
        name = (body or {}).get("principal", {}).get("name") or url.split("/")[-2]
        status = self.principals.get(name, 201)
        if url.endswith("/rotate"):
            status = 200
        if status in (200, 201):
            return status, {"credentials": {"clientId": f"{name}-id",
                                            "clientSecret": f"{name}-secret0123456789"}}
        return status, {}


class Forbidden(Exception):
    pass


class FakeTable:
    def __init__(self, store, name):
        self.store, self.name = store, name
        self.properties = store.properties
        self.metadata = store.metadata

    def append(self, rows):
        self.store.rows[self.name] = list(rows)


class FakeStore:
    def __init__(self):
        self.rows, self.namespaces, self.properties = {}, set(), {}
        self.metadata = b'{"properties": {}}'


class FakeCatalog:
    """Polaris with the gate's policy: gate_smoke may work in its catalog but not drop;
    gate_other may do nothing."""

    def __init__(self, store, properties, readback=None):
        self.store, self.props, self.readback = store, properties, readback
        self.principal = properties["credential"].split("-id:")[0]

    def _allowed(self):
        if self.principal != smoke.SMOKE:
            raise Forbidden("OPA denied authorization")

    def list_namespaces(self):
        self._allowed()
        return [(n,) for n in sorted(self.store.namespaces)]

    def create_namespace(self, name):
        self._allowed()
        self.store.namespaces.add(name)

    def create_table(self, name, schema):
        self._allowed()
        self.store.rows[name] = []
        return FakeTable(self.store, name)

    def load_table(self, name):
        self._allowed()
        return FakeTable(self.store, name)

    def drop_table(self, name):
        raise Forbidden("OPA denied authorization")


class FakeIceberg:
    forbidden = Forbidden
    schema = "schema"

    def __init__(self, store=None, readback=None, catalogs=None):
        self.store = store or FakeStore()
        self.readback, self.made = readback, []
        self.catalog_class = catalogs or FakeCatalog

    def catalog(self, properties):
        self.made.append(properties)
        return self.catalog_class(self.store, properties)

    def arrow(self, rows):
        return rows

    def rows(self, table):
        return self.readback if self.readback is not None else table.store.rows[table.name]

    def metadata_bytes(self, table):
        return table.metadata


def run(http=None, iceberg=None, env=ENV):
    out = io.StringIO()
    code = smoke.run(ARGS, env, http=http or FakeHttp(), iceberg=iceberg or FakeIceberg(),
                     out=out, now=lambda: 1700000000)
    return code, out.getvalue()


class Catalog(unittest.TestCase):
    def test_the_catalog_is_on_s3_without_sts_and_its_properties_carry_no_key(self):
        request = smoke.catalog_request("gate_catalog", "polaris-warehouse",
                                        "http://s3:8333", "us-east-1")
        catalog = request["catalog"]
        self.assertEqual(catalog["type"], "INTERNAL")
        self.assertEqual(catalog["properties"],
                         {"default-base-location": "s3://polaris-warehouse/gate_catalog"})
        self.assertEqual(catalog["storageConfigInfo"], {
            "storageType": "S3", "allowedLocations": ["s3://polaris-warehouse/gate_catalog"],
            "endpoint": "http://s3:8333", "region": "us-east-1", "pathStyleAccess": True,
            "stsUnavailable": True})

    def test_root_creates_the_catalog_and_both_principals_with_its_token(self):
        http = FakeHttp()
        code, _ = run(http=http)
        self.assertEqual(code, 0)
        token_call = http.calls[0]
        self.assertEqual(token_call[4]["client_secret"], ENV["ROOT_CLIENT_SECRET"])
        self.assertEqual(token_call[2], {"Polaris-Realm": "POLARIS"})
        posts = [(c[1], c[3]) for c in http.calls[1:]]
        self.assertEqual(posts[0][0], "http://polaris:8181/api/management/v1/catalogs")
        self.assertEqual(posts[0][1]["catalog"]["name"], "gate_catalog")
        self.assertEqual([b["principal"]["name"] for _, b in posts[1:]],
                         ["gate_smoke", "gate_other"])
        for c in http.calls[1:]:
            self.assertEqual(c[2], {"Authorization": "Bearer root-token",
                                    "Polaris-Realm": "POLARIS"})

    def test_a_catalog_already_there_is_kept_and_another_answer_stops_the_run(self):
        code, out = run(http=FakeHttp(catalog=409))
        self.assertEqual(code, 0)
        self.assertIn("already exists: kept", out)
        code, out = run(http=FakeHttp(catalog=403))
        self.assertEqual(code, 1)
        self.assertIn("FAIL root creating catalog gate_catalog: HTTP 403", out)

    def test_a_principal_already_there_gets_new_credentials(self):
        http = FakeHttp(principals={"gate_smoke": 409})
        code, _ = run(http=http)
        self.assertEqual(code, 0)
        self.assertIn("http://polaris:8181/api/management/v1/principals/gate_smoke/rotate",
                      [c[1] for c in http.calls])

    def test_no_token_for_root_stops_the_run(self):
        iceberg = FakeIceberg()
        code, out = run(http=FakeHttp(token=401), iceberg=iceberg)
        self.assertEqual(code, 1)
        self.assertIn("FAIL root got no token", out)
        self.assertEqual(iceberg.made, [])


class Client(unittest.TestCase):
    def test_pyiceberg_asks_for_no_vended_credentials_and_uses_its_own_s3_key(self):
        iceberg = FakeIceberg()
        run(iceberg=iceberg)
        smoke_props, other_props = iceberg.made
        self.assertEqual(smoke_props, {
            "uri": "http://polaris:8181/api/catalog",
            "oauth2-server-uri": "http://polaris:8181/api/catalog/v1/oauth/tokens",
            "credential": "gate_smoke-id:gate_smoke-secret0123456789",
            "scope": "PRINCIPAL_ROLE:ALL",
            "warehouse": "gate_catalog",
            "header.Polaris-Realm": "POLARIS",
            "header.X-Iceberg-Access-Delegation": "",
            "s3.endpoint": "http://recorder.standins.svc.cluster.local:8333",
            "s3.region": "us-east-1",
            "s3.access-key-id": ENV["S3_ACCESS_KEY_ID"],
            "s3.secret-access-key": ENV["S3_SECRET_ACCESS_KEY"]})
        self.assertEqual(other_props["credential"], "gate_other-id:gate_other-secret0123456789")

    def test_the_round_trip_and_every_refusal_pass(self):
        code, out = run()
        self.assertEqual(code, 0, out)
        for line in ("ok   gate_smoke created table smoke.run_1700000000",
                     "ok   gate_smoke appended 3 rows with its own S3 key",
                     "ok   gate_smoke read back the same 3 rows",
                     "ok   table smoke.run_1700000000's metadata holds no S3 key",
                     "ok   gate_smoke dropping smoke.run_1700000000: refused (403)",
                     "ok   gate_other loading smoke.run_1700000000: refused (403)",
                     "ok   gate_other listing the namespaces of gate_catalog: refused (403)"):
            self.assertIn(line, out)

    def test_an_existing_namespace_is_kept(self):
        store = FakeStore()
        store.namespaces.add("smoke")
        code, _ = run(iceberg=FakeIceberg(store=store))
        self.assertEqual(code, 0)

    def test_rows_that_differ_fail(self):
        code, out = run(iceberg=FakeIceberg(readback=smoke.ROWS[:2]))
        self.assertEqual(code, 1)
        self.assertIn("FAIL gate_smoke read back 2 rows", out)

    def test_an_s3_key_in_the_tables_metadata_fails(self):
        for metadata, properties in (
                (json.dumps({"k": ENV["S3_SECRET_ACCESS_KEY"]}).encode(), {}),
                (b"{}", {"s3.access-key-id": "x"})):
            with self.subTest(properties=properties):
                store = FakeStore()
                store.metadata, store.properties = metadata, properties
                code, out = run(iceberg=FakeIceberg(store=store))
                self.assertEqual(code, 1)
                self.assertIn("metadata holds an S3 key", out)

    def test_an_allowed_or_otherwise_failing_refusal_fails(self):
        class DropAllowed(FakeCatalog):
            def drop_table(self, name):
                return None

        class DropBroken(FakeCatalog):
            def drop_table(self, name):
                raise RuntimeError("HTTP 500")

        for catalogs, said in ((DropAllowed, "allowed, expected 403"),
                               (DropBroken, "RuntimeError: HTTP 500, expected 403")):
            with self.subTest(said=said):
                code, out = run(iceberg=FakeIceberg(catalogs=catalogs))
                self.assertEqual(code, 1)
                self.assertIn(f"FAIL gate_smoke dropping smoke.run_1700000000: {said}", out)

    def test_an_error_in_the_round_trip_is_reported_with_every_credential_masked(self):
        class Leaky(FakeCatalog):
            def create_table(self, name, schema):
                raise ValueError(f"cannot write with {ENV['S3_SECRET_ACCESS_KEY']} as "
                                 f"{ENV['ROOT_CLIENT_SECRET']}")

        code, out = run(iceberg=FakeIceberg(catalogs=Leaky))
        self.assertEqual(code, 1)
        self.assertIn("FAIL gate_smoke round trip on smoke.run_1700000000: ValueError", out)
        for value in ENV.values():
            self.assertNotIn(value, out)
        self.assertIn("gate_other listing the namespaces", out)

    def test_main_needs_six_arguments(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(smoke.main(["smoke.py", "only-one"]), 2)
        self.assertIn("python3 smoke.py POLARIS_URL REALM CATALOG", err.getvalue())


if __name__ == "__main__":
    unittest.main()
