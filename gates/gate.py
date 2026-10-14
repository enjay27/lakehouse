"""The kind gate (ADR-017, ADR-029, ADR-038): pinned tools, the cluster, the CNPG operator.

  tools BIN_DIR [NAME...]
                  downloads kind, kubectl, helm and kubeconform (or only the NAMEs) from
                  gates/pins.json into BIN_DIR, each checked against its pinned sha256 before
                  anything is written.
  up              creates the kind cluster (one control-plane, two workers, the pinned node
                  image), lets the control-plane take pods, so three nodes hold the three
                  PostgreSQL instances apart, then installs the CNPG operator from its pinned
                  chart archive into NS_CNPG_SYSTEM and waits for it. The operator exists here
                  only because the gate has no operator team (ADR-029); a release never
                  installs it. After it, cert-manager and the Barman Cloud Plugin from their
                  pinned release manifests, the plugin in NS_CNPG_SYSTEM, each waited for
                  (ADR-043). Before CNPG, kind's local-path-provisioner gets the class
                  `shared` on a runner directory every node mounts: ReadWriteMany for the
                  log claim (ADR-036).
  secrets         writes `.env` for the gate (its context, the stand-ins' endpoints and
                  the S3 stand-in's three keys, Polaris's, the smoke tests' and the backups',
                  which are the gate's own, ADR-031, ADR-043), then runs
                  scripts/init_env.py --apply-secrets (ADR-006). Refuses an existing `.env`.
  standins        applies the stand-ins (gates/standins.py: SeaweedFS for S3, OPA with
                  gates/opa/policy.rego) in NS_STANDINS and waits for them (ADR-031), then
                  creates the backup bucket in SeaweedFS and checks it (ADR-043). Never
                  part of a release; the install's endpoint check needs them on the gate.
                  With them the recorder (ADR-034), with a TLS certificate made here, and
                  Prometheus loading docs/contracts/polaris-prometheus-scrape.yaml (ADR-046).
  check-standins  runs gates/check_standins.sh in a short-lived curl pod in Polaris's namespace:
                  an S3 round trip with Polaris's credentials, and every case of
                  gates/opa/cases.json against the policy endpoint (ADR-026, ADR-031); then
                  gates/check_db.sh in a pod on Polaris's PostgreSQL image: a login as Polaris
                  with and without TLS, a row written and read. All through the recorder.
  check-traffic FILE...
                  reads every record the recorder wrote and matches them against each
                  expectation file (gates/expectations/*.json, gates/traffic.py): exchanges that
                  must have happened and ones that must not (ADR-034). Prints counts, never a body.
  install [--fresh] [--dry-run]
                  installs the latest release HEAD contains (released_tags) from a checkout of
                  its tag, with the gate's `.env` copied into it, then this commit's release
                  over it (ADR-051); with no tag, or with
                  --fresh, only this commit's release. With --dry-run, this commit's release is
                  a server-side dry-run over the installed one (ADR-038). Prints which it did,
                  then the image pulls since it started, from the kubelet's Pulled events:
                  each image's pulls and nodes, and the busiest node's pull time against the
                  install's (#125). A measurement that fails says so and never fails it.
  rebuild-replica deletes one replica of polaris-pg, never the primary: its PVC without waiting,
                  then its pod. CNPG makes a new instance through a join pod that clones the
                  primary under the network policies; it passes once a new join Job ran and the
                  Cluster is Ready with all its instances, within REBUILD_TIMEOUT s. On failure,
                  the join pods' logs (ADR-048 §7). Gate only: it deletes a volume.
  check-polaris-pg
                  compares the running `Cluster` polaris-pg with the values its helm release
                  was installed with, and checks its instances run on different nodes
                  (ADR-016).
  check-polaris   compares the running Polaris (its Deployment, pods and ConfigMap) with the
                  values its helm release was installed with and with `.env`, checks its
                  replicas run on different nodes (ADR-016), then runs gates/check_polaris.sh
                  in a short-lived curl pod: ready, a token for root, and each pod accepting
                  it (ADR-026).
  console-image   builds the Polaris console's image (ADR-028): apache/polaris-tools fetched at
                  the commit in images/polaris-console/source.json and checked, its console/ as
                  the context with this repository's package-lock.json, built with
                  images/polaris-console/Dockerfile, then loaded into the kind cluster.
  batch-image     builds the log batch's image from images/polaris-log-batch, tagged as
                  install/release.py names it, and loads it into the kind cluster (ADR-036).
  check-logs      follows traffic to the hand-off (ADR-024, ADR-036): gates/logs_traffic.sh in
                  a short-lived curl pod makes the catalog gate_logs on the S3 endpoint, then
                  sends each Polaris pod reads with root's token, requests without one, a 400,
                  a 404 and a table made, each with its own X-Request-ID; a Job from the
                  batch's CronJob runs with its clock past that hour, then again; a stand-in
                  Fluent Bit (pinned, the claim read-only) reads processed-logs/ and
                  aggregated-logs/. Every line and row it read must hold contract v2
                  (gates/contract.py) and every summary's books balance; each refused request
                  must be kept once from its pod, no read kept but all counted on the rows; the
                  policy's rules on Polaris's own lines: the 404s dropped with their
                  application lines (3'), the 400s and the catalog made kept with theirs (2b),
                  each commit counted on its table's row (2a); both pods' files read; the second
                  run must change no file.
  check-console   runs gates/console/check.py (on stdin) in a short-lived pod on the pinned
                  Python image: the console answers and points the browser at Polaris, and
                  gate_maintainer signs in as the browser does, from the console's origin,
                  lists the catalogs and is refused a write (ADR-028, ADR-026).
  check-iceberg   runs gates/iceberg/smoke.py in a short-lived pod on the pinned Python image,
                  mounted with its hashed requirements from the ConfigMap iceberg-smoke: PyIceberg
                  as gate_smoke, with the smoke tests' own S3 key (Secret smoke-s3), writes a
                  table into gate_catalog on the S3 endpoint and reads it back, and the policy
                  endpoint refuses what the gate's policy does not grant (ADR-026, ADR-031).
  check-restore   proves a restore (ADR-043 §4): an on-demand `Backup` of polaris-pg through
                  the Barman Cloud Plugin, waited for until completed; then the `Cluster`
                  polaris-pg-restore, one instance bootstrapped from the ObjectStore
                  polaris-backup, its base backup and archived WAL to the latest, waited for
                  until Ready, read with its full-recovery Job's pods every few seconds: each
                  failed pod's logs printed as soon as it fails, before CNPG deletes it, then
                  every pod seen, when it ran and how it ended (#86); then healthy on several
                  reads in a row, past
                  CNPG's restart after the recovery; then gates/check_restore.sh in a pod on
                  Polaris's PostgreSQL image reads from it, as Polaris, the realm's root
                  principal and gate_catalog.
                  polaris-pg-restore is deleted whatever happened.

  gate-policies   applies the gate's own allow policies in Polaris's namespace (gates/network.py,
                  ADR-048 §5): the check pods, labelled lakehouse-gate/check, reach every pod;
                  the PostgreSQL recorder reaches the CNPG instances on 5432. `install` runs
                  it before the first release it installs for real: the last release, or this
                  commit's when there is none to upgrade from.
  check-network   creates a namespace per caller class with the gate's label, one without any
                  label and Datahub's, then from a short-lived pod in each, from an
                  unlabelled pod in Polaris's namespace, and from a pod with CNPG's join label
                  in Polaris's namespace and in the unlabelled one, connects to each target of
                  gates/network.py's CASES: each allowed one must connect, each refused one
                  must time out or be reset (ADR-048 §6). One that should be refused and
                  connects means the cluster does not enforce NetworkPolicy. polaris-pg's
                  exporter on 9187 is reached through a Service the gate adds (ADR-046 §4).
  check-metrics   asks the Prometheus stand-in, from a check pod, until every target is up:
                  `up` 1 for every running Polaris pod and polaris-pg instance, Polaris's
                  http_server_requests_seconds_count above 0, cnpg_collector_up 1 on every
                  instance (gates/metrics.py, ADR-046 §5). On a problem, every target with
                  its last error.
Every kubectl and helm call names the gate's context, and a namespace where the object has
one. Nothing here reaches any cluster but the kind cluster it creates (ADR-017).

Usage: gate.py tools BIN_DIR [NAME...] | up | secrets | standins | check-standins | check-traffic FILE... | install [--fresh] [--dry-run] | rebuild-replica | check-polaris-pg | check-polaris | console-image | batch-image | check-logs | check-console | check-iceberg | check-restore | gate-policies | check-network | check-metrics
"""

import copy
import datetime as dt
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "install"))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import contract  # noqa: E402
import init_env  # noqa: E402
import metrics  # noqa: E402
import network  # noqa: E402
import release  # noqa: E402
import release_tag  # noqa: E402
import standins  # noqa: E402
import traffic  # noqa: E402

CLUSTER = "lakehouse-gate"
CONTEXT = f"kind-{CLUSTER}"
TOOLS = ("kind", "kubectl", "helm", "kubeconform")
# The binary inside each tool's archive; the others are plain binaries.
ARCHIVE_MEMBERS = {"helm": "linux-amd64/helm", "kubeconform": "kubeconform"}
# The release manifests' own names (gates/pins.json `manifests`, ADR-043 §3).
CERT_MANAGER_NAMESPACE = "cert-manager"
CERT_MANAGER_DEPLOYMENTS = ("cert-manager", "cert-manager-cainjector", "cert-manager-webhook")
PLUGIN_DEPLOYMENT = "barman-cloud"
# On the gate the S3 stand-in is the gate's own, so the gate makes up its credentials, Polaris's
# the smoke tests' and the backups'; on a test or target cluster they are another team's and
# typed in by hand (ADR-031, ADR-043).
GATE_ENV_GENERATED = ("POLARIS_S3_ACCESS_KEY_ID", "POLARIS_S3_SECRET_ACCESS_KEY",
                      "SMOKE_S3_ACCESS_KEY_ID", "SMOKE_S3_SECRET_ACCESS_KEY",
                      "POLARIS_BACKUP_S3_ACCESS_KEY_ID", "POLARIS_BACKUP_S3_SECRET_ACCESS_KEY")
# On the gate, the endpoints of other teams' services are the stand-ins' (ADR-031).
STANDIN_REGION = "us-east-1"
STANDIN_BUCKET = "polaris-warehouse"
STANDIN_BACKUP_BUCKET = "polaris-backup"
# Polaris's own S3 key, as the AWS SDK reads it: catalogs without STS are read and written with
# it (charts/polaris/values-lakehouse.yaml).
POLARIS_S3_ENV = {"AWS_ACCESS_KEY_ID": "accessKeyId", "AWS_SECRET_ACCESS_KEY": "secretAccessKey"}
# Polaris's pods and ports, as charts/polaris renders them.
POLARIS_POD_LABELS = "app.kubernetes.io/name=polaris,app.kubernetes.io/instance=polaris"
POLARIS_API_PORT, POLARIS_MGMT_PORT = 8181, 8182
# Every pod the gate starts in Polaris's namespace to check it carries this label, which the
# gate's own allow policy lets reach every pod there (ADR-048 §5).
CHECK_POD_LABEL = f"--labels={network.CHECK_LABEL}=true"
# The catalog the smoke tests create and the gate's policy names (gates/opa/policy.rego).
ICEBERG_CATALOG = "gate_catalog"
# ReadWriteMany on kind for the log claim (ADR-036): one runner directory mounted into every
# node (gates/kind.yaml), offered by kind's local-path-provisioner as the class `shared`
# through its sharedFileSystemPath. A stand-in, never released (ADR-031).
SHARED_HOST_DIR = "/tmp/lakehouse-gate-shared"
SHARED_NODE_DIR = "/var/local-path-shared"
SHARED_CLASS = "shared"
GATE_LOGS_SIZE = "1Gi"
# kind's own default, which the class `standard` keeps.
STANDARD_NODE_PATHS = [{"node": "DEFAULT_PATH_FOR_NON_LISTED_NODES",
                        "paths": ["/var/local-path-provisioner"]}]
