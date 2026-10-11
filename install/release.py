"""Installs a release of this repository in the order of ADR-029.

0. Preconditions, checked, never installed: the CloudNativePG `Cluster` CRD is present and the
   operator in `NS_CNPG_SYSTEM` runs a version this release supports (install/supported.json).
   On the target clusters the operator is the operator team's; on the gate, gates/gate.py
   installs it first.
1. Polaris, in `NS_CATALOG_POLARIS`. Its Secrets come first (scripts/init_env.py
   --apply-secrets, ADR-006). Datahub adds its steps in P3. The gates run this same script
   (ADR-018).
   a. Its PostgreSQL: the `Cluster` polaris-pg from charts/polaris-postgres (ADR-008,
      ADR-010), installed with helm and waited on until Ready.
   b. The S3 endpoint and the policy endpoint from `.env` must answer, asked from Polaris's
      namespace by one short-lived curl pod (install/images.json): any HTTP status is an
      answer, no connection is not (ADR-029, ADR-031). It runs with --dry-run too.
   c. The bootstrap (ADR-026): Polaris's admin tool, in a short-lived pod, creates the schema,
      the realm and its root principal from the `polaris-root` Secret. A realm already
      bootstrapped is kept.
   d. Polaris 1.6.0 from charts/polaris (ADR-009), with values-lakehouse.yaml and the values
      from `.env` (the database host, the policy endpoint), waited on until rolled out.

With --dry-run, the helm upgrades are server-side dry-runs, the bootstrap does not run and
nothing waits: the dry-run gate (ADR-018).

Settings come from `.env` at the repository root, then the environment, which wins. Every
kubectl and helm call names `$KUBE_CONTEXT` (no default, ADR-004) and its namespace (ADR-007).
Needs kubectl and helm.

Usage: python3 install/release.py [--dry-run]
       Exits 0 when every step passed, 1 with the problems, 2 on an unknown option.
"""

import json
import os
import re
import secrets
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
from init_env import SECRETS, parse  # noqa: E402

CNPG_CRD = "clusters.postgresql.cnpg.io"
OPERATOR_LABEL = "app.kubernetes.io/name=cloudnative-pg"
POLARIS_PG = "polaris-pg"
POLARIS_PG_RELEASE = "polaris-postgres"
POLARIS_PG_CHART = os.path.join(ROOT, "charts", "polaris-postgres")
POLARIS_DB_SECRET = "polaris-db"
POLARIS = "polaris"
POLARIS_REALM = "POLARIS"
POLARIS_DATABASE = "polaris"
POLARIS_PG_SERVICE = f"{POLARIS_PG}-rw"
POLARIS_ROOT_SECRET = "polaris-root"
# The Secrets the chart reads (charts/polaris/values-lakehouse.yaml), from init_env.py.
POLARIS_SECRETS = (POLARIS_DB_SECRET, "polaris-s3", "polaris-token-signing")
# Every `.env` variable that holds a credential: what goes into a Secret.
CREDENTIAL_VARS = sorted({var for s in SECRETS for var in s.from_env.values()})
# The admin tool image's entrypoint. The bootstrap pod runs it only once stdin ends, that is
# once kubectl is attached, so none of its output is lost; all of it goes to stderr, which is
# what a failed command returns here (call).
ADMIN_TOOL_ENTRYPOINT = "/opt/jboss/container/java/run/run-java.sh"
BOOTSTRAP_WRAPPER = f'cat >/dev/null; exec {ADMIN_TOOL_ENTRYPOINT} "$@" 1>&2'
# What Polaris 1.6.0's admin tool says, exit 3, for a realm that has its root principal
# (polaris-core AuthBootstrapUtil.java); the schema script it ran first changes nothing.
ALREADY_BOOTSTRAPPED = "It appears this metastore manager has already been bootstrapped"
BOOTSTRAP_TAIL = 20
POLARIS_CHART = os.path.join(ROOT, "charts", "polaris")
POLARIS_VALUES = os.path.join(POLARIS_CHART, "values-lakehouse.yaml")
READY_TIMEOUT = "900s"
# The endpoint check's pod reads this script on stdin (`sh -s`), so it starts only once kubectl
# is attached and no output is lost. Each URL in its arguments gets one request; any HTTP
# status is an answer. Exits 1 when any URL gave none.
ENDPOINT_CHECK = """fail=0
for url in "$@"; do
  if code=$(curl -s -o /dev/null -w '%{http_code}' --connect-timeout 5 --max-time 15 "$url"); then
    echo "$url answered HTTP $code"
  else
    echo "$url did not answer (curl exit $?)"
    fail=1
  fi
done
exit $fail
"""
# The operator image's last path segment, then a version tag; an optional digest after it.
OPERATOR_IMAGE_RE = re.compile(r"(?:^|/)cloudnative-pg:(\d+\.\d+\.\d+)(?:@sha256:[0-9a-f]+)?$")


