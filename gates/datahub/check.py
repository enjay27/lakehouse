"""Datahub's smoke check (ADR-071 §8, ADR-072 §4, ADR-076 §6): the requests a lineage emitter,
a user and Polaris's ingestion make, from a short-lived pod on the pinned Python image
(`gates/gate.py check-datahub`, `check-datahub-ingestion`). Standard library only.

    python3 check.py gms GMS_URL NAME SEARCH_SECONDS
    python3 check.py login FRONTEND_URL
    python3 check.py write GMS_URL NAME
    python3 check.py read GMS_URL NAME
    python3 check.py search GMS_URL NAME SEARCH_SECONDS
    python3 check.py delete GMS_URL NAME...
    python3 check.py seed POLARIS_URL REALM CATALOG S3_ENDPOINT REGION BUCKET
    python3 check.py ingested GMS_URL CATALOG
    python3 check.py refused POLARIS_URL REALM CATALOG
    python3 check.py clean GMS_URL CATALOG

The credential comes on stdin, one line, never on the command line: the system client's secret
(the Secret datahub-auth-secrets) for every step but `login`, `seed` and `refused`, the root
user's password (datahub-root-user) for `login`, Polaris root's `CLIENT_ID:CLIENT_SECRET`
(polaris-root) for `seed`. `refused` reads datahub_ingest's from CLIENT_ID and CLIENT_SECRET
in its environment, the Secret datahub-polaris-principal mounted.

`gms`, as the system client `__datahub_system`, from a pod labelled as GMS's client:
1. GMS answers /health.
2. A dataset named NAME is written with its datasetProperties aspect (ingestProposal), then read
   back by its URN: datahub-pg.
3. A search for NAME finds its URN within SEARCH_SECONDS: the change log went through Kafka and
   was indexed in OpenSearch.
4. The dataset is hard-deleted, and reading it back finds nothing. Once written, it is deleted
   whatever failed after.

`login`, from a pod in the ingress controller's namespace: the frontend signs in `datahub` with
the password, and refuses the image's own `datahub:datahub` as invalid credentials.

`write`, `read`, `search` and `delete` are `gms`'s steps 2 to 4 one at a time, for the recovery
check (`gates/gate.py check-datahub-recovery`, ADR-071 §8), which loses a store between them:
`write` writes NAME and reads it back, and keeps it; `read` reads back one written before;
`search` finds it within SEARCH_SECONDS; `delete` hard-deletes each NAME, going on after a
failure.

`seed`, `ingested`, `refused` and `clean` are check-datahub-ingestion's (ADR-076 §6):
`seed`, as root over Polaris's APIs, creates CATALOG as the smoke tests create theirs (S3
storage at s3://BUCKET/CATALOG without STS, no key in its properties), the namespace
`ingest`, and the tables TABLES, one with a nested struct column; anything already there is
kept. `ingested`, as the system client, reads from GMS the two datasets by the URNs Datahub's
Iceberg source makes with CATALOG as the platform instance, every field of each, nested ones
included, and their container, the namespace. `refused`, as datahub_ingest: a token, then a
namespace create and a table load with vended credentials refused with 403, the same load
without them allowed. `clean` hard-deletes both datasets and their container, going on after
a failure.

Prints one line per check, `ok` or `FAIL`, with the credential masked; exits 1 when any check
failed, 2 on wrong arguments.
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

SYSTEM_CLIENT = "__datahub_system"
ROOT_USER = "datahub"
# The image's own user.props line, which datahub-root-user replaces (ADR-071 §6).
IMAGE_PASSWORD = "datahub"
INVALID_CREDENTIALS = "Invalid Credentials"
PLATFORM = "urn:li:dataPlatform:hive"
RESTLI = {"X-RestLi-Protocol-Version": "2.0.0"}
POLL_SECONDS = 3
# check-datahub-ingestion's catalog content (ADR-076 §6.1): the namespace and two tables, their
# Iceberg schemas, and the field names Datahub must hold for each.
INGEST_NAMESPACE = "ingest"
INGEST_PLATFORM = "urn:li:dataPlatform:iceberg"
TABLES = {
    "orders": [{"id": 1, "name": "order_id", "required": True, "type": "long"},
               {"id": 2, "name": "amount", "required": False, "type": "double"},
               {"id": 3, "name": "placed_at", "required": False, "type": "timestamptz"}],
    "customers": [{"id": 1, "name": "customer_id", "required": True, "type": "long"},
                  {"id": 2, "name": "name", "required": False, "type": "string"},
                  {"id": 3, "name": "address", "required": False, "type": {
                      "type": "struct", "fields": [
                          {"id": 4, "name": "street", "required": False, "type": "string"},
                          {"id": 5, "name": "city", "required": False, "type": "string"}]}}],
}
DELEGATION = "X-Iceberg-Access-Delegation"


def urllib_http(method, url, headers, body=None, form=None):
    """(status, headers in lowercase, raw body): one request, no exception for an HTTP error."""
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers = {**headers, "Content-Type": "application/x-www-form-urlencoded"}
    elif body is not None:
        data = json.dumps(body).encode()
        headers = {**headers, "Content-Type": "application/json"}
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
            return response.status, _lower(response.headers), response.read()
    except urllib.error.HTTPError as e:
        return e.code, _lower(e.headers), e.read()


def _lower(headers):
    return {k.lower(): v for k, v in headers.items()}


def _json(raw):
    try:
        return json.loads(raw) if raw else {}
    except ValueError:
        return {}


class Report:
    def __init__(self, secret, out):
        self.secret, self.out, self.failed = secret, out, False

    def mask(self, text):
        # A Polaris credential is CLIENT_ID:CLIENT_SECRET: each part is masked on its own too.
        for value in [self.secret] + (self.secret or "").split(":"):
            if value:
                text = text.replace(value, "<redacted>")
        return text

    def check(self, passed, line, why=""):
        if passed:
            print(f"ok   {self.mask(line)}", file=self.out, flush=True)
        else:
            self.failed = True
            print(f"FAIL {self.mask(line)}{': ' + self.mask(why) if why else ''}", file=self.out,
                  flush=True)
        return passed


def dataset_urn(name):
    return f"urn:li:dataset:({PLATFORM},{name},PROD)"


def read_properties(http, gms, headers, urn):
    """(status, the datasetProperties aspect or None) for `urn`."""
    quoted = urllib.parse.quote(urn, safe="")
    status, _, raw = http("GET", f"{gms}/aspects/{quoted}?aspect=datasetProperties&version=0",
                          headers)
    aspect = _json(raw).get("aspect") or {}
    return status, aspect.get("com.linkedin.dataset.DatasetProperties")


def system_headers(secret):
    return {**RESTLI, "Authorization": f"Basic {SYSTEM_CLIENT}:{secret}"}


def write_dataset(http, report, gms, headers, name):
    urn = dataset_urn(name)
    status, _, _ = http("POST", f"{gms}/aspects?action=ingestProposal", headers, {"proposal": {
        "entityType": "dataset", "entityUrn": urn, "changeType": "UPSERT",
        "aspectName": "datasetProperties",
        "aspect": {"contentType": "application/json",
                   "value": json.dumps({"name": name, "description": "lakehouse gate check"})}}})
    return report.check(status == 200, f"{urn} written as {SYSTEM_CLIENT}", f"HTTP {status}")


def read_back(http, report, gms, headers, name):
    urn = dataset_urn(name)
    status, aspect = read_properties(http, gms, headers, urn)
    return report.check(status == 200 and (aspect or {}).get("name") == name,
                        f"{urn} read back by its URN", f"HTTP {status}, {aspect}")


def search(http, report, gms, headers, name, search_seconds, sleep, clock):
    urn = dataset_urn(name)
    end, found, tries = clock() + search_seconds, False, 0
    while True:
        tries += 1
        status, _, raw = http("POST", f"{gms}/entities?action=search", headers,
                              {"input": name, "entity": "dataset", "start": 0, "count": 10})
        found = status == 200 and urn in json.dumps(_json(raw).get("value") or {})
        if found or clock() >= end:
            break
        sleep(POLL_SECONDS)
    return report.check(found, f"{urn} found by search after {tries} tries",
                        f"not within {search_seconds} s, last HTTP {status}")


def delete_dataset(http, report, gms, headers, name):
    urn = dataset_urn(name)
    status, _, _ = http("POST", f"{gms}/entities?action=delete", headers, {"urn": urn})
    report.check(status == 200, f"{urn} hard-deleted", f"HTTP {status}")
    status, aspect = read_properties(http, gms, headers, urn)
    report.check(aspect is None, f"{urn} reads back nothing after the delete",
                 f"HTTP {status}, {aspect}")


def check_gms(http, report, gms, secret, name, search_seconds, sleep=time.sleep,
              clock=time.monotonic):
    headers = system_headers(secret)
    status, _, _ = http("GET", f"{gms}/health", {})
    if not report.check(status == 200, "GMS answers /health", f"HTTP {status}"):
        return
    if not write_dataset(http, report, gms, headers, name):
        return
    try:
        read_back(http, report, gms, headers, name)
        search(http, report, gms, headers, name, search_seconds, sleep, clock)
    finally:
        delete_dataset(http, report, gms, headers, name)


def check_login(http, report, frontend, password):
    status, headers, _ = http("POST", f"{frontend}/logIn", {},
                              {"username": ROOT_USER, "password": password})
    report.check(status == 200 and "set-cookie" in headers,
                 f"the frontend signs in {ROOT_USER} with datahub-root-user's password",
                 f"HTTP {status}")
    if password == IMAGE_PASSWORD:
        report.check(False, f"{ROOT_USER}:{IMAGE_PASSWORD} refused",
                     "datahub-root-user holds the image's own password")
        return
    status, _, raw = http("POST", f"{frontend}/logIn", {},
                          {"username": ROOT_USER, "password": IMAGE_PASSWORD})
    # datahub-frontend's AuthenticationController.logIn answers a failed login with a bad
    # request, its message "Invalid Credentials" (v1.7.0).
    message = _json(raw).get("message")
    report.check(status == 400 and message == INVALID_CREDENTIALS,
                 f"the frontend refuses the image's {ROOT_USER}:{IMAGE_PASSWORD}",
                 f"HTTP {status}, {message}")


def polaris_error(raw):
    error = _json(raw).get("error")
    return f", {error['message']}" if isinstance(error, dict) and error.get("message") else ""


def polaris_token(http, report, polaris, realm, credential, who):
    """A bearer token for `credential` (CLIENT_ID:CLIENT_SECRET), or None."""
    client_id, _, client_secret = credential.partition(":")
    status, _, raw = http("POST", f"{polaris}/api/catalog/v1/oauth/tokens",
                          {"Polaris-Realm": realm}, form={
                              "grant_type": "client_credentials", "client_id": client_id,
                              "client_secret": client_secret, "scope": "PRINCIPAL_ROLE:ALL"})
    token = _json(raw).get("access_token") if status == 200 else None
    report.check(bool(token), f"a token as {who}", f"HTTP {status}")
    return token


def created(report, status, raw, what):
    """A create answered 200 or 201, or 409 for one already there, kept."""
    return report.check(status in (200, 201, 409),
                        f"{what} " + ("already there, kept" if status == 409 else "created"),
                        f"HTTP {status}{polaris_error(raw)}")


def seed(http, report, polaris, realm, catalog, s3_endpoint, region, bucket, credential):
    token = polaris_token(http, report, polaris, realm, credential, "root")
    if not token:
        return
    headers = {"Polaris-Realm": realm, "Authorization": f"Bearer {token}"}
    location = f"s3://{bucket}/{catalog}"
    status, _, raw = http("POST", f"{polaris}/api/management/v1/catalogs", headers, {"catalog": {
        "type": "INTERNAL", "name": catalog,
        "properties": {"default-base-location": location},
        "storageConfigInfo": {"storageType": "S3", "allowedLocations": [location],
                              "endpoint": s3_endpoint, "region": region,
                              "pathStyleAccess": True, "stsUnavailable": True}}})
    if not created(report, status, raw, f"catalog {catalog}"):
        return
    base = f"{polaris}/api/catalog/v1/{catalog}/namespaces"
    status, _, raw = http("POST", base, headers, {"namespace": [INGEST_NAMESPACE]})
    if not created(report, status, raw, f"namespace {INGEST_NAMESPACE}"):
        return
    for table, fields in TABLES.items():
        status, _, raw = http("POST", f"{base}/{INGEST_NAMESPACE}/tables",
                              {**headers, DELEGATION: ""},
                              {"name": table, "schema": {"type": "struct", "schema-id": 0,
                                                         "fields": fields}})
        created(report, status, raw, f"table {INGEST_NAMESPACE}.{table}")


def refused(http, report, polaris, realm, catalog, credential):
    token = polaris_token(http, report, polaris, realm, credential, "datahub_ingest")
    if not token:
        return
    headers = {"Polaris-Realm": realm, "Authorization": f"Bearer {token}"}
    base = f"{polaris}/api/catalog/v1/{catalog}/namespaces"
    status, _, _ = http("POST", base, headers, {"namespace": ["refused_by_policy"]})
    report.check(status == 403, f"datahub_ingest creating a namespace in {catalog} refused",
                 f"HTTP {status}")
    table = next(iter(TABLES))
    url = f"{base}/{INGEST_NAMESPACE}/tables/{table}"
    status, _, _ = http("GET", url, {**headers, DELEGATION: "vended-credentials"})
    report.check(status == 403, f"datahub_ingest loading {INGEST_NAMESPACE}.{table} with vended "
                 "credentials refused", f"HTTP {status}")
    status, _, raw = http("GET", url, {**headers, DELEGATION: ""})
    report.check(status == 200, f"datahub_ingest loading {INGEST_NAMESPACE}.{table} without "
                 "them allowed", f"HTTP {status}{polaris_error(raw)}")


def ingested_urn(catalog, table):
    """The URN Datahub's Iceberg source gives a table, its catalog the platform instance."""
    return f"urn:li:dataset:({INGEST_PLATFORM},{catalog}.{INGEST_NAMESPACE}.{table},PROD)"


