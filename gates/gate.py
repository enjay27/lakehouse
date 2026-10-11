"""The kind gate (ADR-017, ADR-018, ADR-029): pinned tools, the cluster, the CNPG operator.

  tools BIN_DIR [NAME...]
                  downloads kind, kubectl, helm and kubeconform (or only the NAMEs) from
                  gates/pins.json into BIN_DIR, each checked against its pinned sha256 before
                  anything is written.
  up              creates the kind cluster (one control-plane, two workers, the pinned node
                  image), lets the control-plane take pods, so three nodes hold the three
                  PostgreSQL instances apart, then installs the CNPG operator from its pinned
                  chart archive into NS_CNPG_SYSTEM and waits for it. The operator exists here
                  only because the gate has no operator team (ADR-029); a release never
                  installs it.
  secrets         writes `.env` for the gate (its context, the stand-ins' endpoints and
                  the S3 stand-in's two keys, Polaris's and the smoke tests', which are the
                  gate's own, ADR-031), then runs
                  scripts/init_env.py --apply-secrets (ADR-006). Refuses an existing `.env`.
  standins        applies the stand-ins (gates/standins.py: SeaweedFS for S3, OPA with
                  gates/opa/policy.rego) in NS_STANDINS and waits for them (ADR-031). Never
                  part of a release; the install's endpoint check needs them on the gate.
                  With them the recorder (ADR-034), with a TLS certificate made here.
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
                  its tag, then this commit's release over it (ADR-021); with no tag, or with
                  --fresh, only this commit's release. With --dry-run, this commit's release is
                  a server-side dry-run over the installed one (ADR-018). Prints which it did.
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
  check-iceberg   runs gates/iceberg/smoke.py in a short-lived pod on the pinned Python image,
                  mounted with its hashed requirements from the ConfigMap iceberg-smoke: PyIceberg
                  as gate_smoke, with the smoke tests' own S3 key (Secret smoke-s3), writes a
                  table into gate_catalog on the S3 endpoint and reads it back, and the policy
                  endpoint refuses what the gate's policy does not grant (ADR-026, ADR-031).

Every kubectl and helm call names the gate's context, and a namespace where the object has
one. Nothing here reaches any cluster but the kind cluster it creates (ADR-017).

Usage: gate.py tools BIN_DIR [NAME...] | up | secrets | standins | check-standins | check-traffic FILE... | install [--fresh] [--dry-run] | check-polaris-pg | check-polaris | console-image | check-iceberg
"""

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "install"))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import init_env  # noqa: E402
import release  # noqa: E402
import release_tag  # noqa: E402
import standins  # noqa: E402
import traffic  # noqa: E402

CLUSTER = "lakehouse-gate"
CONTEXT = f"kind-{CLUSTER}"
TOOLS = ("kind", "kubectl", "helm", "kubeconform")
# The binary inside each tool's archive; the others are plain binaries.
ARCHIVE_MEMBERS = {"helm": "linux-amd64/helm", "kubeconform": "kubeconform"}
# On the gate the S3 stand-in is the gate's own, so the gate makes up its credentials, Polaris's
# and the smoke tests'; on a test or target cluster they are another team's and typed in by
# hand (ADR-031).
GATE_ENV_GENERATED = ("POLARIS_S3_ACCESS_KEY_ID", "POLARIS_S3_SECRET_ACCESS_KEY",
                      "SMOKE_S3_ACCESS_KEY_ID", "SMOKE_S3_SECRET_ACCESS_KEY")
# On the gate, the endpoints of other teams' services are the stand-ins' (ADR-031).
STANDIN_REGION = "us-east-1"
STANDIN_BUCKET = "polaris-warehouse"
# Polaris's own S3 key, as the AWS SDK reads it: catalogs without STS are read and written with
# it (charts/polaris/values-lakehouse.yaml).
POLARIS_S3_ENV = {"AWS_ACCESS_KEY_ID": "accessKeyId", "AWS_SECRET_ACCESS_KEY": "secretAccessKey"}
# Polaris's pods and ports, as charts/polaris renders them.
POLARIS_POD_LABELS = "app.kubernetes.io/name=polaris,app.kubernetes.io/instance=polaris"
POLARIS_API_PORT, POLARIS_MGMT_PORT = 8181, 8182
# The catalog the smoke tests create and the gate's policy names (gates/opa/policy.rego).
ICEBERG_CATALOG = "gate_catalog"
# The Polaris console's image, built here from a pinned polaris-tools commit (ADR-028).
CONSOLE_IMAGE_DIR = os.path.join(ROOT, "images", "polaris-console")
CONSOLE_IMAGE_REPOSITORY = "lakehouse/polaris-console"
CONSOLE_PORT = 8080


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


