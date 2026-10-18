"""Tests for datahub/check.py, with GMS and the frontend faked: one fake answers each request as
the real ones should, and each test breaks one answer.
Run: python3 -m unittest discover -s gates"""

import importlib.util
import io
import json
import os
import unittest
import unittest.mock
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
# Loaded under its own name: gates/console/check.py is `check` in the same test run.
_SPEC = importlib.util.spec_from_file_location("datahub_check",
                                               os.path.join(HERE, "datahub", "check.py"))
check = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(check)

GMS = "http://datahub-datahub-gms.catalog-datahub.svc.cluster.local:8080"
FRONTEND = "http://datahub-datahub-frontend.catalog-datahub.svc.cluster.local:9002"
NAME = "gate_check_123"
URN = "urn:li:dataset:(urn:li:dataPlatform:hive,gate_check_123,PROD)"
SECRET = "SystemSecret0123456789abcdefABCD"
PASSWORD = "RootPassword0123456789abcdefABCD"
ASPECT_QUERY = "?aspect=datasetProperties&version=0"
OTHER = "gate_check_456"
OTHER_URN = "urn:li:dataset:(urn:li:dataPlatform:hive,gate_check_456,PROD)"


class FakeGms:
    """GMS as it should answer the system client. `broken` names one answer to spoil;
    `indexed_after` is how many searches miss before a dataset is found; `datasets` are the
    datasets it already holds, by URN."""

    def __init__(self, broken=None, indexed_after=0, datasets=None):
        self.broken, self.indexed_after = broken, indexed_after
        self.calls, self.datasets, self.searches = [], dict(datasets or {}), 0

    @property
    def stored(self):
        return self.datasets.get(URN)

    def __call__(self, method, url, headers, body=None):
        self.calls.append((method, url, headers, body))
        if url == f"{GMS}/health":
            return (503 if self.broken == "health" else 200), {}, b""
        if headers.get("Authorization") != f"Basic __datahub_system:{SECRET}":
            return 401, {}, b"{}"
        if url == f"{GMS}/aspects?action=ingestProposal" and method == "POST":
            if self.broken == "write":
                return 500, {}, b"{}"
            proposal = body["proposal"]
            assert proposal["aspectName"] == "datasetProperties"
            stored = json.loads(proposal["aspect"]["value"])
            assert proposal["entityUrn"] == check.dataset_urn(stored["name"])
            self.datasets[proposal["entityUrn"]] = stored
            return 200, {}, json.dumps({"value": proposal["entityUrn"]}).encode()
        aspect = f"{GMS}/aspects/"
        if url.startswith(aspect) and url.endswith(ASPECT_QUERY) and method == "GET":
            urn = urllib.parse.unquote(url[len(aspect):-len(ASPECT_QUERY)])
            if urn not in self.datasets or self.broken == "read":
                return 404, {}, b"{}"
            return 200, {}, json.dumps({"version": 0, "aspect": {
                "com.linkedin.dataset.DatasetProperties": self.datasets[urn]}}).encode()
        if url == f"{GMS}/entities?action=search" and method == "POST":
            self.searches += 1
            urn = check.dataset_urn(body["input"])
            found = (urn in self.datasets and self.broken != "search"
                     and self.searches > self.indexed_after)
            entities = [{"entity": urn}] if found else []
            return 200, {}, json.dumps({"value": {"numEntities": len(entities),
                                                  "entities": entities}}).encode()
        if url == f"{GMS}/entities?action=delete" and method == "POST":
            if self.broken == "delete":
                return 500, {}, b"{}"
            if self.broken != "soft-delete":
                self.datasets.pop(body["urn"], None)
            return 200, {}, b"{}"
        raise AssertionError(f"unexpected {method} {url}")