def simple_name(field_path):
    """A v2 field path without Datahub's [version=...] and [type=...] tokens."""
    return re.sub(r"\[[^\]]*\]\.?", "", field_path)


def field_names(fields, prefix=""):
    names = []
    for field in fields:
        name = prefix + field["name"]
        names.append(name)
        if isinstance(field["type"], dict):
            names += field_names(field["type"]["fields"], name + ".")
    return names


def read_aspect(http, gms, headers, urn, aspect, key):
    """(status, the aspect's value or None)."""
    quoted = urllib.parse.quote(urn, safe="")
    status, _, raw = http("GET", f"{gms}/aspects/{quoted}?aspect={aspect}&version=0", headers)
    return status, (_json(raw).get("aspect") or {}).get(key)


SCHEMA = ("schemaMetadata", "com.linkedin.schema.SchemaMetadata")
CONTAINER = ("container", "com.linkedin.container.Container")
CONTAINER_PROPERTIES = ("containerProperties", "com.linkedin.container.ContainerProperties")


def ingested(http, report, gms, headers, catalog):
    for table, fields in TABLES.items():
        urn = ingested_urn(catalog, table)
        status, schema = read_aspect(http, gms, headers, urn, *SCHEMA)
        found = sorted(simple_name(f["fieldPath"]) for f in (schema or {}).get("fields") or [])
        wanted = field_names(fields)
        report.check(set(wanted) <= set(found),
                     f"{urn} ingested with its fields {', '.join(wanted)}",
                     f"HTTP {status}, fields {found}")
        status, container = read_aspect(http, gms, headers, urn, *CONTAINER)
        container_urn = (container or {}).get("container")
        if not report.check(bool(container_urn), f"{urn} is in a container",
                            f"HTTP {status}, {container}"):
            continue
        status, properties = read_aspect(http, gms, headers, container_urn,
                                         *CONTAINER_PROPERTIES)
        report.check((properties or {}).get("name") == INGEST_NAMESPACE,
                     f"its container {container_urn} is the namespace {INGEST_NAMESPACE}",
                     f"HTTP {status}, {properties}")


