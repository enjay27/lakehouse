"""The stress scenario's data (ADR-053 §3): run by `gates/stress/driver.py` in a short-lived pod
on the pinned Python image, after `pip install --require-hashes -r requirements.txt` (the smoke
tests' PyIceberg, gates/iceberg/requirements.txt) for `seed`; `verify` needs the standard
library only.

    python3 seed.py seed POLARIS_URL REALM S3_ENDPOINT REGION BUCKET
    python3 seed.py verify POLARIS_URL REALM

Both read /stress/config.json (gates/stress/config.json); `verify` also reads
/stress/tables.json, the seed's result. Credentials come from the environment, never the
command line: ROOT_CLIENT_ID and ROOT_CLIENT_SECRET (the Secret polaris-root), and for `seed`
S3_ACCESS_KEY_ID and S3_SECRET_ACCESS_KEY (the Secret smoke-s3).

seed:
1. Root creates the catalog gate_stress_catalog on the S3 endpoint as the smoke tests create
   theirs (gates/iceberg/smoke.py; one already there is kept), and the principal gate_stress,
   which the gate's policy lets work inside that catalog only; one already there gets new
   credentials.
2. PyIceberg as gate_stress creates the namespaces ns00, ns01, ... and in each the tables t00,
   t01, ..., format v2, dropping all but the last `previous_versions_max` metadata files after
   each commit (`write.metadata.delete-after-commit.enabled`), so the runner's disk stays
   bounded: a deviation from the target's defaults the results must name. Each table gets one
   real append: a data file, a manifest and a manifest list.
3. Over the REST API, as gate_stress, each table gets batches of `add-snapshot` updates that
   reuse its manifest list, with rising sequence numbers, and `main` moved to the last, until
   a load of it is at least `metadata_bytes` long. Polaris does not read a manifest list on a
   commit; that it accepts one it never checks is confirmed by the first run.

verify: loads every table of tables.json as gate_stress and lists, per table, how many
snapshots it has, the ids of those newer than the seed (sequence number above the seeded one)
and any id listed twice.

Progress goes out as `ok`/`FAIL` lines, credentials masked; the result as one line starting
with RESULT, holding no credential. Exits 1 when anything failed.
"""

import collections
import concurrent.futures
import json
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
# smoke.py sits beside this file in the pod and in gates/iceberg/ in the repository.
sys.path[:0] = [HERE, os.path.join(os.path.dirname(HERE), "iceberg")]
import smoke  # noqa: E402

STRESS, CATALOG = "gate_stress", "gate_stress_catalog"
RESULT = "RESULT "
CONFIG_PATH, TABLES_PATH = "/stress/config.json", "/stress/tables.json"
ROWS = [{"id": 1, "name": "seed"}]
WORKERS = 8
TIMEOUT = 120


def tables(config):
    """Every table, `nsNN.tNN`, in the order the hot set is taken from."""
    s = config["seed"]
    return [f"ns{n:02d}.t{t:02d}" for n in range(s["namespaces"])
            for t in range(s["tables_per_namespace"])]


def hot(config):
    return tables(config)[:config["hot"]["tables"]]


def table_properties(config):
    return {"format-version": "2", "write.metadata.delete-after-commit.enabled": "true",
            "write.metadata.previous-versions-max": str(config["seed"]["previous_versions_max"])}


def table_state(metadata):
    """The current snapshot, its manifest list and the table's last sequence number."""
    current = metadata["current-snapshot-id"]
    (snap,) = [s for s in metadata["snapshots"] if s["snapshot-id"] == current]
    return {"snapshot": current, "sequence": metadata["last-sequence-number"],
            "manifest_list": snap["manifest-list"]}