# The Polaris console's image, built here from a pinned polaris-tools commit (ADR-028).
CONSOLE_IMAGE_DIR = os.path.join(ROOT, "images", "polaris-console")
CONSOLE_IMAGE_REPOSITORY = "lakehouse/polaris-console"
CONSOLE_PORT = 8080
# The log batch on the gate (ADR-036): requests sent to each Polaris pod, how long a run of the
# batch or of the stand-in Fluent Bit may take, and where the claim is mounted to be read.
LOGS_REQUESTS = 5
LOGS_CATALOG = "gate_logs"
LOGS_NAMESPACE = "logs"
LOGS_POLICY_KINDS = ("invalid", "missing", "commit")
APP_LINE_LOGGERS = ("org.apache.polaris.service.admin.PolarisServiceImpl",
                    "org.apache.polaris.service.exception.IcebergExceptionMapper")
LOGS_TIMEOUT = 300
LOGS_MOUNT = "/logs"
OUTPUT_DIRS = ("processed-logs", "aggregated-logs")
# The stand-in reads as a user that is neither root nor Polaris's and the batch's (10000), as
# the observability team's reader would (ADR-024).
READER_UID = 65534
# Run in a short-lived pod with the claim at $1, read-only: one line per output file, its
# SHA-256, its line count and its path under the claim.
LOGS_LISTING = """cd "$1" || exit 1
for f in processed-logs/*.jsonl aggregated-logs/*.jsonl; do
  [ -f "$f" ] || continue
  printf '%s %s %s\\n' "$(sha256sum < "$f" | cut -c1-64)" "$(($(wc -l < "$f")))" "$f"
done
"""


def load_pins(path=os.path.join(HERE, "pins.json")):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def download(url):
    with urllib.request.urlopen(url, timeout=120) as response:  # noqa: S310 (pinned https)
        return response.read()


def fetch(url, sha256, get=download):
    data = get(url)
    actual = hashlib.sha256(data).hexdigest()
    if actual != sha256:
        raise ValueError(f"{url}: sha256 {actual}, pinned {sha256}")
    return data


def tools(pins, bin_dir, names=TOOLS, get=download):
    """Fetches and checks every named tool first, then writes them; a bad hash writes nothing."""
    for name in names:
        if name not in TOOLS:
            raise KeyError(f"{name}: not a gate tool ({', '.join(TOOLS)})")
    blobs = {name: fetch(pins[name]["url"], pins[name]["sha256"], get) for name in names}
    for name, member in ARCHIVE_MEMBERS.items():
        if name in blobs:
            with tarfile.open(fileobj=io.BytesIO(blobs[name]), mode="r:gz") as tar:
                blobs[name] = tar.extractfile(member).read()
    os.makedirs(bin_dir, exist_ok=True)
    for name, data in blobs.items():
        path = os.path.join(bin_dir, name)
        with open(path, "wb") as f:
            f.write(data)
        os.chmod(path, 0o755)


def shared_storage_class():
    return {"apiVersion": "storage.k8s.io/v1", "kind": "StorageClass",
            "metadata": {"name": SHARED_CLASS}, "provisioner": "rancher.io/local-path",
            "reclaimPolicy": "Delete", "volumeBindingMode": "WaitForFirstConsumer"}


def plugin_manifest(text, namespace):
    """The plugin's release manifest, refused unless every namespace it names is `namespace`:
    it names its own, and the plugin must run in the operator's (ADR-043 §3)."""
    named = {line.split(":", 1)[1].strip() for line in text.splitlines()
             if line.strip().startswith("namespace:")}
    if not named:
        raise ValueError("the plugin's manifest names no namespace")
    if named != {namespace}:
        raise ValueError(f"the plugin's manifest names {', '.join(sorted(named))}, "
                         f"not the operator's namespace {namespace}")
    return text


def up_commands(pins, chart_path, namespace, manifests):
    """(argv, stdin) in order: the cluster, the control-plane taken off NoSchedule, the shared
    class next to kind's `standard` (the provisioner restarted to read it), CNPG, then
    cert-manager and the plugin from `manifests` (name to text), each waited for."""
    config = {"nodePathMap": [],
              "storageClassConfigs": {"standard": {"nodePathMap": STANDARD_NODE_PATHS},
                                      SHARED_CLASS: {"sharedFileSystemPath": SHARED_NODE_DIR}}}
    provisioner = ["kubectl", "--context", CONTEXT, "-n", "local-path-storage"]
    # Server-side: cert-manager's CRDs are too large for the last-applied annotation.
    apply = ["kubectl", "--context", CONTEXT, "apply", "--server-side", "--field-manager=gate",
             "-f", "-"]
    return [
        (["kind", "create", "cluster", "--name", CLUSTER,
          "--config", os.path.join(HERE, "kind.yaml"),
          "--image", pins["node_image"]["image"], "--wait", "180s"], None),
        (["kubectl", "--context", CONTEXT, "taint", "nodes", f"{CLUSTER}-control-plane",
          "node-role.kubernetes.io/control-plane:NoSchedule-"], None),
        (provisioner + ["patch", "configmap", "local-path-config", "--type", "merge", "-p",
                        json.dumps({"data": {"config.json": json.dumps(config)}})], None),
        (["kubectl", "--context", CONTEXT, "apply", "-f", "-"],
         json.dumps(shared_storage_class())),
        (provisioner + ["rollout", "restart", "deployment/local-path-provisioner"], None),
        (provisioner + ["rollout", "status", "deployment/local-path-provisioner",
                        "--timeout=3m"], None),
        (["helm", "--kube-context", CONTEXT, "install", "cnpg", chart_path,
          "--namespace", namespace, "--create-namespace", "--wait", "--timeout", "5m"], None),
        (apply, manifests["cert_manager"]),
        (["kubectl", "--context", CONTEXT, "-n", CERT_MANAGER_NAMESPACE, "rollout", "status",
          *[f"deployment/{d}" for d in CERT_MANAGER_DEPLOYMENTS], "--timeout=5m"], None),
        (apply, manifests["barman_cloud_plugin"]),
        (["kubectl", "--context", CONTEXT, "-n", namespace, "rollout", "status",
          f"deployment/{PLUGIN_DEPLOYMENT}", "--timeout=5m"], None),
    ]


def standin_endpoints(ns):
    """The `.env` values for the gate: every connection Polaris makes goes through the
    recorder in namespace `ns`, which forwards it to the stand-in or to polaris-pg (ADR-034)."""
    recorder = f"recorder.{ns}.svc.cluster.local"
    return {
        "S3_ENDPOINT": f"http://{recorder}:{standins.S3_PORT}",
        "S3_REGION": STANDIN_REGION,
        "POLARIS_WAREHOUSE_BUCKET": STANDIN_BUCKET,
        "POLARIS_BACKUP_BUCKET": STANDIN_BACKUP_BUCKET,
        "POLARIS_OPA_URL": f"http://{recorder}:{standins.OPA_PORT}{standins.OPA_PACKAGE_PATH}",
        "POLARIS_DB_HOST": recorder,
    }


def console_settings(ns):
    """The `.env` values for the gate's console, in Polaris's namespace `ns`: enabled, opened
    at its Service, and reaching Polaris at its Service (ADR-028)."""
    return {
        "POLARIS_CONSOLE_ENABLED": "true",
        "POLARIS_CONSOLE_ORIGIN": f"http://{release.CONSOLE}.{ns}.svc.cluster.local:{CONSOLE_PORT}",
        "POLARIS_CONSOLE_API_URL": f"http://{release.POLARIS}.{ns}.svc.cluster.local:"
                                   f"{POLARIS_API_PORT}",
    }


def gate_env(template_text, gen=init_env.generate):
    """`.env` for the gate: its context, the stand-ins' endpoints, the S3 stand-in's
    credentials, the console's settings, the log claim's shared class and a namespace label
    per caller class (ADR-048 §6); init_env fills the rest."""
    template = init_env.parse(template_text)
    fixed = {"KUBE_CONTEXT": CONTEXT, **standin_endpoints(template["NS_STANDINS"]),
             **console_settings(template["NS_CATALOG_POLARIS"]),
             "POLARIS_LOGS_STORAGE_CLASS": SHARED_CLASS, "POLARIS_LOGS_SIZE": GATE_LOGS_SIZE,
             **network.GATE_CALLER_LABELS}
    lines = []
    for line in template_text.splitlines():
        name = line.split("=", 1)[0]
        if name in fixed:
            line = f"{name}={fixed[name]}"
        elif name in GATE_ENV_GENERATED:
            line = f"{name}={gen(name)}"
        lines.append(line)
    return "\n".join(lines) + "\n"


def write_gate_env(path, template_text):
    if os.path.exists(path):
        sys.exit(f"{path} exists: the gate writes its own .env and never overwrites one")
    init_env.write_env(path, gate_env(template_text))


def standins_commands(env, manifest):
    """(argv, stdin) in order: apply the stand-ins' List `manifest`, wait until all four are
    ready, then create the backup bucket with SeaweedFS's shell (`weed mini -bucket` makes only
    the warehouse's) and check that its filer lists it (ADR-043)."""
    ns = ["kubectl", "--context", CONTEXT, "-n", env["NS_STANDINS"]]
    bucket = env["POLARIS_BACKUP_BUCKET"]
    return [
        (["kubectl", "--context", CONTEXT, "apply", "--server-side", "--field-manager=gate",
          "-f", "-"], manifest),
        (ns + ["rollout", "status", "deployment/seaweedfs", "deployment/opa",
               "deployment/recorder", "deployment/prometheus", "--timeout=5m"], None),
        (ns + ["exec", "-i", "deployment/seaweedfs", "--", "weed", "shell"],
         f"s3.bucket.create -name {bucket}\n"),
        (ns + ["exec", "deployment/seaweedfs", "--", "curl", "-sf", "-o", "/dev/null",
               f"http://127.0.0.1:{standins.FILER_PORT}/buckets/{bucket}/"], None),
    ]


def recorder_certificate(env, directory):
    """A self-signed certificate and key (PEM) for the recorder's PostgreSQL TLS, made here for
    this gate run only (ADR-017, ADR-034)."""
    cert, key = os.path.join(directory, "tls.crt"), os.path.join(directory, "tls.key")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
                    "-subj", f"/CN=recorder.{env['NS_STANDINS']}.svc.cluster.local",
                    "-keyout", key, "-out", cert], check=True, capture_output=True)
    with open(cert, encoding="ascii") as c, open(key, encoding="ascii") as k:
        return c.read(), k.read()


def polaris_pg_values(path=os.path.join(ROOT, "charts", "polaris-postgres", "values.yaml")):
    """The chart's top-level `image` and `database`, read without a YAML library."""
    values = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            for key in ("image", "database"):
                if line.startswith(f"{key}:"):
                    values[key] = line.split(":", 1)[1].strip().strip("\"'")
    return values


