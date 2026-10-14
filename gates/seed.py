"""The seed before an upgrade, and the check after it (ADR-051 §2, §3). Run by `gates/gate.py
seed` and `gates/gate.py check-migration` in a short-lived pod on the pinned Python image, next to
iceberg/smoke.py, whose helpers it uses, after `pip install --require-hashes -r requirements.txt`.

    python3 seed.py seed|check POLARIS_URL REALM S3_ENDPOINT REGION BUCKET

Credentials come from the environment, as for smoke.py: ROOT_CLIENT_ID and ROOT_CLIENT_SECRET
(the Secret polaris-root), S3_ACCESS_KEY_ID and S3_SECRET_ACCESS_KEY (the Secret smoke-s3).

`seed`, against the last release's Polaris, writes a known set and records it:
1. Root, over the management API: the catalog gate_seed_catalog on the S3 endpoint as smoke.py
   makes its own; the principals gate_seed and gate_seed_reader, each with a principal role;
   a catalog role per principal role, assigned to it.
2. Root, over the Iceberg REST API: the namespaces seed and seed.inner, each with properties.
3. PyIceberg as gate_seed, which the gate's policy lets write in gate_seed_catalog only
   (gates/opa/policy.rego): seed.inner.events with an append, a new column and an append;
   seed.orders with two appends; the view seed.inner.events_view.
4. Root: grants on the catalog, a namespace and a table; the policy seed_compaction
   (system.data-compaction) attached to the catalog, seed.inner and seed.orders.
5. The record: every object of `record_paths()` as root reads it (a table's or view's
   metadata and its location only), every snapshot's rows as gate_seed reads them, and the
   seeded principals' credentials. Printed last, on one line after RECORD_PREFIX, for gate.py
   to keep on the runner; never printed otherwise.

`check`, against this commit's Polaris, reads the record on stdin:
1. Each recorded object reads back with every recorded field and value (`compare`); a field
   the new version adds passes and is listed.
2. Each seeded principal gets a token with its recorded credentials.
3. As gate_seed, each table returns the recorded rows at each recorded snapshot; then an
   append to seed.orders is read back.

Prints one line per check, `ok` or `FAIL`, with every credential masked; exits 1 when any check
failed. Polaris's database is counted by gate.py, not here.
"""

import json
import os
import sys
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
# In the repository smoke.py is in iceberg/; in the pod both files are in one directory.
sys.path.insert(0, os.path.join(HERE, "iceberg"))
import smoke  # noqa: E402

MANAGEMENT, ICEBERG, POLICIES = "/api/management/v1", "/api/catalog/v1", "/api/catalog/polaris/v1"
CATALOG = "gate_seed_catalog"
WRITER, READER = "gate_seed", "gate_seed_reader"
# Each principal role and its principal; each catalog role and the principal role it goes to.
PRINCIPAL_ROLES = (("seed_writer", WRITER), ("seed_reader", READER))
CATALOG_ROLES = (("seed_admin", "seed_writer"), ("seed_viewer", "seed_reader"))
NAMESPACES = ((("seed",), {"owner": "gate", "purpose": "migration"}),
              (("seed", "inner"), {"tier": "inner"}))
EVENTS, ORDERS, VIEW = "seed.inner.events", "seed.orders", "seed.inner.events_view"
VIEW_SQL = "SELECT id, name FROM seed.inner.events"
# seed.inner.events gains the column NEW_COLUMN between its two appends.
NEW_COLUMN = "note"
APPENDS = {EVENTS: ([{"id": 1, "name": "one"}, {"id": 2, "name": "two"}],
                    [{"id": 3, "name": "three", NEW_COLUMN: "after the schema change"}]),
           ORDERS: ([{"id": 1, "name": "first"}], [{"id": 2, "name": "second"}])}
AFTER_UPGRADE = [{"id": 3, "name": "after the upgrade"}]
GRANTS = (("seed_admin", {"type": "catalog", "privilege": "CATALOG_MANAGE_CONTENT"}),
          ("seed_viewer", {"type": "namespace", "namespace": ["seed"],
                           "privilege": "NAMESPACE_READ_PROPERTIES"}),
          ("seed_viewer", {"type": "table", "namespace": ["seed"], "tableName": "orders",
                           "privilege": "TABLE_READ_DATA"}))
POLICY, POLICY_NAMESPACE = "seed_compaction", ("seed",)
POLICY_CONTENT = {"version": "2025-02-03", "enable": True,
                  "config": {"target_file_size_bytes": 134217728}}
