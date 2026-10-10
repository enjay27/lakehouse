"""Installs a release of this repository in the order of ADR-029.

0. Preconditions, checked, never installed: the CloudNativePG `Cluster` CRD is present and the
   operator in `NS_CNPG_SYSTEM` runs a version this release supports (install/supported.json).
   On the target clusters the operator is the operator team's; on the gate, gates/gate.py
   installs it first.
1. Polaris: its PostgreSQL, the `Cluster` polaris-pg from charts/polaris-postgres in
   `NS_CATALOG_POLARIS` (ADR-008, ADR-010), installed with helm and waited on until Ready. The
   `polaris-db` Secret must exist first (scripts/init_env.py --apply-secrets, ADR-006). Polaris
   itself follows in P2; Datahub adds its steps in P3. The gates run this same script (ADR-018).

With --dry-run, step 1 is a server-side dry-run of the helm upgrade and nothing waits: the
dry-run gate (ADR-018).

Settings come from `.env` at the repository root, then the environment, which wins. Every
kubectl and helm call names `$KUBE_CONTEXT` (no default, ADR-004) and its namespace (ADR-007).
Needs kubectl and helm.

Usage: python3 install/release.py [--dry-run]
       Exits 0 when every step passed, 1 with the problems, 2 on an unknown option.
"""

import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
from init_env import parse  # noqa: E402

CNPG_CRD = "clusters.postgresql.cnpg.io"
OPERATOR_LABEL = "app.kubernetes.io/name=cloudnative-pg"
POLARIS_PG = "polaris-pg"
POLARIS_PG_RELEASE = "polaris-postgres"
POLARIS_PG_CHART = os.path.join(ROOT, "charts", "polaris-postgres")
POLARIS_DB_SECRET = "polaris-db"
READY_TIMEOUT = "900s"
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


def operator_versions(deployments_json):
    """Versions of the CNPG operator images in a `kubectl get deployments -o json` answer."""
    versions = []
    for item in json.loads(deployments_json).get("items", []):
        for container in item["spec"]["template"]["spec"].get("containers", []):
            match = OPERATOR_IMAGE_RE.search(container.get("image", ""))
            if match:
                versions.append(match.group(1))
    return versions


def call(argv):
    """Runs a command; (exit code, stdout or, on failure, stderr)."""
    result = subprocess.run(argv, capture_output=True, text=True, check=False)
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


def install(env, run, supported, dry_run):
    """Every step in order; the first step with problems stops the rest."""
    problems, notes = check_preconditions(env, run, supported)
    if problems:
        return problems, notes
    problems, more = polaris_postgres(env, run, dry_run)
    return problems, notes + more


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