def check_db_command(env, image, database, name):
    """The pod that runs check_db.sh (on stdin) as Polaris, through POLARIS_DB_HOST."""
    creds = [{"name": var, "valueFrom": {"secretKeyRef": {"name": "polaris-db", "key": key}}}
             for var, key in (("PGUSER", "username"), ("PGPASSWORD", "password"))]
    overrides = {"spec": {"containers": [{"name": name, "image": image, "env": creds,
                                          "stdin": True, "stdinOnce": True}]}}
    return ["kubectl", "--context", CONTEXT, "-n", env["NS_CATALOG_POLARIS"], "run", name,
            CHECK_POD_LABEL, f"--image={image}", "--restart=Never", "--rm", "-i", "--quiet",
            "--pod-running-timeout=3m", "--override-type=strategic",
            "--overrides", json.dumps(overrides),
            "--command", "--", "sh", "-s", "--", env["POLARIS_DB_HOST"], database]


def check_standins_command(env, cases, image, name):
    """The pod that runs check_standins.sh (on stdin) with Polaris's S3 credentials."""
    creds = [{"name": var, "valueFrom": {"secretKeyRef": {"name": "polaris-s3", "key": key}}}
             for var, key in (("AWS_ACCESS_KEY_ID", "accessKeyId"),
                              ("AWS_SECRET_ACCESS_KEY", "secretAccessKey"))]
    overrides = {"spec": {"containers": [{"name": name, "image": image, "env": creds,
                                          "stdin": True, "stdinOnce": True}]}}
    argv = ["kubectl", "--context", CONTEXT, "-n", env["NS_CATALOG_POLARIS"], "run", name,
            CHECK_POD_LABEL, f"--image={image}", "--restart=Never", "--rm", "-i", "--quiet",
            "--pod-running-timeout=3m", "--override-type=strategic",
            "--overrides", json.dumps(overrides),
            "--command", "--", "sh", "-s", "--", env["S3_ENDPOINT"],
            env["POLARIS_WAREHOUSE_BUCKET"], env["S3_REGION"], env["POLARIS_OPA_URL"]]
    for case in cases:
        argv += [case["name"], "true" if case["allow"] else "false",
                 json.dumps(case["input"], separators=(",", ":"))]
    return argv


def recorder_logs_command(env):
    return ["kubectl", "--context", CONTEXT, "-n", env["NS_STANDINS"], "logs",
            "deployment/recorder", "--tail=-1"]


# When this commit's install over the last release ended, for check-traffic's restart window.
INSTALL_RECORD = "/tmp/lakehouse-gate-install.json"


def restart_events_command(namespace):
    return ["kubectl", "--context", CONTEXT, "-n", namespace, "get", "events",
            "--field-selector",
            f"involvedObject.name={release.POLARIS_PG},reason=UpgradingInstance", "-o", "json"]


def restart_window(events, install_end):
    """polaris-pg's restart during the upgrade: from its first UpgradingInstance event to the
    end of this commit's install; None without both (Kade, 2026-10-12). CNPG emits no event
    when a restart ends, and step 1 waits for it, so the install's end bounds it."""
    if not install_end:
        return None
    times = [e.get("eventTime") or e.get("firstTimestamp") or e.get("lastTimestamp")
             for e in events.get("items", [])
             if e.get("involvedObject", {}).get("kind") == "Cluster"
             and e["involvedObject"].get("name") == release.POLARIS_PG
             and e.get("reason") == "UpgradingInstance"]
    times = [t for t in times if t and traffic.when(t) <= traffic.when(install_end)]
    return (min(times, key=traffic.when), install_end) if times else None


def check_traffic(log_text, expectation_lists, restart=None):
    """(problems, lines): a count of the records, the restart window if any, then one line per
    expectation."""
    recorded = traffic.records(log_text)
    by_proto = {p: sum(1 for r in recorded
                       if r.get("proto") == p and r.get("event") != "listening")
                for p in ("http", "pg")}
    lines = [f"Recorder: {by_proto['http'] + by_proto['pg']} records: {by_proto['http']} HTTP, "
             f"{by_proto['pg']} PostgreSQL."]
    if restart:
        lines.append(f"{release.POLARIS_PG} restarted during the upgrade from {restart[0]} to "
                     f"{restart[1]}: only there may a rule let through what it allows.")
    problems = []
    for expectations in expectation_lists:
        more_problems, more_lines = traffic.check(recorded, expectations, restart)
        problems += more_problems
        lines += more_lines
    return problems, lines


def install_plan(previous, previous_dir, fresh, dry_run=False):
    """The commands that install a release on the gate, and the line that says which. The
    release before this one runs from a worktree of its tag, with the gate's `.env`."""
    this = [["python3", os.path.join(ROOT, "install", "release.py")] + (
        ["--dry-run"] if dry_run else [])]
    # The gate's own allow policies (ADR-048 §5), before the first release that installs for
    # real: since platform-2026.10.14 the last release applies Polaris's policies too, and its
    # realm bootstrap reaches polaris-pg through the recorder (dry-run 38493145873). Never
    # before a dry-run alone, which changes nothing.
    policies = [["python3", os.path.join(HERE, "gate.py"), "gate-policies"]]
    then = " (server-side dry-run)" if dry_run else ""
    if fresh or not previous:
        commands = this if dry_run else policies + this
        if fresh:
            return commands, f"Fresh install of this commit{then}, as asked."
        return commands, f"No release to upgrade from: fresh install{then} (ADR-051)."
    return [
        *policies,
        ["git", "-C", ROOT, "worktree", "add", "--detach", previous_dir, previous],
        # release.py reads .env from its own checkout, and a tag's worktree has none.
        ["cp", "-p", os.path.join(ROOT, ".env"), os.path.join(previous_dir, ".env")],
        ["python3", os.path.join(previous_dir, "install", "release.py")],
        *this,
    ], f"Upgrade: {previous} installed from its tag, then this commit over it{then} (ADR-051)."


def pull_events_command():
    """The kubelet's Pulled events in every namespace: one per image pulled, or found already
    present, on a node (#125)."""
    return ["kubectl", "--context", CONTEXT, "get", "events", "--all-namespaces",
            "--field-selector", "reason=Pulled", "-o", "json"]


GO_UNITS = {"h": 3600.0, "m": 60.0, "s": 1.0, "ms": 1e-3, "us": 1e-6, "µs": 1e-6, "ns": 1e-9}
GO_DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)(ms|us|µs|ns|h|m|s)")


def go_seconds(text):
    """A Go duration as the kubelet prints it (`1m2.5s`, `512ms`) in seconds."""
    parts = GO_DURATION_RE.findall(text)
    if not parts or "".join(n + u for n, u in parts) != text:
        raise ValueError(f"not a Go duration: {text!r}")
    return sum(float(n) * GO_UNITS[u] for n, u in parts)


PULLED_RE = re.compile(r'Successfully pulled image "([^"]+)" in (\S+) \((\S+) including waiting\)'
                       r"(?:\. Image size: (\d+) bytes)?")
PRESENT_RE = re.compile(r'Container image "([^"]+)" already present on machine')


def image_pulls(events, since):
    """(pulls, present, unknown) from Pulled events last seen at or after `since`: each pull
    with its node, seconds, seconds with waiting and size; the (image, node) pairs already
    present; how many Pulled messages matched neither form."""
    pulls, present, unknown = [], set(), 0
    for e in events.get("items", []):
        at = e.get("lastTimestamp") or e.get("eventTime") or e.get("firstTimestamp")
        if e.get("reason") != "Pulled" or not at or traffic.when(at) < traffic.when(since):
            continue
        node = (e.get("source") or {}).get("host") or e.get("reportingInstance") or "?"
        message = e.get("message", "")
        m = PULLED_RE.match(message)
        if m:
            pulls.append({"image": m[1], "node": node, "seconds": go_seconds(m[2]),
                          "waiting": go_seconds(m[3]), "bytes": int(m[4]) if m[4] else None})
        elif PRESENT_RE.match(message):
            present.add((PRESENT_RE.match(message)[1], node))
        else:
            unknown += 1
    return pulls, present, unknown


def _count(n, word):
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def pull_lines(pulls, present, unknown, install_seconds):
    """A summary against the install's length, then one line per image, slowest pull first,
    then images only present, then each node's pull time. A kubelet pulls one image at a time
    by default, so the busiest node's sum bounds what pulls cost the install."""
    per_node = {}
    for p in pulls:
        per_node[p["node"]] = per_node.get(p["node"], 0.0) + p["seconds"]
    if per_node:
        busiest = max(per_node, key=per_node.get)
        lines = [f"Image pulls during the install: {_count(len(pulls), 'pull')} of "
                 f"{_count(len({p['image'] for p in pulls}), 'image')}; the busiest node, "
                 f"{busiest}, spent {per_node[busiest]:.1f} s pulling, of the install's "
                 f"{install_seconds:.0f} s."]
    else:
        lines = ["Image pulls during the install: none; every image was already present."]
    images = {}
    for p in pulls:
        images.setdefault(p["image"], []).append(p)
    for image, its in sorted(images.items(), key=lambda kv: -max(p["seconds"] for p in kv[1])):
        slowest = max(its, key=lambda p: p["seconds"])
        line = (f"- {image}: pulled on {_count(len(its), 'node')}, longest "
                f"{slowest['seconds']:.1f} s ({slowest['waiting']:.1f} s with waiting)")
        if slowest["bytes"] is not None:
            line += f", {slowest['bytes'] / 1e6:.1f} MB"
        nodes = sum(1 for i, _ in present if i == image)
        lines.append(line + (f"; already present on {_count(nodes, 'node')}" if nodes else ""))
    for image in sorted({i for i, _ in present} - set(images)):
        lines.append(f"- {image}: already present on "
                     f"{_count(sum(1 for i, _ in present if i == image), 'node')}")
    if per_node:
        lines.append("- per node: " + ", ".join(
            f"{n} {s:.1f} s" for n, s in sorted(per_node.items(), key=lambda kv: -kv[1])))
    if unknown:
        lines.append(f"- {unknown} Pulled events not understood")
    return lines


def measure_pulls(since, install_seconds):
    """pull_lines for the install that started at `since`; never stops the gate."""
    try:
        done = subprocess.run(pull_events_command(), capture_output=True, text=True,
                              check=False)
        if done.returncode != 0:
            raise RuntimeError(done.stderr.strip() or f"kubectl exited {done.returncode}")
        return pull_lines(*image_pulls(json.loads(done.stdout), since), install_seconds)
    except Exception as e:  # noqa: BLE001 - a measurement must never fail the install
        return [f"Image pulls: not measured ({e})"]


def polaris_pg_commands(namespace):
    """The helm release's values, the Cluster and its instance pods (not Job pods), as JSON."""
    kubectl = ["kubectl", "--context", CONTEXT, "-n", namespace, "get"]
    return [
        ["helm", "--kube-context", CONTEXT, "-n", namespace, "get", "values",
         release.POLARIS_PG_RELEASE, "--all", "-o", "json"],
        kubectl + [f"cluster/{release.POLARIS_PG}", "-o", "json"],
        kubectl + ["pods", "-l", f"cnpg.io/cluster={release.POLARIS_PG},cnpg.io/podRole=instance",
                   "-o", "json"],
    ]