POLICY_TARGETS = ({"type": "catalog"}, {"type": "namespace", "path": ["seed", "inner"]},
                  {"type": "table-like", "path": ["seed", "orders"]})
RECORD_PREFIX = "SEED-RECORD "


def ns_path(namespace):
    """A namespace in a URL: its levels joined by the unit separator Polaris splits on."""
    return urllib.parse.quote("\x1f".join(namespace), safe="")


def record_paths():
    """What the record holds, each as root reads it."""
    m, c, p = MANAGEMENT, f"{ICEBERG}/{CATALOG}", f"{POLICIES}/{CATALOG}"
    inner, top = ns_path(("seed", "inner")), ns_path(("seed",))
    return [
        f"{m}/catalogs/{CATALOG}",
        *(f"{m}/principals/{name}" for name in (WRITER, READER)),
        *(f"{m}/principals/{name}/principal-roles" for name in (WRITER, READER)),
        *(f"{m}/principal-roles/{role}" for role, _ in PRINCIPAL_ROLES),
        *(f"{m}/principal-roles/{role}/catalog-roles/{CATALOG}" for role, _ in PRINCIPAL_ROLES),
        *(f"{m}/catalogs/{CATALOG}/catalog-roles/{role}" for role, _ in CATALOG_ROLES),
        *(f"{m}/catalogs/{CATALOG}/catalog-roles/{role}/grants" for role, _ in CATALOG_ROLES),
        f"{c}/namespaces",
        *(f"{c}/namespaces/{ns_path(ns)}" for ns, _ in NAMESPACES),
        f"{c}/namespaces/{inner}/tables/events",
        f"{c}/namespaces/{top}/tables/orders",
        f"{c}/namespaces/{inner}/views/events_view",
        f"{p}/namespaces/{top}/policies/{POLICY}",
        f"{p}/applicable-policies",
        f"{p}/applicable-policies?namespace={inner}",
        f"{p}/applicable-policies?namespace={top}&target-name=orders",
    ]


def pick(path, answer):
    """A table's or view's metadata and where it lives; its answer's config is not data."""
    if "/tables/" in path or "/views/" in path:
        return {k: answer[k] for k in ("metadata-location", "metadata") if k in answer}
    return answer


def _short(value):
    return json.dumps(value)[:120]


def compare(recorded, now, path=""):
    """(problems, added): each recorded field missing or with another value now, and each
    field `now` has beyond the record. Lists match item by item in any order."""
    if isinstance(recorded, dict) and isinstance(now, dict):
        problems, added = [], []
        for key, value in recorded.items():
            if key not in now:
                problems.append(f"{path}.{key}: missing")
                continue
            more_problems, more_added = compare(value, now[key], f"{path}.{key}")
            problems += more_problems
            added += more_added
        return problems, added + [f"{path}.{key}" for key in now if key not in recorded]
    if isinstance(recorded, list) and isinstance(now, list):
        problems, added, left = [], [], list(range(len(now)))
        for i, item in enumerate(recorded):
            for j in left:
                item_problems, item_added = compare(item, now[j], f"{path}[{i}]")
                if not item_problems:
                    left.remove(j)
                    added += item_added
                    break
            else:
                problems.append(f"{path}[{i}]: no item matches {_short(item)}")
        if left:
            added.append(f"{path}[+{len(left)} item{'s' if len(left) > 1 else ''}]")
        return problems, added
    if type(recorded) is not type(now) or recorded != now:
        return [f"{path}: {_short(recorded)} recorded, {_short(now)} now"], []
    return [], []


def record_line(record):
    return RECORD_PREFIX + json.dumps(record)


def read_record(text):
    """The record from the seed pod's output, or None."""
    lines = [line for line in text.splitlines() if line.startswith(RECORD_PREFIX)]
    return json.loads(lines[-1][len(RECORD_PREFIX):]) if lines else None


class SeedIceberg(smoke.PyIceberg):
    """smoke.py's PyIceberg, with what the seed adds: appends in the table's own schema, a
    column, snapshots and their rows, a view."""

    def append(self, table, rows):
        table.append(self._pa.Table.from_pylist(rows, schema=table.schema().as_arrow()))

    def add_column(self, table, name):
        from pyiceberg.types import StringType  # noqa: PLC0415
        with table.update_schema() as update:
            update.add_column(name, StringType())
        return table

    def snapshot_ids(self, table):
        return [s.snapshot_id for s in table.metadata.snapshots]

    def rows_at(self, table, snapshot_id):
        return table.scan(snapshot_id=snapshot_id).to_arrow().to_pylist()

    def create_view(self, catalog, identifier, sql):
        from pyiceberg.view.metadata import SQLViewRepresentation, ViewVersion  # noqa: PLC0415
        namespace = tuple(identifier.split(".")[:-1])
        version = ViewVersion(
            version_id=1, schema_id=0, timestamp_ms=int(time.time() * 1000),
            summary={"engine-name": "gate-seed"}, default_namespace=namespace,
            representations=[SQLViewRepresentation(type="sql", sql=sql, dialect="spark")])
        catalog.create_view(identifier, schema=self.schema, view_version=version)