def load_env(root, environ):
    env = {}
    path = os.path.join(root, ".env")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            env.update(parse(f.read()))
    env.update(environ)
    return env


def supported_versions(path=os.path.join(HERE, "supported.json")):
    with open(path, encoding="utf-8") as f:
        return json.load(f)["cnpg_operator"]


def images(path=os.path.join(HERE, "images.json")):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def operator_versions(deployments_json):
    """Versions of the CNPG operator images in a `kubectl get deployments -o json` answer."""
    versions = []
    for item in json.loads(deployments_json).get("items", []):
        for container in item["spec"]["template"]["spec"].get("containers", []):
            match = OPERATOR_IMAGE_RE.search(container.get("image", ""))
            if match:
                versions.append(match.group(1))
    return versions


def call(argv, stdin=None):
    """Runs a command, with stdin if given; (exit code, stdout or, on failure, stderr)."""
    result = subprocess.run(argv, input=stdin, capture_output=True, text=True, check=False)
    return result.returncode, result.stdout if result.returncode == 0 else result.stderr


def check_preconditions(env, run, supported):
    """Step 0. Returns (problems, notes); no call is made without a context."""
    context = env.get("KUBE_CONTEXT", "")
    namespace = env.get("NS_CNPG_SYSTEM", "")
    if not context:
        return ["KUBE_CONTEXT is empty: set the cluster's context in .env (ADR-004)"], []
    if not namespace:
        return ["NS_CNPG_SYSTEM is empty: set the operator's namespace in .env (ADR-007)"], []
    base = ["kubectl", "--context", context]
    code, out = run(base + ["get", "crd", CNPG_CRD, "-o", "name"])
    if code != 0:
        return [f"CRD {CNPG_CRD} not found: the CNPG operator is missing (ADR-029)"], []
    code, out = run(base + ["-n", namespace, "get", "deployments", "-l", OPERATOR_LABEL,
                            "-o", "json"])
    if code != 0:
        return [f"cannot list the operator's deployments in {namespace}: {out.strip()}"], []
    versions = operator_versions(out)
    if not versions:
        return [f"no CNPG operator with a version tag in {namespace} ({OPERATOR_LABEL})"], []
    problems = [f"CNPG operator {v} in {namespace} is not supported; this release supports "
                + ", ".join(supported) for v in versions
                if ".".join(v.split(".")[:2]) not in supported]
    return problems, [f"Step 0: CNPG operator {', '.join(versions)} in {namespace}, supported."]


def polaris_postgres(env, run, dry_run=False):
    """Step 1, Polaris's PostgreSQL. Returns (problems, notes); no call without a namespace."""
    namespace = env.get("NS_CATALOG_POLARIS", "")
    if not namespace:
        return ["NS_CATALOG_POLARIS is empty: set Polaris's namespace in .env (ADR-007)"], []
    context = env["KUBE_CONTEXT"]
    code, _ = run(["kubectl", "--context", context, "-n", namespace, "get", "secret",
                   POLARIS_DB_SECRET, "-o", "name"])
    if code != 0:
        return [f"Secret {POLARIS_DB_SECRET} not found in {namespace}: run "
                "scripts/init_env.py --apply-secrets first (ADR-006)"], []
    helm = ["helm", "--kube-context", context, "-n", namespace, "upgrade", "--install",
            POLARIS_PG_RELEASE, POLARIS_PG_CHART]
    code, out = run(helm + (["--dry-run=server"] if dry_run else []))
    if code != 0:
        return [f"helm upgrade {POLARIS_PG_RELEASE} in {namespace} failed: {out.strip()}"], []
    if dry_run:
        return [], [f"Step 1: Cluster {POLARIS_PG} in {namespace}, server-side dry-run passed."]
    code, out = run(["kubectl", "--context", context, "-n", namespace, "wait",
                     "--for=condition=Ready", f"cluster/{POLARIS_PG}",
                     f"--timeout={READY_TIMEOUT}"])
    if code != 0:
        return [f"Cluster {POLARIS_PG} in {namespace} is not Ready: {out.strip()}"], []
    return [], [f"Step 1: Cluster {POLARIS_PG} in {namespace} is Ready."]