def polaris_pg_problems(values, cluster, pods):
    """The running Cluster against the values it was installed with (ADR-016)."""
    spec, problems = cluster["spec"], []
    for field, running, wanted in (
            ("instances", spec["instances"], values["instances"]),
            ("image", spec["imageName"], values["image"]),
            ("podAntiAffinityType", spec["affinity"]["podAntiAffinityType"],
             values["podAntiAffinityType"])):
        if running != wanted:
            problems.append(f"{field}: {running} running, {wanted} in the values")
    ready = cluster.get("status", {}).get("readyInstances", 0)
    if ready != values["instances"]:
        problems.append(f"{ready} instances ready of {values['instances']}")
    nodes = sorted({p["spec"].get("nodeName") for p in pods["items"]})
    if len(nodes) != len(pods["items"]) or len(nodes) != values["instances"]:
        problems.append(f"{len(pods['items'])} instances on {len(nodes)} nodes: "
                        + ", ".join(str(n) for n in nodes))
    anti = spec["affinity"]["podAntiAffinityType"]
    return problems, [f"Cluster {release.POLARIS_PG}: {ready} instances ready on "
                      f"{len(nodes)} nodes ({', '.join(str(n) for n in nodes)}), image "
                      f"{spec['imageName']}, anti-affinity {anti}."]


def polaris_commands(namespace):
    """Polaris's helm values, its Deployment, its pods and its ConfigMap, as JSON."""
    kubectl = ["kubectl", "--context", CONTEXT, "-n", namespace, "get"]
    return [
        ["helm", "--kube-context", CONTEXT, "-n", namespace, "get", "values", release.POLARIS,
         "--all", "-o", "json"],
        kubectl + [f"deployment/{release.POLARIS}", "-o", "json"],
        kubectl + ["pods", "-l", POLARIS_POD_LABELS, "-o", "json"],
        kubectl + [f"configmap/{release.POLARIS}", "-o", "json"],
    ]


def _properties(configmap):
    text = configmap["data"]["application.properties"]
    pairs = (line.split("=", 1) for line in text.splitlines()
             if "=" in line and not line.startswith("#"))
    return {k.strip(): v.strip() for k, v in pairs}


def _running(pods):
    return [p for p in pods["items"] if p.get("status", {}).get("phase") == "Running"]


# Seconds a rebuilt replica has to join and the Cluster to get Ready again, as release.py's
# wait for the Cluster (ADR-048 §7).
REBUILD_TIMEOUT = 900
JOIN_SELECTOR = f"cnpg.io/cluster={release.POLARIS_PG},cnpg.io/jobRole=join"


def rebuild_target(cluster, pods):
    """The replica rebuild-replica deletes: the first instance by name that is not the primary;
    None without one."""
    primary = cluster.get("status", {}).get("currentPrimary")
    names = sorted(p["metadata"]["name"] for p in pods["items"]
                   if p["metadata"]["name"] != primary)
    return names[0] if names else None


def rebuild_commands(namespace, instance):
    """The instance's PVC, without waiting (its pod still holds it), then its pod. CNPG names
    both after the instance."""
    kubectl = ["kubectl", "--context", CONTEXT, "-n", namespace, "delete"]
    return [kubectl + ["pvc", instance, "--wait=false"], kubectl + ["pod", instance]]


def join_jobs_command(namespace):
    return ["kubectl", "--context", CONTEXT, "-n", namespace, "get", "jobs", "-l",
            JOIN_SELECTOR, "-o", "json"]


def rebuilt(cluster, joins):
    """A new join Job ran and the Cluster is Ready with every instance."""
    status = cluster.get("status", {})
    ready = any(c.get("type") == "Ready" and c.get("status") == "True"
                for c in status.get("conditions", []))
    return bool(joins) and ready and status.get("readyInstances") == cluster["spec"]["instances"]


def rebuild_replica(env):
    """ADR-048 §7. Returns (problems, notes)."""
    ns = env["NS_CATALOG_POLARIS"]
    cluster_command, pods_command = polaris_pg_commands(ns)[1:]
    instance = rebuild_target(json.loads(output(cluster_command)),
                              json.loads(output(pods_command)))
    if not instance:
        return [f"Cluster {release.POLARIS_PG} has no replica to rebuild"], []
    jobs_command = join_jobs_command(ns)
    # By uid: a Job made again under an old instance's name is new all the same.
    before = {j["metadata"]["uid"] for j in json.loads(output(jobs_command))["items"]}
    for command in rebuild_commands(ns, instance):
        run(command)
    # A poll sees a join Job while it runs, even if CNPG removes it once done.
    joins, end = set(), time.monotonic() + REBUILD_TIMEOUT
    while True:
        joins |= {j["metadata"]["name"] for j in json.loads(output(jobs_command))["items"]
                  if j["metadata"]["uid"] not in before}
        if rebuilt(json.loads(output(cluster_command)), joins):
            return [], [f"Replica {instance} deleted with its PVC; {', '.join(sorted(joins))} "
                        f"cloned the primary, and Cluster {release.POLARIS_PG} is Ready with "
                        f"every instance."]
        if time.monotonic() > end:
            break
        time.sleep(3)
    subprocess.run(["kubectl", "--context", CONTEXT, "-n", ns, "logs", "-l", JOIN_SELECTOR,
                    "--all-containers", "--prefix", "--tail=200"], check=False)
    joined = ", ".join(sorted(joins)) or "no join Job"
    return [f"Cluster {release.POLARIS_PG} not Ready with every instance {REBUILD_TIMEOUT} s "
            f"after {instance} was deleted with its PVC ({joined})"], []


def polaris_problems(values, deployment, pods, configmap, env):
    """The running Polaris against the values it was installed with (ADR-016), and those
    values against `.env`: the database host and the policy endpoint the gate names."""
    problems, wanted = [], values["replicaCount"]
    replicas = deployment["spec"]["replicas"]
    if replicas != wanted:
        problems.append(f"replicas: {replicas} running, {wanted} in the values")
    ready = deployment.get("status", {}).get("readyReplicas", 0)
    if ready != wanted:
        problems.append(f"{ready} replicas ready of {wanted}")
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    image = container["image"]
    running_env = {e["name"]: e.get("valueFrom", {}).get("secretKeyRef")
                   for e in container.get("env", [])}
    for var, key in POLARIS_S3_ENV.items():
        want = {"name": "polaris-s3", "key": key}
        if running_env.get(var) != want:
            problems.append(f"{var}: {running_env.get(var)} running, {want} wanted")
    want_image = f"{values['image']['repository']}:{values['image']['tag']}"
    if image != want_image:
        problems.append(f"image: {image} running, {want_image} in the values")
    running = _running(pods)
    nodes = sorted({p["spec"].get("nodeName") for p in running})
    if len(nodes) != len(running) or len(nodes) != wanted:
        problems.append(f"{len(running)} replicas on {len(nodes)} nodes: "
                        + ", ".join(str(n) for n in nodes))
    advanced, props = values["advancedConfig"], _properties(configmap)
    for key, want in (
            ("polaris.persistence.type", values["persistence"]["type"]),
            ("polaris.authentication.token-broker.type",
             values["authentication"]["tokenBroker"]["type"]),
            ("polaris.authorization.type", advanced["polaris.authorization.type"]),
            ("polaris.authorization.opa.policy-uri",
             advanced["polaris.authorization.opa.policy-uri"]),
            ("quarkus.datasource.jdbc.url", advanced["quarkus.datasource.jdbc.url"])):
        if props.get(key) != want:
            problems.append(f"{key}: {props.get(key)} running, {want} in the values")
    for key, want in (("quarkus.datasource.jdbc.url", release.jdbc_url(env)),
                      ("polaris.authorization.opa.policy-uri", env["POLARIS_OPA_URL"])):
        if advanced[key] != want:
            problems.append(f"{key}: {advanced[key]} in the values, {want} from .env")
    return problems, [
        f"Deployment {release.POLARIS}: {ready} replicas ready on {len(nodes)} nodes "
        f"({', '.join(str(n) for n in nodes)}), image {image}.",
        f"Polaris settings: S3 key from Secret polaris-s3, persistence "
        f"{props.get('polaris.persistence.type')}, database "
        f"{props.get('quarkus.datasource.jdbc.url')}, tokens "
        f"{props.get('polaris.authentication.token-broker.type')}, authorization "
        f"{props.get('polaris.authorization.type')} at "
        f"{props.get('polaris.authorization.opa.policy-uri')}."]


def polaris_pod_urls(pods):
    """Each running Polaris pod's API, by its own address: one check per replica."""
    return [f"http://{p['status']['podIP']}:{POLARIS_API_PORT}" for p in _running(pods)]


def check_polaris_command(env, image, name, pod_urls):
    """The pod that runs check_polaris.sh (on stdin) with root's credentials."""
    creds = [{"name": var, "valueFrom": {"secretKeyRef": {"name": release.POLARIS_ROOT_SECRET,
                                                         "key": key}}}
             for var, key in (("ROOT_CLIENT_ID", "clientId"),
                              ("ROOT_CLIENT_SECRET", "clientSecret"))]
    overrides = {"spec": {"containers": [{"name": name, "image": image, "env": creds,
                                          "stdin": True, "stdinOnce": True}]}}
    return ["kubectl", "--context", CONTEXT, "-n", env["NS_CATALOG_POLARIS"], "run", name,
            CHECK_POD_LABEL, f"--image={image}", "--restart=Never", "--rm", "-i", "--quiet",
            "--pod-running-timeout=3m", "--override-type=strategic",
            "--overrides", json.dumps(overrides),
            "--command", "--", "sh", "-s", "--",
            f"http://{release.POLARIS}:{POLARIS_API_PORT}",
            f"http://{release.POLARIS}-mgmt:{POLARIS_MGMT_PORT}", release.POLARIS_REALM,
            *pod_urls]


def smoke_python_image(pins):
    """The smoke tests' Python: the pinned image the recorder already runs on."""
    return pins["standin_images"]["recorder"]["image"]


def iceberg_configmap(env, script, requirements):
    """gates/iceberg/smoke.py and its hashed requirements, for the smoke pod to mount."""
    return {"apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": "iceberg-smoke", "namespace": env["NS_CATALOG_POLARIS"]},
            "data": {"smoke.py": script, "requirements.txt": requirements}}


def check_iceberg_commands(env, image, name):
    """Apply the ConfigMap (on stdin), then the pod that installs PyIceberg by hash and runs
    smoke.py as the smoke tests' principal, with root's credentials and the smoke S3 key."""
    ns = env["NS_CATALOG_POLARIS"]
    creds = [{"name": var, "valueFrom": {"secretKeyRef": {"name": secret, "key": key}}}
             for var, secret, key in (
                 ("ROOT_CLIENT_ID", release.POLARIS_ROOT_SECRET, "clientId"),
                 ("ROOT_CLIENT_SECRET", release.POLARIS_ROOT_SECRET, "clientSecret"),
                 ("S3_ACCESS_KEY_ID", "smoke-s3", "accessKeyId"),
                 ("S3_SECRET_ACCESS_KEY", "smoke-s3", "secretAccessKey"))]
    settings = [{"name": var, "value": value} for var, value in (
        ("PYTHONUNBUFFERED", "1"), ("PIP_DISABLE_PIP_VERSION_CHECK", "1"),
        ("PIP_NO_CACHE_DIR", "1"), ("PIP_ROOT_USER_ACTION", "ignore"))]
    overrides = {"spec": {
        "containers": [{"name": name, "image": image, "env": creds + settings,
                        "volumeMounts": [{"name": "code", "mountPath": "/smoke",
                                          "readOnly": True}]}],
        "volumes": [{"name": "code", "configMap": {"name": "iceberg-smoke"}}]}}
    install = ("pip install --quiet --require-hashes --only-binary :all: "
               "-r /smoke/requirements.txt && exec python3 /smoke/smoke.py \"$@\"")
    return [
        ["kubectl", "--context", CONTEXT, "-n", ns, "apply", "--server-side",
         "--field-manager=gate", "-f", "-"],
        ["kubectl", "--context", CONTEXT, "-n", ns, "run", name, CHECK_POD_LABEL,
         f"--image={image}",
         "--restart=Never", "--rm", "--attach", "--quiet", "--pod-running-timeout=3m",
         "--override-type=strategic", "--overrides", json.dumps(overrides),
         "--command", "--", "sh", "-c", install, "smoke",
         f"http://{release.POLARIS}:{POLARIS_API_PORT}", release.POLARIS_REALM,
         ICEBERG_CATALOG, env["S3_ENDPOINT"], env["S3_REGION"],
         env["POLARIS_WAREHOUSE_BUCKET"]],
    ]