def up_commands(pins, chart_path, namespace):
    return [
        ["kind", "create", "cluster", "--name", CLUSTER,
         "--config", os.path.join(HERE, "kind.yaml"),
         "--image", pins["node_image"]["image"], "--wait", "180s"],
        ["kubectl", "--context", CONTEXT, "taint", "nodes", f"{CLUSTER}-control-plane",
         "node-role.kubernetes.io/control-plane:NoSchedule-"],
        ["helm", "--kube-context", CONTEXT, "install", "cnpg", chart_path,
         "--namespace", namespace, "--create-namespace", "--wait", "--timeout", "5m"],
    ]


def standin_endpoints(ns):
    """The `.env` values for the gate: every connection Polaris makes goes through the
    recorder in namespace `ns`, which forwards it to the stand-in or to polaris-pg (ADR-034)."""
    recorder = f"recorder.{ns}.svc.cluster.local"
    return {
        "S3_ENDPOINT": f"http://{recorder}:{standins.S3_PORT}",
        "S3_REGION": STANDIN_REGION,
        "POLARIS_WAREHOUSE_BUCKET": STANDIN_BUCKET,
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
    credentials and the console's settings; init_env fills the rest."""
    template = init_env.parse(template_text)
    fixed = {"KUBE_CONTEXT": CONTEXT, **standin_endpoints(template["NS_STANDINS"]),
             **console_settings(template["NS_CATALOG_POLARIS"])}
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


def standins_commands(env):
    """Apply the stand-ins' List (on stdin), then wait until both are ready."""
    return [
        ["kubectl", "--context", CONTEXT, "apply", "--server-side", "--field-manager=gate",
         "-f", "-"],
        ["kubectl", "--context", CONTEXT, "-n", env["NS_STANDINS"], "rollout", "status",
         "deployment/seaweedfs", "deployment/opa", "deployment/recorder", "--timeout=5m"],
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
            f"--image={image}", "--restart=Never", "--rm", "-i", "--quiet",
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
            f"--image={image}", "--restart=Never", "--rm", "-i", "--quiet",
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


def check_traffic(log_text, expectation_lists):
    """(problems, lines): a count of the records, then one line per expectation."""
    recorded = traffic.records(log_text)
    by_proto = {p: sum(1 for r in recorded
                       if r.get("proto") == p and r.get("event") != "listening")
                for p in ("http", "pg")}
    lines = [f"Recorder: {by_proto['http'] + by_proto['pg']} records: {by_proto['http']} HTTP, "
             f"{by_proto['pg']} PostgreSQL."]
    problems = []
    for expectations in expectation_lists:
        more_problems, more_lines = traffic.check(recorded, expectations)
        problems += more_problems
        lines += more_lines
    return problems, lines


def install_plan(previous, previous_dir, fresh, dry_run=False):
    """The commands that install a release on the gate, and the line that says which."""
    this = ["python3", os.path.join(ROOT, "install", "release.py")] + (
        ["--dry-run"] if dry_run else [])
    then = " (server-side dry-run)" if dry_run else ""
    if fresh:
        return [this], f"Fresh install of this commit{then}, as asked."
    if not previous:
        return [this], f"No release to upgrade from: fresh install{then} (ADR-021)."
    return [
        ["git", "-C", ROOT, "worktree", "add", "--detach", previous_dir, previous],
        ["python3", os.path.join(previous_dir, "install", "release.py")],
        this,
    ], f"Upgrade: {previous} installed from its tag, then this commit over it{then} (ADR-021)."


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
            f"--image={image}", "--restart=Never", "--rm", "-i", "--quiet",
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
        ["kubectl", "--context", CONTEXT, "-n", ns, "run", name, f"--image={image}",
         "--restart=Never", "--rm", "--attach", "--quiet", "--pod-running-timeout=3m",
         "--override-type=strategic", "--overrides", json.dumps(overrides),
         "--command", "--", "sh", "-c", install, "smoke",
         f"http://{release.POLARIS}:{POLARIS_API_PORT}", release.POLARIS_REALM,
         ICEBERG_CATALOG, env["S3_ENDPOINT"], env["S3_REGION"],
         env["POLARIS_WAREHOUSE_BUCKET"]],
    ]


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
            for command in up_commands(pins, chart_path, namespace):
                run(command)
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
            manifest = json.dumps(standins.manifests(env, pins, f.read(), code.read(),
                                                     recorder_certificate(env, d)))
        apply, wait = standins_commands(env)
        print("+ " + " ".join(apply) + "  (the stand-ins' List on stdin)", flush=True)
        subprocess.run(apply, input=manifest, text=True, check=True)
        run(wait)
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
        problems, lines = check_traffic(log, [traffic.load(path) for path in argv[2:]])
        for line in lines:
            print(line)
        return 1 if problems else 0
    if argv[1:2] == ["install"] and set(argv[2:]) <= {"--fresh", "--dry-run"} \
            and len(set(argv[2:])) == len(argv[2:]):
        with tempfile.TemporaryDirectory() as d:
            commands, summary = install_plan(release_tag.previous(released_tags()),
                                             os.path.join(d, "previous"),
                                             "--fresh" in argv, "--dry-run" in argv)
            print(summary, flush=True)
            for command in commands:
                run(command)
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
    print(__doc__.strip().splitlines()[-1], file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
