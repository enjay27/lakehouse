"""The kind gate (ADR-017, ADR-018, ADR-029): pinned tools, the cluster, the CNPG operator.

  tools BIN_DIR   downloads kind, kubectl and helm from gates/pins.json into BIN_DIR, each
                  checked against its pinned sha256 before it is written.
  up              creates the kind cluster (one control-plane, two workers, the pinned node
                  image), then installs the CNPG operator from its pinned chart archive into
                  NS_CNPG_SYSTEM and waits for it. The operator exists here only because the
                  gate has no operator team (ADR-029); a release never installs it.
  smoke-cnpg      creates a one-instance PostgreSQL `Cluster` in a scratch namespace, waits
                  until it is Ready, then deletes the namespace: the operator works.

Every kubectl and helm call names the gate's context and a namespace. Nothing here reaches any
cluster but the kind cluster it creates (ADR-017).

Usage: python3 gates/gate.py tools BIN_DIR | up | smoke-cnpg
"""

import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CLUSTER = "lakehouse-gate"
CONTEXT = f"kind-{CLUSTER}"
SMOKE_NAMESPACE = "gate-smoke"
SMOKE_CLUSTER = """apiVersion: postgresql.cnpg.io/v1
kind: Cluster
metadata:
  name: smoke
spec:
  instances: 1
  storage:
    size: 1Gi
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


def tools(pins, bin_dir, get=download):
    """Fetches and checks every tool first, then writes them; a bad hash writes nothing."""
    blobs = {name: fetch(pins[name]["url"], pins[name]["sha256"], get)
             for name in ("kind", "kubectl", "helm")}
    with tarfile.open(fileobj=io.BytesIO(blobs["helm"]), mode="r:gz") as tar:
        blobs["helm"] = tar.extractfile("linux-amd64/helm").read()
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
        ["helm", "--kube-context", CONTEXT, "install", "cnpg", chart_path,
         "--namespace", namespace, "--create-namespace", "--wait", "--timeout", "5m"],
    ]


def smoke_commands():
    """(argv, stdin) pairs."""
    base = ["kubectl", "--context", CONTEXT, "-n", SMOKE_NAMESPACE]
    return [
        (base + ["create", "namespace", SMOKE_NAMESPACE], None),
        (base + ["apply", "-f", "-"], SMOKE_CLUSTER),
        (base + ["wait", "--for=condition=Ready", "cluster/smoke", "--timeout=600s"], None),
        (base + ["delete", "namespace", SMOKE_NAMESPACE, "--wait=true"], None),
    ]


def run(argv, stdin=None):
    print("+ " + " ".join(argv), flush=True)
    subprocess.run(argv, input=stdin, text=True, check=True)


def main(argv):
    pins = load_pins()
    if len(argv) == 3 and argv[1] == "tools":
        tools(pins, argv[2])
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
    if len(argv) == 2 and argv[1] == "smoke-cnpg":
        for command, stdin in smoke_commands():
            run(command, stdin)
        return 0
    print(__doc__.strip().splitlines()[-1], file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
