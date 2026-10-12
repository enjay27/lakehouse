"""The Iceberg REST smoke test (ADR-009, ADR-026, ADR-031): PyIceberg, as an ordinary principal,
writes a table into a catalog on the S3 endpoint and reads it back, and the policy endpoint
refuses what the principal is not granted. Run by `gates/gate.py check-iceberg` in a short-lived
pod on the pinned Python image, after `pip install --require-hashes -r requirements.txt`.

    python3 smoke.py POLARIS_URL REALM CATALOG S3_ENDPOINT REGION BUCKET

Credentials come from the environment, never the command line: ROOT_CLIENT_ID and
ROOT_CLIENT_SECRET (the Secret polaris-root), S3_ACCESS_KEY_ID and S3_SECRET_ACCESS_KEY (the
Secret smoke-s3, the smoke tests' own key).

1. Root, over the management API, creates CATALOG (a catalog already there is kept): S3
   storage at s3://BUCKET/CATALOG, `stsUnavailable`, path-style access, S3_ENDPOINT. Its
   properties carry no key: Polaris reads and writes table metadata with its own key, and a
   key in `table-default.*` would be copied into every table's metadata.
2. Root creates the principals gate_smoke, which the gate's policy lets work inside CATALOG,
   and gate_other, which it grants nothing; a principal already there gets new credentials.
3. PyIceberg as gate_smoke, with its own S3 key and the header
   `X-Iceberg-Access-Delegation` empty: Polaris 1.6.0 answers 400 to a request for vended
   credentials on a catalog without STS. It creates the namespace `smoke`, a table named for
   this run, appends rows and reads the same rows back. The table's metadata, read with the
   smoke key, holds no S3 key, and its properties no `s3.*`.
4. Refusals, each a 403 from the policy endpoint: gate_smoke dropping the table, gate_other
   listing the namespaces and loading the table.

Prints one line per check, `ok` or `FAIL`, with every credential masked; exits 1 when any
check failed.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

SMOKE, OTHER = "gate_smoke", "gate_other"
NAMESPACE = "smoke"
ROWS = [{"id": 1, "name": "one"}, {"id": 2, "name": "two"}, {"id": 3, "name": "three"}]
CREDENTIAL_VARS = ("ROOT_CLIENT_ID", "ROOT_CLIENT_SECRET", "S3_ACCESS_KEY_ID",
                   "S3_SECRET_ACCESS_KEY")


def catalog_request(catalog, bucket, s3_endpoint, region):
    """The management API's create-catalog body."""
    location = f"s3://{bucket}/{catalog}"
    return {"catalog": {
        "type": "INTERNAL", "name": catalog,
        "properties": {"default-base-location": location},
        "storageConfigInfo": {"storageType": "S3", "allowedLocations": [location],
                              "endpoint": s3_endpoint, "region": region,
                              "pathStyleAccess": True, "stsUnavailable": True}}}


def catalog_properties(polaris, realm, catalog, credential, s3_endpoint, region, env):
    """PyIceberg's RestCatalog properties for a principal: Polaris's token endpoint, the realm,
    no access delegation, and the smoke tests' own S3 key."""
    return {
        "uri": f"{polaris}/api/catalog",
        "oauth2-server-uri": f"{polaris}/api/catalog/v1/oauth/tokens",
        "credential": credential,
        "scope": "PRINCIPAL_ROLE:ALL",
        "warehouse": catalog,
        "header.Polaris-Realm": realm,
        "header.X-Iceberg-Access-Delegation": "",
        "s3.endpoint": s3_endpoint,
        "s3.region": region,
        "s3.access-key-id": env["S3_ACCESS_KEY_ID"],
        "s3.secret-access-key": env["S3_SECRET_ACCESS_KEY"],
    }


def urllib_http(method, url, headers, body=None, form=None):
    """(status, the JSON answer or {}): one request, no exception for an HTTP error status."""
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers = {**headers, "Content-Type": "application/x-www-form-urlencoded"}
    elif body is not None:
        data = json.dumps(body).encode()
        headers = {**headers, "Content-Type": "application/json"}
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
    try:
        return status, json.loads(raw) if raw else {}
    except ValueError:
        return status, {}


class PyIceberg:
    """PyIceberg and PyArrow, imported only where they are installed: the pod."""

    def __init__(self):
        import pyarrow  # noqa: PLC0415
        from pyiceberg.catalog.rest import RestCatalog  # noqa: PLC0415
        from pyiceberg.exceptions import ForbiddenError  # noqa: PLC0415
        self._pa, self._catalog, self.forbidden = pyarrow, RestCatalog, ForbiddenError
        self.schema = pyarrow.schema([("id", pyarrow.int64()), ("name", pyarrow.string())])

    def catalog(self, properties):
        return self._catalog("gate", **properties)

    def arrow(self, rows):
        return self._pa.Table.from_pylist(rows, schema=self.schema)

    def rows(self, table):
        return sorted(table.scan().to_arrow().to_pylist(), key=lambda r: r["id"])

    def metadata_bytes(self, table):
        with table.io.new_input(table.metadata_location).open() as f:
            return f.read()


class Report:
    def __init__(self, env, out):
        self.secrets = [env[v] for v in CREDENTIAL_VARS if env.get(v)]
        self.out, self.failed = out, False

    def mask(self, text):
        for value in self.secrets:
            text = text.replace(value, "<redacted>")
        return text

    def ok(self, line):
        print(f"ok   {self.mask(line)}", file=self.out, flush=True)

    def bad(self, line):
        self.failed = True
        print(f"FAIL {self.mask(line)}", file=self.out, flush=True)