def snapshot_updates(state, count, now_ms, new_id):
    """`count` add-snapshot updates chained on `state`, each with the next sequence number and
    the same manifest list, timestamps rising to now_ms; then `main` moved to the last. Returns
    (updates, the state after them)."""
    updates, parent, sequence = [], state["snapshot"], state["sequence"]
    for i in range(count):
        snapshot_id, sequence = new_id(), sequence + 1
        updates.append({"action": "add-snapshot", "snapshot": {
            "snapshot-id": snapshot_id, "parent-snapshot-id": parent,
            "sequence-number": sequence, "timestamp-ms": now_ms - count + 1 + i,
            "manifest-list": state["manifest_list"], "summary": {"operation": "append"},
            "schema-id": 0}})
        parent = snapshot_id
    updates.append({"action": "set-snapshot-ref", "ref-name": "main", "type": "branch",
                    "snapshot-id": parent})
    return updates, {**state, "snapshot": parent, "sequence": sequence}


def commit_body(state, updates):
    return {"requirements": [{"type": "assert-ref-snapshot-id", "ref": "main",
                              "snapshot-id": state["snapshot"]}],
            "updates": updates}


def urllib_http(method, url, headers, body=None, form=None):
    """(status, the raw answer): one request, no exception for an HTTP error status."""
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers = {**headers, "Content-Type": "application/x-www-form-urlencoded"}
    elif body is not None:
        data = json.dumps(body).encode()
        headers = {**headers, "Content-Type": "application/json"}
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310
            return response.status, response.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def json_http(http):
    """smoke.py's interface, (status, the JSON answer or {}), over `http`."""
    def call(method, url, headers, body=None, form=None):
        status, raw = http(method, url, headers, body=body, form=form)
        try:
            return status, json.loads(raw) if raw else {}
        except ValueError:
            return status, {}
    return call


def token(http, polaris, realm, client_id, client_secret):
    status, raw = http("POST", f"{polaris}/api/catalog/v1/oauth/tokens",
                       {"Polaris-Realm": realm},
                       form={"grant_type": "client_credentials", "client_id": client_id,
                             "client_secret": client_secret, "scope": "PRINCIPAL_ROLE:ALL"})
    return json.loads(raw).get("access_token") if status == 200 else None


def table_url(polaris, name):
    namespace, table = name.split(".")
    return f"{polaris}/api/catalog/v1/{CATALOG}/namespaces/{namespace}/tables/{table}"


def stress_session(http, report, polaris, realm, env):
    """Root's headers and gate_stress's `clientId:clientSecret`, or None."""
    root = smoke.root_token(json_http(http), polaris, realm, env)
    if not root:
        report.bad(f"root got no token in realm {realm}")
        return None
    headers = {"Authorization": f"Bearer {root}", "Polaris-Realm": realm}
    credential = smoke.principal_credential(json_http(http), report, polaris, headers, STRESS)
    if credential is None:
        return None
    report.secrets.append(credential.split(":", 1)[1])
    return headers, credential


def stress_headers(http, report, polaris, realm, credential):
    access = token(http, polaris, realm, *credential.split(":", 1))
    if not access:
        report.bad(f"{STRESS} got no token")
        return None
    return {"Authorization": f"Bearer {access}", "Polaris-Realm": realm}


def grow(http, polaris, headers, name, config, now, new_id):
    """Batches on table `name` until a load of it is at least `metadata_bytes` long. Returns
    its seeded state, or raises RuntimeError."""
    s, url = config["seed"], table_url(polaris, name)
    for _ in range(s["max_batches"] + 1):
        status, raw = http("GET", url, headers)
        if status != 200:
            raise RuntimeError(f"loading {name}: HTTP {status}")
        metadata = json.loads(raw)["metadata"]
        state = table_state(metadata)
        if len(raw) >= s["metadata_bytes"]:
            return {"name": name, "hot": False, "manifest_list": state["manifest_list"],
                    "snapshots": len(metadata["snapshots"]), "sequence": state["sequence"],
                    "metadata_bytes": len(raw)}
        updates, _ = snapshot_updates(state, s["batch"], int(now() * 1000), new_id)
        status, _ = http("POST", url, headers, body=commit_body(state, updates))
        if status != 200:
            raise RuntimeError(f"committing {s['batch']} snapshots to {name}: HTTP {status}")
    raise RuntimeError(f"{name} is {len(raw)} bytes after {s['max_batches']} batches, "
                       f"expected {s['metadata_bytes']}")


