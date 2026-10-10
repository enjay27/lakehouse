"""Installs a release of this repository in the order of ADR-029.

Today the release holds no service yet, so only step 0 runs:

0. Preconditions, checked, never installed: the CloudNativePG `Cluster` CRD is present and the
   operator in `NS_CNPG_SYSTEM` runs a version this release supports (install/supported.json).
   On the target clusters the operator is the operator team's; on the gate, gates/gate.py
   installs it first.

Polaris (P2) and Datahub (P3) add their steps here; the gates run this same script (ADR-018).

Settings come from `.env` at the repository root, then the environment, which wins. Every
kubectl call names `--context $KUBE_CONTEXT` (no default, ADR-004) and its namespace (ADR-007).

Usage: python3 install/release.py     Exits 0 when every step passed, 1 with the problems.
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


def kubectl(argv):
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


def main():
    env = load_env(ROOT, os.environ)
    problems, notes = check_preconditions(env, kubectl, supported_versions())
    for line in problems or notes:
        print(line)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