class FakeFrontend:
    """The frontend with user.props `datahub:<password>`. `broken` names one answer to spoil."""

    def __init__(self, password=PASSWORD, broken=None):
        self.password, self.broken, self.calls = password, broken, []

    def __call__(self, method, url, headers, body=None):
        self.calls.append((method, url, headers, body))
        assert method == "POST" and url == f"{FRONTEND}/logIn", url
        accepted = body["username"] == "datahub" and (body["password"] == self.password
                                                      or self.broken == "image-password")
        if not accepted:
            # datahub-frontend's AuthenticationController.logIn at v1.7.0: a bad request.
            return 400, {}, json.dumps({"message": "Invalid Credentials"}).encode()
        cookies = {} if self.broken == "no-cookie" else {"set-cookie": "PLAY_SESSION=x"}
        return 200, cookies, b"{}"


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def run_gms(http, seconds=30):
    out, clock = io.StringIO(), Clock()
    code = check.run(["gms", GMS, NAME, str(seconds)], SECRET, http=http, out=out,
                     sleep=clock.sleep, clock=clock)
    return code, out.getvalue()


def run_step(http, *args):
    """One of the recovery check's single steps (ADR-071 §8)."""
    out, clock = io.StringIO(), Clock()
    code = check.run([args[0], GMS, *args[1:]], SECRET, http=http, out=out, sleep=clock.sleep,
                     clock=clock)
    return code, out.getvalue()


def held(*names):
    return {check.dataset_urn(n): {"name": n, "description": "lakehouse gate check"}
            for n in names}


def run_login(http, password=PASSWORD):
    out = io.StringIO()
    return check.run(["login", FRONTEND], password, http=http, out=out), out.getvalue()