# The restore the smoke gate proves (ADR-043 §4): its Cluster, sized for the gate only.
RESTORE = "polaris-pg-restore"
RESTORE_SIZE = "1Gi"
BACKUP_TIMEOUT = 600
RESTORE_TIMEOUT = 900
# How often the wait for the restore reads it and its full-recovery pods (#86).
RESTORE_POLL = 3


def backup_manifest(env, name):
    """An on-demand Backup of polaris-pg through the Barman Cloud Plugin."""
    return {"apiVersion": "postgresql.cnpg.io/v1", "kind": "Backup",
            "metadata": {"name": name, "namespace": env["NS_CATALOG_POLARIS"]},
            "spec": {"cluster": {"name": release.POLARIS_PG}, "method": "plugin",
                     "pluginConfiguration": {"name": release.PLUGIN_NAME}}}


def backup_outcome(backup):
    """None while the Backup runs; (phase, error) once it completed or failed."""
    status = backup.get("status", {})
    if status.get("phase") in ("completed", "failed"):
        return status["phase"], status.get("error", "")
    return None


def restore_cluster(env, image):
    """polaris-pg-restore: one instance bootstrapped from polaris-pg's ObjectStore, with no
    recovery target, so every archived WAL is replayed. It names no plugin of its own, so it
    never archives into polaris-pg's backups. The Polaris user keeps its password from
    polaris-db."""
    source = release.POLARIS_PG
    return {"apiVersion": "postgresql.cnpg.io/v1", "kind": "Cluster",
            "metadata": {"name": RESTORE, "namespace": env["NS_CATALOG_POLARIS"]},
            "spec": {"instances": 1, "imageName": image, "storage": {"size": RESTORE_SIZE},
                     "bootstrap": {"recovery": {
                         "source": source, "database": release.POLARIS_DATABASE,
                         "owner": "polaris", "secret": {"name": release.POLARIS_DB_SECRET}}},
                     "externalClusters": [{
                         "name": source,
                         "plugin": {"name": release.PLUGIN_NAME, "parameters": {
                             "barmanObjectName": release.POLARIS_BACKUP_STORE,
                             "serverName": source}}}]}}


def check_restore_command(env, image, database, name):
    """The pod that runs check_restore.sh (on stdin) as Polaris against the restore."""
    creds = [{"name": var, "valueFrom": {"secretKeyRef": {"name": release.POLARIS_DB_SECRET,
                                                          "key": key}}}
             for var, key in (("PGUSER", "username"), ("PGPASSWORD", "password"))]
    overrides = {"spec": {"containers": [{"name": name, "image": image, "env": creds,
                                          "stdin": True, "stdinOnce": True}]}}
    return ["kubectl", "--context", CONTEXT, "-n", env["NS_CATALOG_POLARIS"], "run", name,
            CHECK_POD_LABEL, f"--image={image}", "--restart=Never", "--rm", "-i", "--quiet",
            "--pod-running-timeout=3m", "--override-type=strategic",
            "--overrides", json.dumps(overrides),
            "--command", "--", "sh", "-s", "--", f"{RESTORE}-rw", database,
            release.POLARIS_REALM, ICEBERG_CATALOG]


def check_network(env, pins):
    """ADR-048 §6: the source namespaces, then from a pod in each a connection to each target
    of network.CASES. Returns (problems, lines)."""
    objects = network.namespaces_manifest(env)
    objects["items"].append(network.metrics_service(env))
    manifest = json.dumps(objects)
    apply = ["kubectl", "--context", CONTEXT, "apply", "--server-side", "--field-manager=gate",
             "-f", "-"]
    subprocess.run(apply, input=manifest, text=True, check=True, capture_output=True)
    addresses, results = network.targets(env), {}
    for source, (namespace, _) in network.source_namespaces(env).items():
        wanted = sorted({c.target for c in network.CASES if c.source == source})
        command = network.probe_command(namespace, smoke_python_image(pins),
                                        f"check-network-{source}-{os.getpid()}",
                                        [addresses[t] for t in wanted],
                                        labels=network.POD_LABELS.get(source))
        out = subprocess.run(command, input=network.PROBE, capture_output=True, text=True,
                             check=False)
        by_address = network.parse_probe(out.stdout)
        results[source] = {t: by_address.get(addresses[t]) for t in wanted}
    return network.report(network.CASES, results)


def check_metrics(env, pins):
    """ADR-046 §5: every running pod of each job of the contract, then the stand-in asked from
    a check pod until they are all up. Returns (problems, notes)."""
    kubectl = ["kubectl", "--context", CONTEXT, "-n", env["NS_CATALOG_POLARIS"]]
    pods = {job: [p["metadata"]["name"] for p in
                  _running(json.loads(output(kubectl + ["get", "pods", "-l", selector,
                                                        "-o", "json"])))]
            for job, selector in metrics.JOBS.items()}
    command = metrics.query_command(env, smoke_python_image(pins),
                                    f"check-metrics-{os.getpid()}",
                                    sum(len(names) for names in pods.values()))
    out = subprocess.run(command, input=metrics.QUERY, capture_output=True, text=True,
                         check=False)
    print(out.stderr, end="", flush=True)
    return metrics.problems(metrics.parse_answer(out.stdout), pods)


def recovery_pods_command(namespace):
    """The restore's full-recovery Job pods, failed ones included, as JSON (#86)."""
    return ["kubectl", "--context", CONTEXT, "-n", namespace, "get", "pods", "-l",
            f"cnpg.io/cluster={RESTORE},cnpg.io/jobRole=full-recovery", "-o", "json"]


def _container_end(status):
    state = status.get("state", {})
    if "terminated" in state:
        t = state["terminated"]
        text = f"terminated {t.get('reason', '?')} (exit {t.get('exitCode', '?')})"
    else:
        text = next(iter(state), "unknown")
    restarts = status.get("restartCount", 0)
    return f"{status['name']}: {text}" + (f", {restarts} restarts" if restarts else "")


def _clock(timestamp):
    return timestamp[11:19] if timestamp else "?"


def recovery_report(pods):
    """(lines, failed pod names): how many pods the full-recovery Job ran, when each ran and
    how it ended, oldest first. #86: it ran six on the gate before one completed."""
    items = sorted(pods.get("items", []), key=lambda p: p["metadata"]["creationTimestamp"])
    failed = [p["metadata"]["name"] for p in items if p["status"].get("phase") == "Failed"]
    lines = [f"Full recovery of {RESTORE}: {len(items)} pods, {len(failed)} failed."]
    for pod in items:
        statuses = (pod["status"].get("initContainerStatuses", [])
                    + pod["status"].get("containerStatuses", []))
        ends = [c["state"]["terminated"].get("finishedAt") for c in statuses
                if "terminated" in c.get("state", {})]
        when = f"created {_clock(pod['metadata']['creationTimestamp'])}"
        if any(ends):
            when += f", ended {_clock(max(e for e in ends if e))}"
        lines.append(f"  {pod['metadata']['name']}: {pod['status'].get('phase')}, {when}; "
                     + "; ".join(_container_end(c) for c in statuses))
    return lines, failed


def cluster_ready(cluster):
    return any(c.get("type") == "Ready" and c.get("status") == "True"
               for c in cluster.get("status", {}).get("conditions", []))


def remember_pods(seen, pods):
    """Keeps each pod's latest state in `seen`, by name, after it is gone too; returns the
    pods that failed since the last read, oldest first, each named once."""
    newly = []
    for pod in sorted(pods.get("items", []), key=lambda p: p["metadata"]["creationTimestamp"]):
        name = pod["metadata"]["name"]
        before = seen.get(name, {}).get("status", {}).get("phase")
        seen[name] = pod
        if pod["status"].get("phase") == "Failed" and before != "Failed":
            newly.append(name)
    return newly


def _read_json(argv):
    """(the command's JSON, None) or (None, why it could not be read)."""
    done = subprocess.run(argv, capture_output=True, text=True, check=False)
    try:
        if done.returncode != 0:
            raise ValueError(done.stderr.strip() or f"exit {done.returncode}")
        return json.loads(done.stdout), None
    except ValueError as e:
        return None, str(e)


def watch_restore(kubectl, namespace):
    """Waits up to RESTORE_TIMEOUT s for the restore to get Ready, reading it and its
    full-recovery pods every RESTORE_POLL s. Each pod's logs are printed as soon as it is seen
    failed, before CNPG deletes it (#86). A read that fails is counted and the wait goes on.
    Returns (ready, recovery_report's lines for every pod seen)."""
    seen, unread, end = {}, [], time.monotonic() + RESTORE_TIMEOUT
    while True:
        cluster, _ = _read_json(kubectl + ["get", f"cluster/{RESTORE}", "-o", "json"])
        pods, why = _read_json(recovery_pods_command(namespace))
        if pods is None:
            unread.append(why)
        else:
            try:
                failed = remember_pods(seen, pods)
            except (KeyError, TypeError) as e:
                unread.append(f"unexpected pod list: {e}")
                failed = []
            for name in failed:
                print(f"--- logs of {name}, failed", flush=True)
                subprocess.run(kubectl + ["logs", name, "--all-containers", "--prefix",
                                          "--tail=100"], check=False)
        ready = cluster is not None and cluster_ready(cluster)
        if ready or time.monotonic() > end:
            break
        time.sleep(RESTORE_POLL)
    lines, _ = recovery_report({"items": list(seen.values())})
    if unread:
        lines.append(f"  {len(unread)} read{'' if len(unread) == 1 else 's'} of the pods "
                     f"failed: {unread[-1]}")
    return ready, lines