def jdbc_url(env):
    """Polaris's database: POLARIS_DB_HOST, or the Cluster's read-write service when empty.
    TLS is required and the server's certificate is not verified (Kade, 2026-10-11), so it
    passes the gate's recorder (ADR-035)."""
    host = env.get("POLARIS_DB_HOST") or POLARIS_PG_SERVICE
    return f"jdbc:postgresql://{host}:5432/{POLARIS_DATABASE}?sslmode=require"


def polaris_values(env):
    """The chart values that come from `.env`, on top of charts/polaris/values-lakehouse.yaml."""
    return {"advancedConfig": {"quarkus.datasource.jdbc.url": jdbc_url(env),
                               "polaris.authorization.opa.policy-uri": env["POLARIS_OPA_URL"]}}


def endpoints_answer(env, run, image=None, name=None):
    """Before Polaris: the S3 and policy endpoints answer from Polaris's namespace (ADR-029).

    Returns (problems, notes); no call is made while an endpoint is empty.
    """
    endpoints = {"S3_ENDPOINT": "the S3 endpoint", "POLARIS_OPA_URL": "the policy endpoint"}
    empty = [f"{var} is empty: set {what} in .env (ADR-031)"
             for var, what in endpoints.items() if not env.get(var)]
    if empty:
        return empty, []
    namespace = env["NS_CATALOG_POLARIS"]
    image = image or images()["curl"]
    name = name or f"endpoint-check-{secrets.token_hex(4)}"
    code, out = run(["kubectl", "--context", env["KUBE_CONTEXT"], "-n", namespace, "run", name,
                     f"--image={image}", "--restart=Never", "--rm", "-i", "--quiet",
                     "--pod-running-timeout=3m", "--command", "--", "sh", "-s", "--",
                     env["S3_ENDPOINT"], env["POLARIS_OPA_URL"]], ENDPOINT_CHECK)
    if code != 0:
        return [f"before Polaris, from {namespace}: {out.strip()} (ADR-029)"], []
    return [], [f"Step 1: the S3 endpoint and the policy endpoint answer from {namespace}."]


def masked(text, env):
    """`text` with every credential from `.env` replaced, before it is printed (ADR-002)."""
    for var in CREDENTIAL_VARS:
        if env.get(var):
            text = text.replace(env[var], "<redacted>")
    return text


def secret_ref(secret, key):
    return {"valueFrom": {"secretKeyRef": {"name": secret, "key": key}}}