def seed(argv, config, env, http=urllib_http, iceberg=None, out=sys.stdout, now=time.time,
         new_id=lambda: random.SystemRandom().randrange(1, 2 ** 62)):
    polaris, realm, s3_endpoint, region, bucket = argv
    report = smoke.Report(env, out)
    session = stress_session(http, report, polaris, realm, env)
    if session is None:
        return 1
    root, credential = session
    if not smoke.create_catalog(json_http(http), report, polaris, root,
                                smoke.catalog_request(CATALOG, bucket, s3_endpoint, region)):
        return 1
    iceberg = iceberg or smoke.PyIceberg()
    catalog = iceberg.catalog(smoke.catalog_properties(polaris, realm, CATALOG, credential,
                                                       s3_endpoint, region, env))
    names = tables(config)
    try:
        present = {".".join(n) for n in catalog.list_namespaces()}
        for namespace in sorted({n.split(".")[0] for n in names} - present):
            catalog.create_namespace(namespace)
        for name in names:
            catalog.create_table(name, schema=iceberg.schema,
                                 properties=table_properties(config)).append(
                iceberg.arrow(ROWS))
    except Exception as e:  # noqa: BLE001 (reported, masked, and the seed fails)
        report.bad(f"{STRESS} creating and appending the tables: {smoke._error(e)}")
        return 1
    report.ok(f"{STRESS} created and appended {len(names)} tables")
    headers = stress_headers(http, report, polaris, realm, credential)
    if headers is None:
        return 1
    seeded, hot_set = [], set(hot(config))
    with concurrent.futures.ThreadPoolExecutor(WORKERS) as pool:
        futures = [pool.submit(grow, http, polaris, headers, n, config, now, new_id)
                   for n in names]
        for future in futures:
            try:
                seeded.append(future.result())
            except RuntimeError as e:
                report.bad(str(e))
    if report.failed:
        return 1
    for t in seeded:
        t["hot"] = t["name"] in hot_set
    sizes = [t["metadata_bytes"] for t in seeded]
    report.ok(f"{len(seeded)} tables seeded, loads of {min(sizes)} to {max(sizes)} bytes, "
              f"{sum(t['snapshots'] for t in seeded)} snapshots")
    print(RESULT + json.dumps({"catalog": CATALOG, "tables": seeded}, separators=(",", ":")),
          file=out, flush=True)
    return 0


def snapshots_after(metadata, sequence):
    ids = [s["snapshot-id"] for s in metadata["snapshots"]]
    return {"snapshots": len(ids),
            "new": [str(s["snapshot-id"]) for s in metadata["snapshots"]
                    if s["sequence-number"] > sequence],
            "duplicates": sorted(str(i) for i, n in collections.Counter(ids).items() if n > 1)}


def verify(argv, seeded, env, http=urllib_http, out=sys.stdout):
    polaris, realm = argv
    report = smoke.Report(env, out)
    session = stress_session(http, report, polaris, realm, env)
    if session is None:
        return 1
    headers = stress_headers(http, report, polaris, realm, session[1])
    if headers is None:
        return 1
    found = {}
    for t in seeded["tables"]:
        status, raw = http("GET", table_url(polaris, t["name"]), headers)
        if status != 200:
            report.bad(f"loading {t['name']}: HTTP {status}")
            continue
        found[t["name"]] = snapshots_after(json.loads(raw)["metadata"], t["sequence"])
    if report.failed:
        return 1
    report.ok(f"{len(found)} tables loaded, "
              f"{sum(len(f['new']) for f in found.values())} snapshots newer than the seed")
    print(RESULT + json.dumps({"tables": found}, separators=(",", ":")), file=out, flush=True)
    return 0


def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def main(argv):
    if len(argv) == 7 and argv[1] == "seed":
        return seed(argv[2:], _load(CONFIG_PATH), os.environ)
    if len(argv) == 4 and argv[1] == "verify":
        return verify(argv[2:], _load(TABLES_PATH), os.environ)
    print("Usage: seed.py seed POLARIS_URL REALM S3_ENDPOINT REGION BUCKET | "
          "verify POLARIS_URL REALM", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