class Gms(unittest.TestCase):
    def test_a_round_trip_through_every_store_passes(self):
        http = FakeGms()
        code, out = run_gms(http)
        self.assertEqual(code, 0, out)
        self.assertEqual([line.split()[0] for line in out.splitlines()], ["ok"] * 6)
        self.assertIsNone(http.stored)

    def test_the_search_is_retried_until_the_index_has_it(self):
        http = FakeGms(indexed_after=3)
        code, out = run_gms(http)
        self.assertEqual(code, 0, out)
        self.assertEqual(http.searches, 4)
        self.assertIn("found by search after 4 tries", out)

    def test_a_dataset_never_indexed_fails_within_the_time_limit_and_is_still_deleted(self):
        http = FakeGms(indexed_after=1000)
        code, out = run_gms(http, seconds=30)
        self.assertEqual(code, 1)
        self.assertIn("FAIL", out)
        self.assertIn("not within 30 s", out)
        self.assertLessEqual(http.searches, 30 // check.POLL_SECONDS + 1)
        self.assertIsNone(http.stored)
        self.assertIn(("POST", f"{GMS}/entities?action=delete"),
                      [(c[0], c[1]) for c in http.calls])

    def test_each_broken_answer_fails_the_check(self):
        for broken in ("health", "write", "read", "search", "delete", "soft-delete"):
            with self.subTest(broken=broken):
                code, out = run_gms(FakeGms(broken=broken))
                self.assertEqual(code, 1, out)
                self.assertIn("FAIL", out)

    def test_nothing_is_written_when_gms_is_not_healthy(self):
        http = FakeGms(broken="health")
        run_gms(http)
        self.assertEqual([c[1] for c in http.calls], [f"{GMS}/health"])

    def test_every_call_but_health_is_the_system_clients(self):
        http = FakeGms()
        run_gms(http)
        for method, url, headers, _ in http.calls[1:]:
            with self.subTest(url=url):
                self.assertEqual(headers["Authorization"], f"Basic __datahub_system:{SECRET}")
                self.assertEqual(headers["X-RestLi-Protocol-Version"], "2.0.0")

    def test_the_dataset_is_named_for_the_search(self):
        self.assertEqual(check.dataset_urn(NAME), URN)

    def test_the_secret_is_never_printed(self):
        for broken in (None, "write", "search"):
            with self.subTest(broken=broken):
                _, out = run_gms(FakeGms(broken=broken))
                self.assertNotIn(SECRET, out)


class Steps(unittest.TestCase):
    """The recovery check's steps, each from its own pod (ADR-071 §8)."""

    def test_write_writes_and_reads_back_and_keeps_the_dataset(self):
        http = FakeGms()
        code, out = run_step(http, "write", NAME)
        self.assertEqual(code, 0, out)
        self.assertEqual(out.splitlines(), [
            f"ok   {URN} written as __datahub_system", f"ok   {URN} read back by its URN"])
        self.assertEqual(http.stored["name"], NAME)

    def test_a_write_refused_reads_nothing_back(self):
        http = FakeGms(broken="write")
        code, out = run_step(http, "write", NAME)
        self.assertEqual(code, 1)
        self.assertEqual(len(out.splitlines()), 1)
        self.assertIn("FAIL", out)

    def test_read_finds_a_dataset_written_before(self):
        code, out = run_step(FakeGms(datasets=held(NAME)), "read", NAME)
        self.assertEqual(code, 0, out)
        self.assertEqual(out.splitlines(), [f"ok   {URN} read back by its URN"])

    def test_read_of_a_lost_dataset_fails(self):
        code, out = run_step(FakeGms(), "read", NAME)
        self.assertEqual(code, 1)
        self.assertIn(f"FAIL {URN} read back by its URN", out)

    def test_search_retries_until_the_index_has_it(self):
        http = FakeGms(datasets=held(NAME), indexed_after=2)
        code, out = run_step(http, "search", NAME, "30")
        self.assertEqual(code, 0, out)
        self.assertIn("found by search after 3 tries", out)
        self.assertEqual(http.stored["name"], NAME)

    def test_search_never_finding_it_fails_within_the_time_limit(self):
        http = FakeGms(datasets=held(NAME), indexed_after=1000)
        code, out = run_step(http, "search", NAME, "30")
        self.assertEqual(code, 1)
        self.assertIn("not within 30 s", out)
        self.assertLessEqual(http.searches, 30 // check.POLL_SECONDS + 1)

    def test_delete_hard_deletes_every_name_given(self):
        http = FakeGms(datasets=held(NAME, OTHER))
        code, out = run_step(http, "delete", NAME, OTHER)
        self.assertEqual(code, 0, out)
        self.assertEqual(http.datasets, {})
        self.assertEqual([line[5:] for line in out.splitlines()], [
            f"{URN} hard-deleted", f"{URN} reads back nothing after the delete",
            f"{OTHER_URN} hard-deleted", f"{OTHER_URN} reads back nothing after the delete"])

    def test_delete_goes_on_after_a_failure_and_fails(self):
        http = FakeGms(datasets=held(NAME, OTHER), broken="soft-delete")
        code, out = run_step(http, "delete", NAME, OTHER)
        self.assertEqual(code, 1)
        self.assertEqual(out.count("FAIL"), 2)
        self.assertIn(f"{OTHER_URN} hard-deleted", out)

    def test_every_step_is_the_system_clients_and_never_prints_the_secret(self):
        for args in (("write", NAME), ("read", NAME), ("search", NAME, "6"),
                     ("delete", NAME, OTHER)):
            with self.subTest(step=args[0]):
                http = FakeGms(datasets=held(NAME))
                _, out = run_step(http, *args)
                self.assertNotIn(SECRET, out)
                self.assertTrue(http.calls)
                for _, _, headers, _ in http.calls:
                    self.assertEqual(headers["Authorization"], f"Basic __datahub_system:{SECRET}")


class Login(unittest.TestCase):
    def test_the_root_users_password_signs_in_and_the_images_does_not(self):
        http = FakeFrontend()
        code, out = run_login(http)
        self.assertEqual(code, 0, out)
        self.assertEqual([c[3] for c in http.calls], [
            {"username": "datahub", "password": PASSWORD},
            {"username": "datahub", "password": "datahub"}])

    def test_a_wrong_password_fails(self):
        code, out = run_login(FakeFrontend(password="other"))
        self.assertEqual(code, 1)
        self.assertIn("FAIL", out)

    def test_the_images_password_still_accepted_fails(self):
        code, out = run_login(FakeFrontend(broken="image-password"))
        self.assertEqual(code, 1)
        self.assertIn("FAIL", out)

    def test_another_bad_request_is_not_a_refused_password(self):
        class Broken(FakeFrontend):
            def __call__(self, method, url, headers, body=None):
                if body["password"] == "datahub":
                    return 400, {}, json.dumps({"message": "User name must not be empty."}).encode()
                return super().__call__(method, url, headers, body)
        code, out = run_login(Broken())
        self.assertEqual(code, 1)
        self.assertIn("FAIL", out)

    def test_a_sign_in_without_a_session_cookie_fails(self):
        code, _ = run_login(FakeFrontend(broken="no-cookie"))
        self.assertEqual(code, 1)

    def test_a_secret_holding_the_images_password_fails(self):
        code, out = run_login(FakeFrontend(password="datahub"), password="datahub")
        self.assertEqual(code, 1)
        self.assertIn("holds the image's own password", out)

    def test_the_password_is_never_printed(self):
        for http in (FakeFrontend(), FakeFrontend(password="other")):
            _, out = run_login(http)
            self.assertNotIn(PASSWORD, out)


POLARIS = "http://polaris.catalog-polaris.svc.cluster.local:8181"
CATALOG = "gate_ingest_catalog"
ROOT_ID, ROOT_SECRET = "rootid0123456789", "RootSecretValue0123456789abcdefg"
INGEST_ID, INGEST_SECRET = "ingestid01234567", "IngestSecretValue0123456789abcde"
S3 = "http://recorder.standins.svc.cluster.local:8333"


class FakePolarisCatalog:
    """Polaris as root and datahub_ingest see it under the gate's policy (ADR-076 §4, §6).
    `existing` names what is already there; `broken` names one answer to spoil."""

    def __init__(self, existing=(), broken=None):
        self.existing, self.broken, self.calls = set(existing), broken, []
        self.tables = {}

    def token(self, form):
        known = {ROOT_ID: ROOT_SECRET, INGEST_ID: INGEST_SECRET}
        if (form.get("grant_type") != "client_credentials" or form.get("scope") !=
                "PRINCIPAL_ROLE:ALL" or known.get(form.get("client_id")) !=
                form.get("client_secret") or self.broken == "token"):
            return 401, {}, b'{"error": "unauthorized_client"}'
        return 200, {}, json.dumps({"access_token": f"token-{form['client_id']}"}).encode()

    def __call__(self, method, url, headers, body=None, form=None):
        self.calls.append((method, url, headers, body, form))
        if headers.get("Polaris-Realm") != "POLARIS":
            return 404, {}, b"{}"
        if url == f"{POLARIS}/api/catalog/v1/oauth/tokens" and method == "POST":
            return self.token(form or {})
        who = {"Bearer token-" + ROOT_ID: "root", "Bearer token-" + INGEST_ID: "ingest"}.get(
            headers.get("Authorization"))
        if who is None:
            return 401, {}, b"{}"
        base = f"{POLARIS}/api/catalog/v1/{CATALOG}"
        if url == f"{POLARIS}/api/management/v1/catalogs" and method == "POST":
            if self.broken == "catalog":
                return 500, {}, b"{}"
            if "catalog" in self.existing:
                return 409, {}, b"{}"
            self.existing.add("catalog")
            return 201, {}, b"{}"
        if url == f"{base}/namespaces" and method == "POST":
            if who != "root":
                return 403, {}, b'{"error": {"message": "not authorized"}}'
            if "namespace" in self.existing:
                return 409, {}, b"{}"
            self.existing.add("namespace")
            return 200, {}, b"{}"
        if url == f"{base}/namespaces/ingest/tables" and method == "POST":
            if self.broken == "table":
                return 400, {}, b'{"error": {"message": "bad schema"}}'
            if body["name"] in self.existing:
                return 409, {}, b"{}"
            self.existing.add(body["name"])
            self.tables[body["name"]] = body["schema"]
            return 200, {}, b"{}"
        if url == f"{base}/namespaces/ingest/tables/orders" and method == "GET":
            delegated = headers.get("X-Iceberg-Access-Delegation") == "vended-credentials"
            if delegated and self.broken != "delegation-allowed":
                return 403, {}, b"{}"
            if self.broken == "load-refused":
                return 403, {}, b"{}"
            return 200, {}, b'{"metadata": {}}'
        raise AssertionError(f"unexpected {method} {url}")


def run_polaris(http, step, *args, secret=f"{ROOT_ID}:{ROOT_SECRET}"):
    out = io.StringIO()
    code = check.run([step, POLARIS, "POLARIS", CATALOG, *args], secret, http=http, out=out)
    return code, out.getvalue()


def ingested_urn(table):
    return f"urn:li:dataset:(urn:li:dataPlatform:iceberg,{CATALOG}.ingest.{table},PROD)"


CONTAINER = "urn:li:container:0123456789abcdef0123456789abcdef"


def field_paths(fields, prefix="[version=2.0].[type=struct]"):
    """Datahub's v2 field paths for (name, type or [children]) pairs, as its Avro converter
    writes them."""
    paths = []
    for name, kind in fields:
        if isinstance(kind, list):
            path = f"{prefix}.[type=struct].{name}"
            paths.append(path)
            paths += field_paths(kind, path)
        else:
            paths.append(f"{prefix}.[type={kind}].{name}")
    return paths


INGESTED = {
    "orders": field_paths([("order_id", "long"), ("amount", "double"), ("placed_at", "long")]),
    "customers": field_paths([("customer_id", "long"), ("name", "string"),
                              ("address", [("street", "string"), ("city", "string")])]),
}


class FakeIngestedGms:
    """GMS holding what one ingestion run of the seeded catalog writes. `missing` names what it
    lacks; deletes remove for good."""

    def __init__(self, missing=(), broken=None):
        self.entities, self.broken, self.calls = {}, broken, []
        for table, paths in INGESTED.items():
            if table in missing:
                continue
            fields = [{"fieldPath": p} for p in paths]
            if f"{table}.address.city" in missing:
                fields = [f for f in fields if not f["fieldPath"].endswith(".city")]
            self.entities[ingested_urn(table)] = {
                "schemaMetadata": {"com.linkedin.schema.SchemaMetadata": {"fields": fields}},
                "container": {"com.linkedin.container.Container": {"container": CONTAINER}}}
        if "container" not in missing:
            self.entities[CONTAINER] = {"containerProperties": {
                "com.linkedin.container.ContainerProperties": {"name": "ingest"}}}

    def __call__(self, method, url, headers, body=None):
        self.calls.append((method, url, headers, body))
        if headers.get("Authorization") != f"Basic __datahub_system:{SECRET}":
            return 401, {}, b"{}"
        prefix = f"{GMS}/aspects/"
        if url.startswith(prefix) and method == "GET":
            quoted, _, query = url[len(prefix):].partition("?")
            urn = urllib.parse.unquote(quoted)
            aspect = urllib.parse.parse_qs(query)["aspect"][0]
            found = self.entities.get(urn, {}).get(aspect)
            if found is None:
                return 404, {}, b"{}"
            return 200, {}, json.dumps({"version": 0, "aspect": found}).encode()
        if url == f"{GMS}/entities?action=delete" and method == "POST":
            if self.broken == "delete":
                return 500, {}, b"{}"
            self.entities.pop(body["urn"], None)
            return 200, {}, b"{}"
        raise AssertionError(f"unexpected {method} {url}")


def run_gms_step(http, step):
    out = io.StringIO()
    code = check.run([step, GMS, CATALOG], SECRET, http=http, out=out)
    return code, out.getvalue()


class Seed(unittest.TestCase):
    """`seed`, as root (ADR-076 §6.1): the catalog, the namespace ingest and two tables."""

    def test_creates_the_catalog_the_namespace_and_both_tables(self):
        polaris = FakePolarisCatalog()
        code, out = run_polaris(polaris, "seed", S3, "us-east-1", "polaris-warehouse")
        self.assertEqual(code, 0, out)
        self.assertEqual(sorted(polaris.existing),
                         ["catalog", "customers", "namespace", "orders"])
        (create,) = [c for c in polaris.calls if c[1].endswith("/management/v1/catalogs")]
        storage = create[3]["catalog"]["storageConfigInfo"]
        self.assertEqual((create[3]["catalog"]["name"], storage["endpoint"],
                          storage["stsUnavailable"], storage["allowedLocations"]),
                         (CATALOG, S3, True, [f"s3://polaris-warehouse/{CATALOG}"]))
        self.assertNotIn("s3.", json.dumps(create[3]))

    def test_one_table_has_a_nested_struct_column(self):
        polaris = FakePolarisCatalog()
        run_polaris(polaris, "seed", S3, "us-east-1", "polaris-warehouse")
        nested = [f for f in polaris.tables["customers"]["fields"]
                  if isinstance(f["type"], dict)]
        self.assertEqual([(f["name"], f["type"]["type"],
                           [c["name"] for c in f["type"]["fields"]]) for f in nested],
                         [("address", "struct", ["street", "city"])])
        self.assertTrue(all(isinstance(f["type"], str)
                            for f in polaris.tables["orders"]["fields"]))

    def test_what_is_already_there_is_kept(self):
        polaris = FakePolarisCatalog(existing={"catalog", "namespace", "orders", "customers"})
        code, out = run_polaris(polaris, "seed", S3, "us-east-1", "polaris-warehouse")
        self.assertEqual(code, 0, out)
        self.assertIn("already there", out)

    def test_each_broken_answer_fails(self):
        for broken in ("token", "catalog", "table"):
            with self.subTest(broken=broken):
                code, out = run_polaris(FakePolarisCatalog(broken=broken), "seed", S3,
                                        "us-east-1", "polaris-warehouse")
                self.assertEqual(code, 1)
                self.assertIn("FAIL", out)

    def test_roots_credential_is_never_printed(self):
        for broken in (None, "token", "table"):
            _, out = run_polaris(FakePolarisCatalog(broken=broken), "seed", S3, "us-east-1",
                                 "polaris-warehouse")
            self.assertNotIn(ROOT_SECRET, out)
            self.assertNotIn(ROOT_ID, out)


class Refused(unittest.TestCase):
    """`refused`, as datahub_ingest (ADR-076 §6.4)."""

    def run_refused(self, polaris):
        return run_polaris(polaris, "refused", secret=f"{INGEST_ID}:{INGEST_SECRET}")

    def test_a_write_and_a_delegated_load_refused_a_plain_load_allowed(self):
        polaris = FakePolarisCatalog(existing={"catalog", "namespace", "orders"})
        code, out = self.run_refused(polaris)
        self.assertEqual(code, 0, out)
        self.assertEqual(len([line for line in out.splitlines() if line.startswith("ok")]), 4)
        self.assertNotIn("namespace-made", polaris.existing)
        loads = [c[2].get("X-Iceberg-Access-Delegation") for c in polaris.calls
                 if c[0] == "GET"]
        self.assertEqual(loads, ["vended-credentials", ""])

    def test_a_delegated_load_allowed_or_a_plain_load_refused_fails(self):
        for broken in ("delegation-allowed", "load-refused"):
            with self.subTest(broken=broken):
                code, out = self.run_refused(FakePolarisCatalog(broken=broken))
                self.assertEqual(code, 1)
                self.assertIn("FAIL", out)

    def test_the_credential_is_never_printed(self):
        _, out = self.run_refused(FakePolarisCatalog(broken="token"))
        self.assertNotIn(INGEST_SECRET, out)
        self.assertNotIn(INGEST_ID, out)

    def test_main_reads_the_mounted_secret_from_the_environment(self):
        calls = []
        with unittest.mock.patch.dict(os.environ, {"CLIENT_ID": INGEST_ID,
                                                   "CLIENT_SECRET": INGEST_SECRET}), \
                unittest.mock.patch.object(check, "run",
                                           lambda args, secret: calls.append(secret) or 0):
            code = check.main(["check.py", "refused", POLARIS, "POLARIS", CATALOG],
                              stdin=io.StringIO(""))
        self.assertEqual((code, calls), (0, [f"{INGEST_ID}:{INGEST_SECRET}"]))
        with unittest.mock.patch.dict(os.environ, {"CLIENT_ID": "", "CLIENT_SECRET": ""}):
            self.assertEqual(check.main(["check.py", "refused", POLARIS, "POLARIS", CATALOG],
                                        stdin=io.StringIO("")), 1)


class Ingested(unittest.TestCase):
    """`ingested` and `clean`, as the system client (ADR-076 §6.3, §6.6)."""

    def test_the_container_both_datasets_and_their_fields_pass(self):
        code, out = run_gms_step(FakeIngestedGms(), "ingested")
        self.assertEqual(code, 0, out)
        for table in ("orders", "customers"):
            self.assertIn(ingested_urn(table), out)
        self.assertIn("address.street", out)

    def test_anything_missing_fails(self):
        for missing in ("orders", "container", "customers.address.city"):
            with self.subTest(missing=missing):
                code, out = run_gms_step(FakeIngestedGms(missing={missing}), "ingested")
                self.assertEqual(code, 1)
                self.assertIn("FAIL", out)

    def test_simple_names_drop_datahubs_type_tokens(self):
        self.assertEqual(check.simple_name(
            "[version=2.0].[type=struct].[type=struct].address.[type=string].street"),
            "address.street")
        self.assertEqual(check.simple_name("[version=2.0].[type=struct].[type=long].order_id"),
                         "order_id")

    def test_clean_hard_deletes_both_datasets_and_the_container(self):
        gms = FakeIngestedGms()
        code, out = run_gms_step(gms, "clean")
        self.assertEqual(code, 0, out)
        self.assertEqual(gms.entities, {})
        deleted = [c[3]["urn"] for c in gms.calls if c[1].endswith("action=delete")]
        self.assertEqual(deleted, [ingested_urn("orders"), ingested_urn("customers"), CONTAINER])

    def test_clean_goes_on_after_a_failure_and_fails(self):
        code, out = run_gms_step(FakeIngestedGms(broken="delete"), "clean")
        self.assertEqual(code, 1)
        self.assertEqual(len([c for c in out.splitlines() if c.startswith("FAIL")]) > 1, True)

    def test_clean_of_nothing_ingested_passes(self):
        code, out = run_gms_step(FakeIngestedGms(missing={"orders", "customers", "container"}),
                                 "clean")
        self.assertEqual(code, 0, out)

    def test_the_secret_is_never_printed(self):
        for step in ("ingested", "clean"):
            _, out = run_gms_step(FakeIngestedGms(missing={"orders"}), step)
            self.assertNotIn(SECRET, out)


class Main(unittest.TestCase):
    def test_wrong_arguments_exit_2(self):
        for argv in (["check.py"], ["check.py", "gms", GMS], ["check.py", "login"],
                     ["check.py", "other", GMS], ["check.py", "write", GMS],
                     ["check.py", "write", GMS, NAME, "30"], ["check.py", "read", GMS],
                     ["check.py", "search", GMS, NAME], ["check.py", "delete", GMS],
                     ["check.py", "seed", POLARIS, "POLARIS", CATALOG],
                     ["check.py", "ingested", GMS], ["check.py", "refused", POLARIS],
                     ["check.py", "clean", GMS, CATALOG, "x"]):
            with self.subTest(argv=argv):
                self.assertEqual(check.main(argv, stdin=io.StringIO(SECRET + "\n")), 2)

    def test_each_step_takes_its_own_arguments(self):
        for argv in (["check.py", "write", GMS, NAME], ["check.py", "read", GMS, NAME],
                     ["check.py", "search", GMS, NAME, "30"], ["check.py", "delete", GMS, NAME],
                     ["check.py", "delete", GMS, NAME, OTHER],
                     ["check.py", "seed", POLARIS, "POLARIS", CATALOG, S3, "us-east-1", "b"],
                     ["check.py", "ingested", GMS, CATALOG], ["check.py", "clean", GMS, CATALOG]):
            with self.subTest(argv=argv):
                # No credential: past the arguments, the step stops at stdin with 1, never 2.
                self.assertEqual(check.main(argv, stdin=io.StringIO("")), 1)

    def test_no_credential_on_stdin_fails(self):
        self.assertEqual(check.main(["check.py", "login", FRONTEND], stdin=io.StringIO("")), 1)


if __name__ == "__main__":
    unittest.main()