def check_restore(env):
    """Backs up polaris-pg, restores it into polaris-pg-restore, reads it, deletes it.
    Returns (problems, notes)."""
    ns = env["NS_CATALOG_POLARIS"]
    kubectl = ["kubectl", "--context", CONTEXT, "-n", ns]
    apply = kubectl + ["apply", "--server-side", "--field-manager=gate", "-f", "-"]
    pg = polaris_pg_values()
    name = f"{release.POLARIS_PG}-gate-{os.getpid()}"
    run(apply, json.dumps(backup_manifest(env, name)))
    phase, error = wait_for(kubectl + ["get", f"backup/{name}", "-o", "json"], backup_outcome,
                            f"Backup {name}", timeout=BACKUP_TIMEOUT)
    if phase != "completed":
        return [f"Backup {name} of {release.POLARIS_PG} failed: {error}"], []
    notes = [f"Backup {name} of {release.POLARIS_PG} completed through the plugin."]
    try:
        run(apply, json.dumps(restore_cluster(env, pg["image"])))
        ready, lines = watch_restore(kubectl, ns)
        notes += lines
        if not ready:
            return [f"Cluster {RESTORE} did not get Ready from "
                    f"{release.POLARIS_BACKUP_STORE}"], notes
        # CNPG restarts the restored instance once after the recovery: Ready, then refusing
        # connections (smoke 38245393066), as polaris-pg after an upgrade.
        problem = release.settle(release.call, CONTEXT, ns, cluster=RESTORE)
        if problem:
            return [problem], notes
        notes.append(f"Cluster {RESTORE} restored from {release.POLARIS_BACKUP_STORE} is Ready, "
                     f"healthy on {release.SETTLE_READS} reads {release.SETTLE_INTERVAL} s apart.")
        with open(os.path.join(HERE, "check_restore.sh"), encoding="utf-8") as f:
            script = f.read()
        command = check_restore_command(env, pg["image"], pg["database"],
                                        f"check-restore-{os.getpid()}")
        if subprocess.run(command, input=script, text=True, check=False).returncode != 0:
            return [f"Cluster {RESTORE} lacks what polaris-pg held"], notes
        return [], notes
    finally:
        subprocess.run(kubectl + ["delete", "cluster", RESTORE, "--ignore-not-found",
                                  "--wait", "--timeout=5m"], check=False)


def console_source(path=os.path.join(CONSOLE_IMAGE_DIR, "source.json")):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def console_image(source):
    """The console image's name: built here, never pulled, tagged with its commit."""
    return f"{CONSOLE_IMAGE_REPOSITORY}:{source['commit'][:12]}"


def console_image_commands(source, context_dir):
    """Fetch the pinned commit into `context_dir`, build its console/ with our Dockerfile, load
    the image into kind. Our lockfile is copied in, and the commit checked, between the third
    and the fourth command."""
    return [
        ["git", "init", "--quiet", context_dir],
        ["git", "-C", context_dir, "fetch", "--quiet", "--depth", "1", source["repository"],
         source["commit"]],
        ["git", "-C", context_dir, "-c", "advice.detachedHead=false", "checkout", "--quiet",
         "FETCH_HEAD"],
        ["docker", "build", "--file", os.path.join(CONSOLE_IMAGE_DIR, "Dockerfile"),
         "--tag", console_image(source), os.path.join(context_dir, source["path"])],
        ["kind", "load", "docker-image", console_image(source), "--name", CLUSTER],
    ]


def check_commit(repo, commit):
    """Raises unless `repo` has exactly `commit` checked out."""
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    if head != commit:
        raise ValueError(f"{repo}: {head or 'nothing'} checked out, pinned {commit}")


def check_console_command(env, image, name):
    """The pod that runs console/check.py (on stdin) with root's credentials, against the
    console's origin and the URL its browser reaches Polaris at."""
    if not release.console_enabled(env):
        sys.exit("the console is not enabled in .env (POLARIS_CONSOLE_ENABLED): nothing to check")
    creds = [{"name": var, "valueFrom": {"secretKeyRef": {"name": release.POLARIS_ROOT_SECRET,
                                                         "key": key}}}
             for var, key in (("ROOT_CLIENT_ID", "clientId"),
                              ("ROOT_CLIENT_SECRET", "clientSecret"))]
    overrides = {"spec": {"containers": [{"name": name, "image": image, "env": creds,
                                          "stdin": True, "stdinOnce": True}]}}
    return ["kubectl", "--context", CONTEXT, "-n", env["NS_CATALOG_POLARIS"], "run", name,
            CHECK_POD_LABEL, f"--image={image}", "--restart=Never", "--rm", "-i", "--quiet",
            "--pod-running-timeout=3m", "--override-type=strategic",
            "--overrides", json.dumps(overrides),
            "--command", "--", "python3", "-", env["POLARIS_CONSOLE_ORIGIN"],
            env["POLARIS_CONSOLE_API_URL"], release.POLARIS_REALM]


def batch_image_commands(directory=release.BATCH_IMAGE_DIR,
                         registry=release.DEFAULT_IMAGE_REGISTRY):
    """Build the log batch's image, tagged as release.py names it in that registry (ADR-042),
    and load it into kind, which runs it without pulling."""
    image = release.batch_image(directory, registry)
    return [["docker", "build", "--file", os.path.join(directory, "Dockerfile"),
             "--tag", image, directory],
            ["kind", "load", "docker-image", image, "--name", CLUSTER]]


def logs_request_ids(prefix, count, pod_names, failing=False):
    """The X-Request-ID each request carries, as logs_traffic.sh makes it -> (its pod, its kind),
    each kind for a rule of the audit policy (contract v2): `read`, root listing the catalogs,
    which it counts (6); `denied`, the same without a token, which it keeps (3); once per pod,
    `invalid`, a table made without a schema, 400, refused before anything is written, kept
    with its application line (2b); `missing`, a table that does not exist, 404, dropped with
    its application line (3'); `commit`, a table made, whose commit counts on its row (2a).
    `catalog`, through the Service, so on no pod known in advance: LOGS_CATALOG made, its
    application line kept (2b). With `failing` (Create Fixtures, ADR-040, logs_traffic.sh's
    LOGS_DOWN_ENDPOINT), also LOGS_CATALOG_down made (`catalog`) and once per pod `failed`, a
    table made there, 500, kept with its ERROR line and the line's `exception` (1)."""
    sent = {f"{prefix}-{pod}-{kind}-{n}": (pod, kind) for pod in pod_names
            for n in range(1, count + 1) for kind in ("read", "denied")}
    sent.update({f"{prefix}-{pod}-{kind}-1": (pod, kind) for pod in pod_names
                 for kind in LOGS_POLICY_KINDS})
    sent[f"{prefix}-setup-catalog"] = (None, "catalog")
    if failing:
        sent.update({f"{prefix}-{pod}-failed-1": (pod, "failed") for pod in pod_names})
        sent[f"{prefix}-setup-down-catalog"] = (None, "catalog")
    return sent


def logs_table(request_id):
    """The table a `commit` request makes, named for it as logs_traffic.sh names it."""
    return request_id.replace("-", "_")


def logs_traffic_command(env, image, name, prefix, count, pods, down=None):
    """The pod that runs logs_traffic.sh (on stdin) with root's credentials against each of
    `pods`, (name, URL) pairs, LOGS_CATALOG on the S3 endpoint in `env`; with `down`, its
    LOGS_DOWN_ENDPOINT (Create Fixtures, ADR-040)."""
    creds = [{"name": var, "valueFrom": {"secretKeyRef": {"name": release.POLARIS_ROOT_SECRET,
                                                         "key": key}}}
             for var, key in (("ROOT_CLIENT_ID", "clientId"),
                              ("ROOT_CLIENT_SECRET", "clientSecret"))]
    if down:
        creds.append({"name": "LOGS_DOWN_ENDPOINT", "value": down})
    overrides = {"spec": {"containers": [{"name": name, "image": image, "env": creds,
                                          "stdin": True, "stdinOnce": True}]}}
    return ["kubectl", "--context", CONTEXT, "-n", env["NS_CATALOG_POLARIS"], "run", name,
            CHECK_POD_LABEL, f"--image={image}", "--restart=Never", "--rm", "-i", "--quiet",
            "--pod-running-timeout=3m", "--override-type=strategic",
            "--overrides", json.dumps(overrides),
            "--command", "--", "sh", "-s", "--",
            f"http://{release.POLARIS}:{POLARIS_API_PORT}", release.POLARIS_REALM, str(count),
            prefix, LOGS_CATALOG, env["S3_ENDPOINT"], env["S3_REGION"],
            env["POLARIS_WAREHOUSE_BUCKET"], *[part for pod in pods for part in pod]]


def batch_now(t):
    """The clock the gate gives the batch: the hour after `t` began, plus the batch's 60 s of
    grace and one more, so the hour `t` is in is publishable (ADR-036)."""
    hour = t.astimezone(dt.timezone.utc).replace(minute=0, second=0, microsecond=0)
    return (hour + dt.timedelta(hours=1, seconds=61)).strftime("%Y-%m-%dT%H:%M:%SZ")


def batch_job(cronjob, name, now):
    """A Job from the CronJob's own template, the batch told `--now`."""
    template = copy.deepcopy(cronjob["spec"]["jobTemplate"])
    container = template["spec"]["template"]["spec"]["containers"][0]
    container["args"] = container.get("args", []) + ["--now", now]
    metadata = {"name": name, "namespace": cronjob["metadata"]["namespace"]}
    if template.get("metadata", {}).get("labels"):
        metadata["labels"] = template["metadata"]["labels"]
    return {"apiVersion": "batch/v1", "kind": "Job", "metadata": metadata,
            "spec": template["spec"]}


def job_outcome(job):
    """`succeeded`, `failed`, or None while the Job runs."""
    status = job.get("status", {})
    if status.get("succeeded"):
        return "succeeded"
    if status.get("failed"):
        return "failed"
    return None


def json_lines(text):
    """The lines of `text` that are JSON objects; anything else (a log line, a traceback) is
    skipped."""
    out = []
    for line in text.splitlines():
        if line.startswith("{"):
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


batch_events = fluent_bit_records = json_lines


def _logs_volume(read_only):
    return ({"name": release.POLARIS_LOGS,
             "persistentVolumeClaim": {"claimName": release.POLARIS_LOGS,
                                       "readOnly": read_only}},
            {"name": release.POLARIS_LOGS, "mountPath": LOGS_MOUNT, "readOnly": read_only})


def logs_listing_command(env, image, name):
    """The pod that runs LOGS_LISTING (on stdin) with the claim mounted read-only."""
    volume, mount = _logs_volume(True)
    overrides = {"spec": {"containers": [{"name": name, "image": image, "stdin": True,
                                          "stdinOnce": True, "volumeMounts": [mount]}],
                          "volumes": [volume]}}
    return ["kubectl", "--context", CONTEXT, "-n", env["NS_CATALOG_POLARIS"], "run", name,
            CHECK_POD_LABEL, f"--image={image}", "--restart=Never", "--rm", "-i", "--quiet",
            "--pod-running-timeout=3m", "--override-type=strategic",
            "--overrides", json.dumps(overrides), "--command", "--", "sh", "-s", "--",
            LOGS_MOUNT]


def parse_listing(text):
    """LOGS_LISTING's lines -> {path: {"sha256": ..., "lines": ...}}."""
    out = {}
    for line in text.splitlines():
        parts = line.split(" ", 2)
        if len(parts) == 3:
            out[parts[2]] = {"sha256": parts[0], "lines": int(parts[1])}
    return out


def fluent_bit_pod(env, image, name):
    """The stand-in for the observability team's Fluent Bit (ADR-024, ADR-036): in Polaris's
    namespace, the claim mounted read-only, it tails both output directories from their start
    and prints every line as JSON with its path, then exits once it has read them all."""
    volume, mount = _logs_volume(True)
    paths = ",".join(f"{LOGS_MOUNT}/{d}/*.jsonl" for d in OUTPUT_DIRS)
    return {"apiVersion": "v1", "kind": "Pod",
            "metadata": {"name": name, "namespace": env["NS_CATALOG_POLARIS"],
                         "labels": {"app.kubernetes.io/name": "fluent-bit-standin"}},
            "spec": {
                "restartPolicy": "Never",
                "securityContext": {"runAsNonRoot": True, "runAsUser": READER_UID,
                                    "runAsGroup": READER_UID,
                                    "seccompProfile": {"type": "RuntimeDefault"}},
                "containers": [{
                    "name": "fluent-bit", "image": image,
                    "command": ["/fluent-bit/bin/fluent-bit"],
                    "args": ["-R", "/fluent-bit/etc/parsers.conf",
                             "-i", "tail", "-p", f"path={paths}", "-p", "read_from_head=true",
                             "-p", "exit_on_eof=true", "-p", "parser=json",
                             "-p", "path_key=source_file", "-p", "buffer_max_size=1M",
                             "-o", "stdout", "-p", "format=json_lines",
                             "-p", "json_date_key=false", "-f", "1"],
                    "securityContext": {"allowPrivilegeEscalation": False,
                                        "readOnlyRootFilesystem": True,
                                        "capabilities": {"drop": ["ALL"]}},
                    "volumeMounts": [mount]}],
                "volumes": [volume]}}