def _by_id(rows):
    return sorted(rows, key=lambda r: r["id"])


def _write(http, report, polaris, headers, method, path, body, what):
    status, answer = http(method, f"{polaris}{path}", headers, body)
    if 200 <= status < 300:
        report.ok(f"root {what}: HTTP {status}")
        return True
    report.bad(f"root {what}: HTTP {status}")
    return False


def plant(http, iceberg, report, polaris, realm, headers, s3, env):
    """Writes the seed; returns its record, or None at the first write that fails."""
    s3_endpoint, region, bucket = s3
    if not smoke.create_catalog(http, report, polaris, headers,
                                smoke.catalog_request(CATALOG, bucket, s3_endpoint, region)):
        return None
    principals = {}
    for name in (WRITER, READER):
        credential = smoke.principal_credential(http, report, polaris, headers, name)
        if credential is None:
            return None
        report.secrets += [credential, credential.split(":", 1)[1]]
        principals[name] = credential
    m, c, p = MANAGEMENT, f"{ICEBERG}/{CATALOG}", f"{POLICIES}/{CATALOG}"
    writes = []
    for role, principal in PRINCIPAL_ROLES:
        writes += [("POST", f"{m}/principal-roles", {"principalRole": {"name": role}},
                    f"created principal role {role}"),
                   ("PUT", f"{m}/principals/{principal}/principal-roles",
                    {"principalRole": {"name": role}}, f"gave {principal} the role {role}")]
    for role, principal_role in CATALOG_ROLES:
        writes += [("POST", f"{m}/catalogs/{CATALOG}/catalog-roles",
                    {"catalogRole": {"name": role}}, f"created catalog role {role}"),
                   ("PUT", f"{m}/principal-roles/{principal_role}/catalog-roles/{CATALOG}",
                    {"catalogRole": {"name": role}},
                    f"gave principal role {principal_role} the catalog role {role}")]
    for namespace, properties in NAMESPACES:
        writes.append(("POST", f"{c}/namespaces",
                       {"namespace": list(namespace), "properties": properties},
                       f"created namespace {'.'.join(namespace)}"))
    for method, path, body, what in writes:
        if not _write(http, report, polaris, headers, method, path, body, what):
            return None

    rows = {}
    catalog = iceberg.catalog(smoke.catalog_properties(
        polaris, realm, CATALOG, principals[WRITER], s3_endpoint, region, env))
    try:
        for name, (first, second) in APPENDS.items():
            table = catalog.create_table(name, schema=iceberg.schema)
            iceberg.append(table, first)
            if NEW_COLUMN in second[0]:
                table = iceberg.add_column(table, NEW_COLUMN)
            iceberg.append(table, second)
            table = catalog.load_table(name)
            rows[name] = {str(s): _by_id(iceberg.rows_at(table, s))
                          for s in iceberg.snapshot_ids(table)}
            report.ok(f"{WRITER} wrote {name} over {len(rows[name])} snapshots")
        iceberg.create_view(catalog, VIEW, VIEW_SQL)
        report.ok(f"{WRITER} created the view {VIEW}")
    except Exception as e:  # noqa: BLE001 (reported, masked, and the seed fails)
        report.bad(f"{WRITER} writing the tables and the view: {smoke._error(e)}")
        return None

    writes = [("PUT", f"{m}/catalogs/{CATALOG}/catalog-roles/{role}/grants", {"grant": grant},
               f"granted {grant['privilege']} on a {grant['type']} to {role}")
              for role, grant in GRANTS]
    policies = f"{p}/namespaces/{ns_path(POLICY_NAMESPACE)}/policies"
    writes.append(("POST", policies, {"name": POLICY, "type": "system.data-compaction",
                                      "description": "Seeded before an upgrade (ADR-051)",
                                      "content": json.dumps(POLICY_CONTENT)},
                   f"created the policy {POLICY}"))
    writes += [("PUT", f"{policies}/{POLICY}/mappings", {"target": target},
                f"attached {POLICY} to a {target['type']}") for target in POLICY_TARGETS]
    for method, path, body, what in writes:
        if not _write(http, report, polaris, headers, method, path, body, what):
            return None

    objects = {}
    for path in record_paths():
        status, answer = http("GET", f"{polaris}{path}", headers)
        if status != 200:
            report.bad(f"root reading {path}: HTTP {status}, expected 200")
            return None
        objects[path] = pick(path, answer)
    report.ok(f"recorded {len(objects)} objects and {sum(map(len, rows.values()))} snapshots")
    return {"objects": objects, "rows": rows, "principals": principals}