def bootstrap(env, run, image=None, name=None, dry_run=False):
    """Step 1, after the endpoints: the realm and its root principal (ADR-026), created by
    Polaris's admin tool in the database Polaris uses. A realm already bootstrapped is kept.

    Returns (problems, notes); a dry-run makes no call, since the tool has no dry-run.
    """
    namespace, context = env["NS_CATALOG_POLARIS"], env["KUBE_CONTEXT"]
    if dry_run:
        return [], [f"Step 1: the bootstrap of realm {POLARIS_REALM} does not run in a dry-run."]
    code, _ = run(["kubectl", "--context", context, "-n", namespace, "get", "secret",
                   POLARIS_ROOT_SECRET, "-o", "name"])
    if code != 0:
        return [f"Secret {POLARIS_ROOT_SECRET} not found in {namespace}: run "
                "scripts/init_env.py --apply-secrets first (ADR-006)"], []
    image = image or images()["polaris_admin_tool"]
    name = name or f"polaris-bootstrap-{secrets.token_hex(4)}"
    container = {"name": name, "image": image, "stdin": True, "stdinOnce": True, "env": [
        {"name": "QUARKUS_DATASOURCE_JDBC_URL", "value": jdbc_url(env)},
        {"name": "QUARKUS_DATASOURCE_USERNAME", **secret_ref(POLARIS_DB_SECRET, "username")},
        {"name": "QUARKUS_DATASOURCE_PASSWORD", **secret_ref(POLARIS_DB_SECRET, "password")},
        {"name": "POLARIS_ROOT_CLIENT_ID", **secret_ref(POLARIS_ROOT_SECRET, "clientId")},
        {"name": "POLARIS_ROOT_CLIENT_SECRET", **secret_ref(POLARIS_ROOT_SECRET, "clientSecret")},
    ]}
    # Kubernetes expands $(VAR) from the container's env, so no credential is on this command
    # line or in the pod's spec.
    credential = f"{POLARIS_REALM},$(POLARIS_ROOT_CLIENT_ID),$(POLARIS_ROOT_CLIENT_SECRET)"
    code, out = run(["kubectl", "--context", context, "-n", namespace, "run", name,
                     f"--image={image}", "--restart=Never", "--rm", "-i", "--quiet",
                     "--pod-running-timeout=3m", "--override-type=strategic",
                     "--overrides=" + json.dumps({"spec": {"containers": [container]}}),
                     "--command", "--", "sh", "-c", BOOTSTRAP_WRAPPER, "admin-tool",
                     "bootstrap", "-r", POLARIS_REALM, "-c", credential], "\n")
    out = masked(out, env)
    if code == 0:
        return [], [f"Step 1: realm {POLARIS_REALM} bootstrapped in {namespace}: its schema "
                    "and root principal."]
    if ALREADY_BOOTSTRAPPED in out:
        return [], [f"Step 1: realm {POLARIS_REALM} in {namespace} was already bootstrapped; "
                    "kept as it is."]
    tail = "\n".join(out.strip().splitlines()[-BOOTSTRAP_TAIL:])
    return [f"the bootstrap of realm {POLARIS_REALM} in {namespace} failed (exit {code}):\n"
            f"{tail}"], []


def polaris(env, run, dry_run=False):
    """Step 1, last: Polaris from charts/polaris with values-lakehouse.yaml and the values from
    `.env`, waited on until its rollout is done. Returns (problems, notes)."""
    namespace, context = env["NS_CATALOG_POLARIS"], env["KUBE_CONTEXT"]
    code, out = run(["kubectl", "--context", context, "-n", namespace, "get", "secret",
                     *POLARIS_SECRETS, "-o", "name"])
    if code != 0:
        return [f"Secrets {', '.join(POLARIS_SECRETS)} must exist in {namespace}: run "
                f"scripts/init_env.py --apply-secrets first (ADR-006): {out.strip()}"], []
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(polaris_values(env), f)
    try:
        helm = ["helm", "--kube-context", context, "-n", namespace, "upgrade", "--install",
                POLARIS, POLARIS_CHART, "-f", POLARIS_VALUES, "-f", f.name]
        code, out = run(helm + (["--dry-run=server"] if dry_run else []))
    finally:
        os.unlink(f.name)
    if code != 0:
        return [f"helm upgrade {POLARIS} in {namespace} failed: {out.strip()}"], []
    if dry_run:
        return [], [f"Step 1: Polaris in {namespace}, server-side dry-run passed."]
    code, out = run(["kubectl", "--context", context, "-n", namespace, "rollout", "status",
                     f"deployment/{POLARIS}", f"--timeout={READY_TIMEOUT}"])
    if code != 0:
        return [f"deployment/{POLARIS} in {namespace} did not finish its rollout: "
                f"{out.strip()}"], []
    return [], [f"Step 1: deployment/{POLARIS} in {namespace} rolled out."]


def install(env, run, supported, dry_run):
    """Every step in order; the first step with problems stops the rest."""
    notes = []
    for step in (lambda: check_preconditions(env, run, supported),
                 lambda: polaris_postgres(env, run, dry_run),
                 lambda: endpoints_answer(env, run),
                 lambda: bootstrap(env, run, dry_run=dry_run),
                 lambda: polaris(env, run, dry_run)):
        problems, more = step()
        notes += more
        if problems:
            return problems, notes
    return [], notes


def main(argv):
    if argv[1:] not in ([], ["--dry-run"]):
        print(__doc__.strip().splitlines()[-2].strip(), file=sys.stderr)
        return 2
    env = load_env(ROOT, os.environ)
    problems, notes = install(env, call, supported_versions(), dry_run=len(argv) == 2)
    for line in notes + problems:
        print(line)
    return 1 if problems else 0

if __name__ == "__main__":
    sys.exit(main(sys.argv))