def _error(e):
    return f"{type(e).__name__}: {str(e)[:300]}"


def root_token(http, polaris, realm, env):
    status, answer = http("POST", f"{polaris}/api/catalog/v1/oauth/tokens",
                          {"Polaris-Realm": realm},
                          form={"grant_type": "client_credentials",
                                "client_id": env["ROOT_CLIENT_ID"],
                                "client_secret": env["ROOT_CLIENT_SECRET"],
                                "scope": "PRINCIPAL_ROLE:ALL"})
    return answer.get("access_token") if status == 200 else None


def create_catalog(http, report, polaris, headers, request):
    name = request["catalog"]["name"]
    status, _ = http("POST", f"{polaris}/api/management/v1/catalogs", headers, request)
    if status == 201:
        report.ok(f"root created catalog {name} on S3 without STS: HTTP 201")
    elif status == 409:
        report.ok(f"catalog {name} already exists: kept")
    else:
        report.bad(f"root creating catalog {name}: HTTP {status}, expected 201")
        return False
    return True


def principal_credential(http, report, polaris, headers, name):
    """`clientId:clientSecret` for a new principal, or new credentials for one already there."""
    management = f"{polaris}/api/management/v1/principals"
    status, answer = http("POST", management, headers, {"principal": {"name": name}})
    if status == 409:
        status, answer = http("POST", f"{management}/{name}/rotate", headers)
        expected = 200
    else:
        expected = 201
    creds = answer.get("credentials") or {}
    if status != expected or not creds.get("clientId") or not creds.get("clientSecret"):
        report.bad(f"root creating principal {name}: HTTP {status}, expected {expected}")
        return None
    report.ok(f"principal {name} ready: HTTP {status}")
    return f"{creds['clientId']}:{creds['clientSecret']}"


def refused(report, iceberg, what, call):
    try:
        call()
    except iceberg.forbidden:
        report.ok(f"{what}: refused (403)")
        return
    except Exception as e:  # noqa: BLE001 (any other outcome fails the check)
        report.bad(f"{what}: {_error(e)}, expected 403")
        return
    report.bad(f"{what}: allowed, expected 403")


def round_trip(report, iceberg, catalog, table_name, env):
    """gate_smoke's namespace, table, append and read; returns the table, or None."""
    names = [".".join(n) for n in catalog.list_namespaces()]
    if NAMESPACE not in names:
        catalog.create_namespace(NAMESPACE)
    report.ok(f"gate_smoke has namespace {NAMESPACE}")
    table = catalog.create_table(table_name, schema=iceberg.schema)
    report.ok(f"gate_smoke created table {table_name}")
    table.append(iceberg.arrow(ROWS))
    report.ok(f"gate_smoke appended {len(ROWS)} rows with its own S3 key")
    got = iceberg.rows(catalog.load_table(table_name))
    if got != ROWS:
        report.bad(f"gate_smoke read back {len(got)} rows that differ from the {len(ROWS)} "
                   "written")
        return None
    report.ok(f"gate_smoke read back the same {len(ROWS)} rows")
    table = catalog.load_table(table_name)
    data = iceberg.metadata_bytes(table)
    keys = [p for p in table.properties if p.startswith("s3.")]
    if any(env[v].encode() in data for v in ("S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY")) \
            or keys:
        report.bad(f"table {table_name}'s metadata holds an S3 key ({len(keys)} s3.* "
                   "properties)")
    else:
        report.ok(f"table {table_name}'s metadata holds no S3 key")
    return table


def run(argv, env, http=urllib_http, iceberg=None, out=sys.stdout, now=time.time):
    polaris, realm, catalog_name, s3_endpoint, region, bucket = argv
    report = Report(env, out)
    token = root_token(http, polaris, realm, env)
    if not token:
        report.bad(f"root got no token in realm {realm}")
        return 1
    headers = {"Authorization": f"Bearer {token}", "Polaris-Realm": realm}
    if not create_catalog(http, report, polaris, headers,
                          catalog_request(catalog_name, bucket, s3_endpoint, region)):
        return 1
    creds = {p: principal_credential(http, report, polaris, headers, p) for p in (SMOKE, OTHER)}
    if None in creds.values():
        return 1

    iceberg = iceberg or PyIceberg()
    smoke, other = (iceberg.catalog(catalog_properties(polaris, realm, catalog_name, creds[p],
                                                       s3_endpoint, region, env))
                    for p in (SMOKE, OTHER))
    table_name = f"{NAMESPACE}.run_{int(now())}"
    try:
        table = round_trip(report, iceberg, smoke, table_name, env)
    except Exception as e:  # noqa: BLE001 (reported, masked, and the run fails)
        report.bad(f"gate_smoke round trip on {table_name}: {_error(e)}")
        table = None
    if table is not None:
        refused(report, iceberg, f"gate_smoke dropping {table_name}",
                lambda: smoke.drop_table(table_name))
        refused(report, iceberg, f"gate_other loading {table_name}",
                lambda: other.load_table(table_name))
    refused(report, iceberg, f"gate_other listing the namespaces of {catalog_name}",
            other.list_namespaces)
    return 1 if report.failed else 0


def main(argv):
    if len(argv) != 7:
        print(__doc__.strip().splitlines()[5].strip(), file=sys.stderr)
        return 2
    return run(argv[1:], os.environ)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