def logs_problems(sent, pods, events, before, after, records):
    """What the reader saw against what was sent (ADR-036, contract v2): every line and row
    holds the contract, every file was read whole, every summary's books balance, each kept
    request was read once from its pod, no counted one was kept but the rows count them all,
    the policy's rules 2a, 2b and 3' held on Polaris's own lines (logs_request_ids), every
    pod's file was read, and a second run changed nothing. Returns (problems, notes)."""
    problems = []
    first, second = events
    published = [e["hour"] for e in first if e.get("event") == "published"]
    if not published:
        why = "; ".join(e.get("reason", e.get("event", "")) for e in first) or "no output"
        problems.append(f"the first run published no hour: {why}")
    if any(e.get("event") == "published" for e in second):
        problems.append("the second run published again: "
                        + ", ".join(e["hour"] for e in second if e.get("event") == "published"))
    for path in sorted(set(before) | set(after)):
        if before.get(path) != after.get(path):
            problems.append(f"the second run changed {path}: {before.get(path)} -> "
                            f"{after.get(path)}")
    hours = {d: {p.split("/", 1)[1] for p in after if p.startswith(d + "/")}
             for d in OUTPUT_DIRS}
    for missing in sorted(hours["processed-logs"] ^ hours["aggregated-logs"]):
        where = "aggregated-logs" if missing in hours["processed-logs"] else "processed-logs"
        problems.append(f"{where}/{missing} is missing: an hour has both files")

    processed_schema = contract.load("processed")
    read, rows, seen, by_rid = {}, [], {}, {}
    for record in records:
        record = dict(record)
        path = record.pop("source_file", "")
        rel = path[len(LOGS_MOUNT) + 1:] if path.startswith(LOGS_MOUNT + "/") else path
        read[rel] = read.get(rel, 0) + 1
        kind = rel.split("/", 1)[0]
        if kind == "aggregated-logs":
            problems += [f"{rel}: {p}" for p in contract.row_problems(record)]
            rows.append((rel, record))
        elif kind == "processed-logs":
            problems += [f"{rel}: {p}" for p in contract.problems(record, processed_schema)]
            rid = record.get("mdc", {}).get("requestId")
            if rid in sent:
                seen.setdefault(rid, []).append(record.get("hostName"))
                by_rid.setdefault(rid, []).append(record)
        else:
            problems.append(f"a record from {path or 'no file'}, outside the hand-off")
    for path in sorted(set(after) | set(read)):
        want = after.get(path, {}).get("lines", 0)
        if read.get(path, 0) != want:
            problems.append(f"{path}: the reader read {read.get(path, 0)} lines of {want}")

    summaries = [(rel, r) for rel, r in rows if r.get("report_type") == "summary"]
    for rel, s in summaries:
        books = (("lines_in", s.get("processed", 0) + s.get("dropped", 0) + s.get("malformed", 0)),
                 ("dropped", s.get("access_counted", 0) + s.get("app_dropped_total", 0)
                  + s.get("app_dropped_404", 0)),
                 ("access_kept", s.get("access_seen", 0) - s.get("access_counted", 0)),
                 ("processed", after.get(f"processed-logs/{rel.split('/', 1)[1]}",
                                         {}).get("lines")))
        for field, want in books:
            if s.get(field) != want:
                problems.append(f"{rel}: {field} is {s.get(field)}, its books say {want}")

    kept = {rid: pod for rid, (pod, kind) in sent.items() if kind == "denied"}
    counted = {rid: pod for rid, (pod, kind) in sent.items() if kind == "read"}
    for rid, pod in sorted(kept.items()):
        hosts = seen.get(rid, [])
        if hosts != [pod]:
            problems.append(f"request {rid}, sent to {pod} without a token: read {len(hosts)} "
                            f"times" + (f", from {', '.join(map(str, hosts))}" if hosts else ""))
    for rid in sorted(set(counted) & set(seen)):
        problems.append(f"request {rid}, a read the policy counts, was kept {len(seen[rid])} "
                        f"times")

    def total(kind, key, value, field):
        return sum(r.get(field, 0) for _, r in rows
                   if r.get("report_type") == kind and r.get(key) == value)

    catalogs = "/api/management/v1/catalogs"
    for what, have, want in (
            (f"resource {catalogs} reads", total("resource", "resource", catalogs, "reads"),
             len(kept) + len(counted)),
            (f"resource {catalogs} auth_denied",
             total("resource", "resource", catalogs, "auth_denied"), len(kept)),
            ("principal root reads", total("principal", "user_principal_name", "root", "reads"),
             len(counted)),
            ("principal - auth_denied",
             total("principal", "user_principal_name", "-", "auth_denied"), len(kept))):
        if have < want:
            problems.append(f"{what}: {have}, fewer than the {want} sent")

    def of_kind(kind):
        return sorted(rid for rid, (_, k) in sent.items() if k == kind)

    missing, invalid, commits = of_kind("missing"), of_kind("invalid"), of_kind("commit")
    for rid in missing:
        if by_rid.get(rid):
            problems.append(f"request {rid}, a 404: {len(by_rid[rid])} of its lines kept "
                            f"({', '.join(r.get('loggerName', '') for r in by_rid[rid])})")
    for field in ("counted_404", "app_dropped_404"):
        have = sum(s.get(field, 0) for _, s in summaries)
        if have < len(missing):
            problems.append(f"the summaries' {field}: {have}, fewer than the {len(missing)} "
                            f"404s sent")
    mapper = APP_LINE_LOGGERS[1]
    for rid in invalid + of_kind("catalog"):
        pod, lines = sent[rid][0], by_rid.get(rid, [])
        access = [r for r in lines if "http_status" in r]
        app = [r for r in lines if r.get("loggerName") in
               ((mapper,) if pod else APP_LINE_LOGGERS)]
        hosts = sorted({r.get("hostName") for r in lines})
        if len(access) != 1 or not app or (pod and hosts != [pod]):
            problems.append(f"request {rid}{f', sent to {pod}' if pod else ''}: "
                            f"{len(access)} access line(s) and {len(app)} application line(s) "
                            f"kept, from {', '.join(map(str, hosts)) or 'no pod'}; want one "
                            f"access line and its application line, kept from its pod")
    for rid in commits:
        table = (f"/api/catalog/v1/{LOGS_CATALOG}/namespaces/{LOGS_NAMESPACE}/tables/"
                 f"{logs_table(rid)}")
        if total("resource", "resource", table, "commit_count") < 1:
            problems.append(f"resource {table}: no commit counted for request {rid}")
    failed = of_kind("failed")
    for rid in failed:
        pod, lines = sent[rid][0], by_rid.get(rid, [])
        access = [r for r in lines if r.get("http_status") == 500]
        errors = [r for r in lines if r.get("level") == "ERROR"
                  and isinstance(r.get("exception"), dict)]
        hosts = sorted({r.get("hostName") for r in lines})
        if len(access) != 1 or not errors or hosts != [pod]:
            problems.append(f"request {rid}, sent to {pod}: {len(access)} access line(s) of a "
                            f"500 and {len(errors)} ERROR line(s) with `exception` kept, from "
                            f"{', '.join(map(str, hosts)) or 'no pod'}; want one access line "
                            f"and its ERROR line, kept from its pod")

    logged = {p for _, s in summaries for p in s.get("pods", [])}
    files = {f["file"] for _, s in summaries for f in s.get("sources", [])}
    for pod in pods:
        if pod not in logged or f"polaris-{pod}.log" not in files:
            problems.append(f"pod {pod}: its file polaris-{pod}.log was not read")

    n_files = len(after)
    notes = [
        f"The batch published {len(published)} hour(s) ({', '.join(published)}); the second "
        f"run published nothing and left all {n_files} files as they were.",
        f"The stand-in Fluent Bit read {len(records)} lines from {len(read)} files, each "
        f"checked against contract v2, every summary's books balanced.",
        f"All {len(kept)} kept request IDs (no token, 401) read exactly once, each from the pod "
        f"it was sent to; none of the {len(counted)} counted (root's reads) kept, all counted "
        f"on the rows; pods {', '.join(pods)}, both files read.",
        f"Rules 2a, 2b and 3' on Polaris's own lines: {len(missing)} 404s dropped with their "
        f"application lines, {len(invalid)} 400s kept with theirs, {len(commits)} commits on "
        f"their tables' rows; {LOGS_CATALOG} made, its application line kept."
        + (f" Rule 1: {len(failed)} 500s kept with their ERROR line and its exception."
           if failed else "")]
    return problems, ([] if problems else notes)


def _git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, check=False)


def released_tags(repo=ROOT):
    """The release tags whose content HEAD contains.

    A tag sits on an `rc` merge commit that `main` never contains (ADR-033), so a tag counts
    when it is an ancestor of HEAD (a commit on `rc`) or when its second parent, the `main`
    commit the release gate tested, is (a commit on `main` or a gate branch).
    """
    tags = _git(repo, "tag", "--list", "platform-*").stdout.split()
    return [t for t in tags
            if _git(repo, "merge-base", "--is-ancestor", t, "HEAD").returncode == 0
            or _git(repo, "merge-base", "--is-ancestor", f"{t}^2", "HEAD").returncode == 0]


def run(argv, stdin=None):
    print("+ " + " ".join(argv), flush=True)
    subprocess.run(argv, input=stdin, text=True, check=True)


def output(argv, stdin=None):
    """A command's stdout; its failure stops the gate with what it said."""
    done = subprocess.run(argv, input=stdin, capture_output=True, text=True, check=False)
    if done.returncode != 0:
        sys.exit(f"{' '.join(argv[:8])}... failed: {done.stderr.strip() or done.stdout.strip()}")
    return done.stdout


def wait_for(argv, done, what, timeout=LOGS_TIMEOUT):
    """Reads `argv` as JSON every 3 s until done(answer) gives a value; stops the gate after
    `timeout` seconds."""
    end = time.monotonic() + timeout
    while True:
        result = done(json.loads(output(argv)))
        if result:
            return result
        if time.monotonic() > end:
            sys.exit(f"{what}: not done after {timeout} s")
        time.sleep(3)