def check(http, iceberg, report, polaris, realm, headers, s3, env, record):
    """Reads the record back from this commit's Polaris; failures go to the report."""
    s3_endpoint, region, _ = s3
    principals = record["principals"]
    report.secrets += [v for credential in principals.values()
                       for v in (credential, credential.split(":", 1)[1])]
    for path, recorded in record["objects"].items():
        status, answer = http("GET", f"{polaris}{path}", headers)
        if status != 200:
            report.bad(f"{path}: HTTP {status}, expected 200")
            continue
        problems, added = compare(recorded, pick(path, answer), path)
        for problem in problems:
            report.bad(problem)
        if not problems:
            report.ok(f"{path} reads back as recorded"
                      + "".join(f"; new field {a}" for a in added))
    for name, credential in principals.items():
        client_id, client_secret = credential.split(":", 1)
        status, answer = http("POST", f"{polaris}/api/catalog/v1/oauth/tokens",
                              {"Polaris-Realm": realm},
                              form={"grant_type": "client_credentials", "client_id": client_id,
                                    "client_secret": client_secret,
                                    "scope": "PRINCIPAL_ROLE:ALL"})
        if status == 200 and answer.get("access_token"):
            report.ok(f"{name} got a token with its recorded credentials")
        else:
            report.bad(f"{name} got no token with its recorded credentials: HTTP {status}")

    catalog = iceberg.catalog(smoke.catalog_properties(
        polaris, realm, CATALOG, principals[WRITER], s3_endpoint, region, env))
    for name, snapshots in record["rows"].items():
        try:
            table = catalog.load_table(name)
            for snapshot, rows in snapshots.items():
                got = _by_id(iceberg.rows_at(table, int(snapshot)))
                if got == _by_id(rows):
                    report.ok(f"{name} at snapshot {snapshot}: the {len(rows)} recorded rows")
                else:
                    report.bad(f"{name} at snapshot {snapshot}: {len(got)} rows that differ "
                               f"from the {len(rows)} recorded")
        except Exception as e:  # noqa: BLE001 (reported, masked, and the check fails)
            report.bad(f"{WRITER} reading {name}: {smoke._error(e)}")
    try:
        iceberg.append(catalog.load_table(ORDERS), AFTER_UPGRADE)
        got = _by_id(iceberg.rows(catalog.load_table(ORDERS)))
        want = _by_id(list(record["rows"][ORDERS].values())[-1] + AFTER_UPGRADE)
        if got == want:
            report.ok(f"{WRITER} appended to {ORDERS} after the upgrade and read it back")
        else:
            report.bad(f"{WRITER} appended to {ORDERS} after the upgrade: {len(got)} rows "
                       f"read back, {len(want)} expected")
    except Exception as e:  # noqa: BLE001 (reported, masked, and the check fails)
        report.bad(f"{WRITER} appending to {ORDERS} after the upgrade: {smoke._error(e)}")


def run(argv, env, http=smoke.urllib_http, iceberg=None, out=sys.stdout, stdin=sys.stdin):
    """(exit code, the record `seed` wrote or None)."""
    mode, polaris, realm, s3_endpoint, region, bucket = argv
    report = smoke.Report(env, out)
    token = smoke.root_token(http, polaris, realm, env)
    if not token:
        report.bad(f"root got no token in realm {realm}")
        return 1, None
    headers = {"Authorization": f"Bearer {token}", "Polaris-Realm": realm}
    s3 = (s3_endpoint, region, bucket)
    iceberg = iceberg or SeedIceberg()
    if mode == "seed":
        record = plant(http, iceberg, report, polaris, realm, headers, s3, env)
        return (0, record) if record is not None and not report.failed else (1, None)
    check(http, iceberg, report, polaris, realm, headers, s3, env, json.load(stdin))
    return (1 if report.failed else 0), None


def main(argv):
    if len(argv) != 7 or argv[1] not in ("seed", "check"):
        print(__doc__.strip().splitlines()[4].strip(), file=sys.stderr)
        return 2
    code, record = run(argv[1:], os.environ)
    if record is not None:
        print(record_line(record), flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