def delete_urn(http, report, gms, headers, urn, aspect, key):
    status, _, _ = http("POST", f"{gms}/entities?action=delete", headers, {"urn": urn})
    report.check(status == 200, f"{urn} hard-deleted", f"HTTP {status}")
    status, value = read_aspect(http, gms, headers, urn, aspect, key)
    report.check(value is None, f"{urn} reads back nothing after the delete",
                 f"HTTP {status}, {value}")


def clean(http, report, gms, headers, catalog):
    containers = []
    for table in TABLES:
        urn = ingested_urn(catalog, table)
        _, container = read_aspect(http, gms, headers, urn, *CONTAINER)
        if (container or {}).get("container") and container["container"] not in containers:
            containers.append(container["container"])
        delete_urn(http, report, gms, headers, urn, *SCHEMA)
    for urn in containers:
        delete_urn(http, report, gms, headers, urn, *CONTAINER_PROPERTIES)


def run(argv, secret, http=urllib_http, out=sys.stdout, sleep=time.sleep, clock=time.monotonic):
    report = Report(secret, out)
    step = argv[0]
    if step == "gms":
        _, gms, name, seconds = argv
        check_gms(http, report, gms, secret, name, int(seconds), sleep, clock)
    elif step == "login":
        _, frontend = argv
        check_login(http, report, frontend, secret)
    elif step == "write":
        _, gms, name = argv
        headers = system_headers(secret)
        if write_dataset(http, report, gms, headers, name):
            read_back(http, report, gms, headers, name)
    elif step == "read":
        _, gms, name = argv
        read_back(http, report, gms, system_headers(secret), name)
    elif step == "search":
        _, gms, name, seconds = argv
        search(http, report, gms, system_headers(secret), name, int(seconds), sleep, clock)
    elif step == "seed":
        _, polaris, realm, catalog, s3_endpoint, region, bucket = argv
        seed(http, report, polaris, realm, catalog, s3_endpoint, region, bucket, secret)
    elif step == "refused":
        _, polaris, realm, catalog = argv
        refused(http, report, polaris, realm, catalog, secret)
    elif step == "ingested":
        _, gms, catalog = argv
        ingested(http, report, gms, system_headers(secret), catalog)
    elif step == "clean":
        _, gms, catalog = argv
        clean(http, report, gms, system_headers(secret), catalog)
    else:
        _, gms, *names = argv
        for name in names:
            delete_dataset(http, report, gms, system_headers(secret), name)
    return 1 if report.failed else 0


# The number of arguments each step takes; `delete` takes one name or more.
ARGUMENTS = {"gms": 4, "login": 2, "write": 3, "read": 3, "search": 4, "seed": 7,
             "ingested": 3, "refused": 4, "clean": 3}


def valid(args):
    if args[:1] == ["delete"]:
        return len(args) >= 3
    return bool(args) and ARGUMENTS.get(args[0]) == len(args)


def main(argv, stdin=sys.stdin):
    args = argv[1:]
    if not valid(args):
        print("\n".join(line.strip() for line in __doc__.strip().splitlines()[4:14]),
              file=sys.stderr)
        return 2
    if args[0] == "refused":
        client_id, client_secret = os.environ.get("CLIENT_ID"), os.environ.get("CLIENT_SECRET")
        secret = f"{client_id}:{client_secret}" if client_id and client_secret else ""
    else:
        secret = stdin.readline().strip()
    if not secret:
        print("FAIL no credential on stdin", flush=True)
        return 1
    return run(args, secret)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