def check_logs(env, pins):
    """`check-logs`: see the module's docstring. Returns (problems, notes)."""
    ns, pid = env["NS_CATALOG_POLARIS"], os.getpid()
    kubectl = ["kubectl", "--context", CONTEXT, "-n", ns]
    pods = _running(json.loads(output(kubectl + ["get", "pods", "-l", POLARIS_POD_LABELS,
                                                 "-o", "json"])))
    if len(pods) < 2:
        return [f"{len(pods)} Polaris pod(s) running: the check needs both replicas"], []
    targets = [(p["metadata"]["name"], f"http://{p['status']['podIP']}:{POLARIS_API_PORT}")
               for p in pods]
    prefix = f"gate-logs-{pid}"
    sent = logs_request_ids(prefix, LOGS_REQUESTS, [name for name, _ in targets])
    with open(os.path.join(HERE, "logs_traffic.sh"), encoding="utf-8") as f:
        script = f.read()
    if subprocess.run(logs_traffic_command(env, release.images()["curl"], f"logs-traffic-{pid}",
                                           prefix, LOGS_REQUESTS, targets),
                      input=script, text=True, check=False).returncode:
        return ["the traffic to Polaris's pods failed: see its lines above"], []
    now = batch_now(dt.datetime.now(dt.timezone.utc))
    print(f"The batch runs twice with --now {now}.", flush=True)
    cronjob = json.loads(output(kubectl + ["get", f"cronjob/{release.BATCH}", "-o", "json"]))
    events, listings = [], []
    for n in (1, 2):
        name = f"{release.BATCH}-gate-{pid}-{n}"
        run(kubectl + ["create", "-f", "-"], json.dumps(batch_job(cronjob, name, now)))
        outcome = wait_for(kubectl + ["get", f"job/{name}", "-o", "json"], job_outcome,
                           f"job/{name}")
        log = output(kubectl + ["logs", f"job/{name}"])
        print(log, end="", flush=True)
        if outcome != "succeeded":
            return [f"job/{name} {outcome}: see its output above"], []
        events.append(batch_events(log))
        listings.append(parse_listing(output(
            logs_listing_command(env, release.images()["curl"], f"logs-listing-{pid}-{n}"),
            LOGS_LISTING)))
    reader = fluent_bit_pod(env, pins["standin_images"]["fluent_bit"]["image"],
                            f"fluent-bit-standin-{pid}")
    name = reader["metadata"]["name"]
    run(kubectl + ["apply", "-f", "-"], json.dumps(reader))
    phase = wait_for(kubectl + ["get", f"pod/{name}", "-o", "json"],
                     lambda p: p.get("status", {}).get("phase") in ("Succeeded", "Failed")
                     and p["status"]["phase"], f"pod/{name}")
    records = fluent_bit_records(output(kubectl + ["logs", f"pod/{name}"]))
    run(kubectl + ["delete", f"pod/{name}", "--wait=false"])
    if phase != "Succeeded":
        return [f"the stand-in Fluent Bit ended {phase}"], []
    return logs_problems(sent, [name for name, _ in targets], events, listings[0],
                         listings[1], records)


def main(argv):
    pins = load_pins()
    if len(argv) >= 3 and argv[1] == "tools":
        tools(pins, argv[2], argv[3:] or TOOLS)
        return 0
    if len(argv) == 2 and argv[1] == "up":
        namespace = os.environ.get("NS_CNPG_SYSTEM") or "cnpg-system"
        chart = pins["cnpg_chart"]
        with tempfile.TemporaryDirectory() as d:
            chart_path = os.path.join(d, os.path.basename(chart["url"]))
            with open(chart_path, "wb") as f:
                f.write(fetch(chart["url"], chart["sha256"]))
            manifests = {name: fetch(pin["url"], pin["sha256"]).decode("utf-8")
                         for name, pin in pins["manifests"].items()}
            plugin_manifest(manifests["barman_cloud_plugin"], namespace)
            os.makedirs(SHARED_HOST_DIR, exist_ok=True)
            os.chmod(SHARED_HOST_DIR, 0o777)
            for command, stdin in up_commands(pins, chart_path, namespace, manifests):
                run(command, stdin)
        return 0
    if argv[1:] == ["secrets"]:
        with open(os.path.join(ROOT, ".env.template"), encoding="utf-8") as f:
            write_gate_env(os.path.join(ROOT, ".env"), f.read())
        run(["python3", os.path.join(ROOT, "scripts", "init_env.py"), "--apply-secrets"])
        return 0
    if argv[1:] == ["standins"]:
        env = release.load_env(ROOT, os.environ)
        with open(os.path.join(HERE, "opa", "policy.rego"), encoding="utf-8") as f, \
                open(os.path.join(HERE, "recorder", "proxy.py"), encoding="utf-8") as code, \
                tempfile.TemporaryDirectory() as d:
            with open(metrics.CONTRACT, encoding="utf-8") as scrape:
                config = metrics.prometheus_config(scrape.read(), env)
            manifest = json.dumps(standins.manifests(env, pins, f.read(), code.read(),
                                                     recorder_certificate(env, d), config))
        (apply, _), *rest = standins_commands(env, manifest)
        print("+ " + " ".join(apply) + "  (the stand-ins' List on stdin)", flush=True)
        subprocess.run(apply, input=manifest, text=True, check=True)
        for command, stdin in rest:
            run(command, stdin)
        return 0
    if argv[1:] == ["check-standins"]:
        env = release.load_env(ROOT, os.environ)
        cases = standins.load_cases(os.path.join(HERE, "opa", "cases.json"))
        with open(os.path.join(HERE, "check_standins.sh"), encoding="utf-8") as f:
            script = f.read()
        command = check_standins_command(env, cases, release.images()["curl"],
                                         f"check-standins-{os.getpid()}")
        failed = subprocess.run(command, input=script, text=True, check=False).returncode
        with open(os.path.join(HERE, "check_db.sh"), encoding="utf-8") as f:
            script = f.read()
        pg = polaris_pg_values()
        command = check_db_command(env, pg["image"], pg["database"], f"check-db-{os.getpid()}")
        failed |= subprocess.run(command, input=script, text=True, check=False).returncode
        return 1 if failed else 0
    if argv[1:2] == ["check-traffic"] and len(argv) > 2:
        env = release.load_env(ROOT, os.environ)
        log = subprocess.run(recorder_logs_command(env), capture_output=True, text=True,
                             check=True).stdout
        install_end = None
        if os.path.exists(INSTALL_RECORD):
            with open(INSTALL_RECORD, encoding="utf-8") as f:
                install_end = json.load(f)["end"]
        events = json.loads(output(restart_events_command(env["NS_CATALOG_POLARIS"])))
        problems, lines = check_traffic(log, [traffic.load(path) for path in argv[2:]],
                                        restart_window(events, install_end))
        for line in lines:
            print(line)
        return 1 if problems else 0
    if argv[1:2] == ["install"] and set(argv[2:]) <= {"--fresh", "--dry-run"} \
            and len(set(argv[2:])) == len(argv[2:]):
        start = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
        with tempfile.TemporaryDirectory() as d:
            commands, summary = install_plan(release_tag.previous(released_tags()),
                                             os.path.join(d, "previous"),
                                             "--fresh" in argv, "--dry-run" in argv)
            print(summary, flush=True)
            for command in commands:
                run(command)
        end = dt.datetime.now(dt.timezone.utc)
        if len(commands) > 1 and "--dry-run" not in argv:
            with open(INSTALL_RECORD, "w", encoding="utf-8") as f:
                json.dump({"end": end.isoformat()}, f)
        for line in measure_pulls(start.isoformat(), (end - start).total_seconds()):
            print(line, flush=True)
        return 0
    if argv[1:] == ["console-image"]:
        source = console_source()
        with tempfile.TemporaryDirectory() as d:
            commands = console_image_commands(source, d)
            for command in commands[:3]:
                run(command)
            check_commit(d, source["commit"])
            shutil.copyfile(os.path.join(CONSOLE_IMAGE_DIR, "package-lock.json"),
                            os.path.join(d, source["path"], "package-lock.json"))
            for command in commands[3:]:
                run(command)
        return 0
    if argv[1:] == ["batch-image"]:
        env = release.load_env(ROOT, os.environ)
        for command in batch_image_commands(registry=release.image_registry(env)):
            run(command)
        return 0
    if argv[1:] == ["check-logs"]:
        problems, notes = check_logs(release.load_env(ROOT, os.environ), pins)
        for line in notes + problems:
            print(line, flush=True)
        return 1 if problems else 0
    if argv[1:] == ["check-console"]:
        env = release.load_env(ROOT, os.environ)
        with open(os.path.join(HERE, "console", "check.py"), encoding="utf-8") as f:
            script = f.read()
        command = check_console_command(env, smoke_python_image(pins),
                                        f"check-console-{os.getpid()}")
        return subprocess.run(command, input=script, text=True, check=False).returncode
    if argv[1:] == ["rebuild-replica"]:
        problems, notes = rebuild_replica(release.load_env(ROOT, os.environ))
        for line in notes + problems:
            print(line, flush=True)
        return 1 if problems else 0
    if argv[1:] == ["check-polaris-pg"]:
        namespace = os.environ.get("NS_CATALOG_POLARIS") or "catalog-polaris"
        answers = [json.loads(subprocess.run(c, capture_output=True, text=True,
                                             check=True).stdout)
                   for c in polaris_pg_commands(namespace)]
        problems, notes = polaris_pg_problems(*answers)
        for line in notes + problems:
            print(line)
        return 1 if problems else 0
    if argv[1:] == ["check-polaris"]:
        env = release.load_env(ROOT, os.environ)
        answers = [json.loads(subprocess.run(c, capture_output=True, text=True,
                                             check=True).stdout)
                   for c in polaris_commands(env["NS_CATALOG_POLARIS"])]
        problems, notes = polaris_problems(*answers, env)
        for line in notes + problems:
            print(line, flush=True)
        with open(os.path.join(HERE, "check_polaris.sh"), encoding="utf-8") as f:
            script = f.read()
        command = check_polaris_command(env, release.images()["curl"],
                                        f"check-polaris-{os.getpid()}",
                                        polaris_pod_urls(answers[2]))
        failed = subprocess.run(command, input=script, text=True, check=False).returncode
        return 1 if problems or failed else 0
    if argv[1:] == ["check-iceberg"]:
        env = release.load_env(ROOT, os.environ)
        with open(os.path.join(HERE, "iceberg", "smoke.py"), encoding="utf-8") as f, \
                open(os.path.join(HERE, "iceberg", "requirements.txt"), encoding="utf-8") as r:
            manifest = json.dumps(iceberg_configmap(env, f.read(), r.read()))
        apply, pod = check_iceberg_commands(env, smoke_python_image(pins),
                                            f"check-iceberg-{os.getpid()}")
        print("+ " + " ".join(apply) + "  (the ConfigMap iceberg-smoke on stdin)", flush=True)
        subprocess.run(apply, input=manifest, text=True, check=True)
        return subprocess.run(pod, check=False).returncode
    if argv[1:] == ["gate-policies"]:
        env = release.load_env(ROOT, os.environ)
        manifest = json.dumps(network.gate_policies(env))
        apply = ["kubectl", "--context", CONTEXT, "apply", "--server-side",
                 "--field-manager=gate", "-f", "-"]
        print("+ " + " ".join(apply) + "  (the gate's allow policies on stdin)", flush=True)
        subprocess.run(apply, input=manifest, text=True, check=True)
        return 0
    if argv[1:] == ["check-network"]:
        problems, lines = check_network(release.load_env(ROOT, os.environ), pins)
        for line in lines + problems:
            print(line, flush=True)
        return 1 if problems else 0
    if argv[1:] == ["check-metrics"]:
        problems, notes = check_metrics(release.load_env(ROOT, os.environ), pins)
        for line in notes + problems:
            print(line, flush=True)
        return 1 if problems else 0
    if argv[1:] == ["check-restore"]:
        problems, notes = check_restore(release.load_env(ROOT, os.environ))
        for line in notes + problems:
            print(line, flush=True)
        return 1 if problems else 0
    print(__doc__.strip().splitlines()[-1], file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
