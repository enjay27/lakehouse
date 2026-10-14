"""Polaris's network policies on the gate (ADR-048 §5, §6). Never part of a release.

- **Gate-only callers (§5):** `gate_policies(env)` is a v1 List of two allow policies in
  NS_CATALOG_POLARIS that `gate.py gate-policies` applies just before this commit's release:
  the gate's check pods (label CHECK_LABEL) reach every port of every pod, and the PostgreSQL
  recorder in NS_STANDINS reaches the CNPG instances on 5432 (ADR-034).
- **check-network (§6, ADR-046 §4):** one namespace per caller class, labelled as the gate's
  `.env` says (GATE_CALLER_LABELS), one without any label, and Datahub's namespace, empty. From a
  short-lived pod in each, and from an unlabelled pod in Polaris's namespace, PROBE tries a TCP
  connection to each target of CASES. Two more pods carry CNPG's join label (POD_LABELS), one in
  Polaris's namespace and one in the unlabelled one (ADR-048 §6). An allowed case must connect; a refused one must time
  out or be reset. A refused case that connects means the cluster does not enforce
  NetworkPolicy: ADR-048 says to stop there. polaris-pg's own Services expose 5432 only, so
  the gate adds one, METRICS_SERVICE, for the instances' exporter on 9187.
"""

import collections
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "install"))
import release  # noqa: E402
import standins  # noqa: E402

CHECK_LABEL = standins.CHECK_LABEL
# The namespace label the gate's `.env` gives each caller class, and its source namespaces.
CALLER_KEY = "lakehouse-gate/caller"
GATE_CALLER_LABELS = {var: f"{CALLER_KEY}={name}" for name, var in release.CALLER_VARS.items()}
SOURCE_PREFIX = "gate-net-"
UNLABELLED = "none"
# Seconds a refused connection waits before it counts as blocked.
TIMEOUT = 5
INSTANCES = {"cnpg.io/podRole": "instance"}
# The label CNPG puts on the Job pod that clones the primary to make a replica (ADR-048 §3).
JOIN = {"cnpg.io/jobRole": "join"}
# The sources whose probe pod carries labels: CNPG's join label, never the check label.
POD_LABELS = {"polaris-join": JOIN, "none-join": JOIN}
# The gate's Service for CNPG's exporter on polaris-pg's instances (ADR-046 §4).
METRICS_SERVICE = "gate-polaris-pg-metrics"
PG_METRICS_PORT = 9187
NAME = "kubernetes.io/metadata.name"

Case = collections.namedtuple("Case", "source target allowed")
# One allowed and one refused connection per rule of ADR-048 §3, by source: a caller class,
# `none` (a namespace with no label), `datahub` (NS_CATALOG_DATAHUB), `polaris` (a pod in
# NS_CATALOG_POLARIS without the check label), or `polaris-join` and `none-join` (a pod with
# CNPG's join label in those two namespaces).
CASES = [
    Case("gateway", "polaris-api", True),
    Case("gateway", "polaris-management", False),
    Case("gateway", "polaris-pg", False),
    Case("query", "polaris-api", True),
    Case("query", "console", False),
    Case("metrics", "polaris-management", True),
    Case("metrics", "polaris-api", False),
    Case("metrics", "polaris-pg-metrics", True),
    Case("gateway", "polaris-pg-metrics", False),
    Case("ingress", "polaris-api", True),
    Case("ingress", "console", True),
    Case("ingress", "polaris-management", False),
    Case("datahub", "polaris-api", True),
    Case("datahub", "polaris-management", False),
    Case(UNLABELLED, "polaris-api", False),
    Case(UNLABELLED, "polaris-management", False),
    Case(UNLABELLED, "console", False),
    Case(UNLABELLED, "polaris-pg", False),
    Case(UNLABELLED, "polaris-pg-metrics", False),
    Case(UNLABELLED, "recorder", False),
    Case(UNLABELLED, "seaweedfs", False),
    Case(UNLABELLED, "opa", False),
    Case("polaris", "polaris-api", False),
    Case("polaris", "polaris-pg", False),
    Case("polaris", "recorder", True),
    Case("polaris", "seaweedfs", False),
    Case("polaris", "opa", False),
    Case("polaris-join", "polaris-pg", True),
    Case("none-join", "polaris-pg", False),
]

# Connects to each `host:port` argument at once and prints `host:port RESULT` for each:
# reached (connected), refused (reset: the port is closed or a policy rejected it), blocked
# (timed out or unreachable: dropped), error (anything else, a name that does not resolve).
PROBE = r"""
import concurrent.futures, errno, socket, sys
DROPPED = (errno.EHOSTUNREACH, errno.ENETUNREACH, errno.EACCES, errno.EPERM)
def probe(target, timeout):
    host, port = target.rsplit(":", 1)
    try:
        socket.create_connection((host, int(port)), timeout=timeout).close()
        return "reached"
    except ConnectionRefusedError:
        return "refused"
    except socket.gaierror:
        return "error"
    except (TimeoutError, socket.timeout):
        return "blocked"
    except OSError as e:
        return "blocked" if e.errno in DROPPED else "error"
if __name__ == "__main__":
    timeout, targets = float(sys.argv[1]), sys.argv[2:]
    with concurrent.futures.ThreadPoolExecutor(max(len(targets), 1)) as pool:
        for target, result in zip(targets, pool.map(lambda t: probe(t, timeout), targets)):
            print(target, result, flush=True)
"""

def _svc(name, namespace, port):
    return f"{name}.{namespace}.svc.cluster.local:{port}"


def targets(env):
    """Each target of CASES, as host:port through its Service."""
    pol, si = env["NS_CATALOG_POLARIS"], env["NS_STANDINS"]
    return {
        "polaris-api": _svc(release.POLARIS, pol, 8181),
        "polaris-management": _svc(f"{release.POLARIS}-mgmt", pol, 8182),
        "console": _svc(release.CONSOLE, pol, 8080),
        "polaris-pg": _svc(release.POLARIS_PG_SERVICE, pol, standins.PG_PORT),
        "polaris-pg-metrics": _svc(METRICS_SERVICE, pol, PG_METRICS_PORT),
        "recorder": _svc("recorder", si, standins.S3_PORT),
        "seaweedfs": _svc("seaweedfs", si, standins.S3_PORT),
        "opa": _svc("opa", si, standins.OPA_PORT),
    }


def source_namespaces(env):
    """{source: (namespace, labels)}; labels None for a namespace the gate does not create for
    that source: Polaris's, which it neither creates nor labels, and the join sources', which
    other sources already name."""
    sources = {}
    for name, var in release.CALLER_VARS.items():
        key, _, value = env[var].partition("=")
        sources[name] = (SOURCE_PREFIX + name, {key: value})
    sources[UNLABELLED] = (SOURCE_PREFIX + UNLABELLED, {})
    sources["datahub"] = (env["NS_CATALOG_DATAHUB"], {})
    sources["polaris"] = (env["NS_CATALOG_POLARIS"], None)
    sources["polaris-join"] = (env["NS_CATALOG_POLARIS"], None)
    sources["none-join"] = (SOURCE_PREFIX + UNLABELLED, None)
    return sources


def namespaces_manifest(env):
    """A v1 List of the source namespaces the gate creates, with their labels."""
    items = []
    for namespace, labels in source_namespaces(env).values():
        if labels is None:
            continue
        metadata = {"name": namespace}
        if labels:
            metadata["labels"] = labels
        items.append({"apiVersion": "v1", "kind": "Namespace", "metadata": metadata})
    return {"apiVersion": "v1", "kind": "List", "items": items}


def metrics_service(env):
    """The gate's Service in Polaris's namespace for polaris-pg's instances on 9187."""
    return {"apiVersion": "v1", "kind": "Service",
            "metadata": {"name": METRICS_SERVICE, "namespace": env["NS_CATALOG_POLARIS"],
                         "labels": {"app.kubernetes.io/part-of": "gate"}},
            "spec": {"selector": {"cnpg.io/cluster": release.POLARIS_PG, **INSTANCES},
                     "ports": [{"name": "metrics", "port": PG_METRICS_PORT,
                                "targetPort": PG_METRICS_PORT, "protocol": "TCP"}]}}


def _policy(name, namespace, pod_selector, ingress):
    return {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
            "metadata": {"name": name, "namespace": namespace,
                         "labels": {"app.kubernetes.io/part-of": "gate"}},
            "spec": {"podSelector": pod_selector, "policyTypes": ["Ingress"],
                     "ingress": ingress}}


def gate_policies(env):
    """§5: the gate's check pods and the PostgreSQL recorder, in Polaris's namespace."""
    namespace = env["NS_CATALOG_POLARIS"]
    return {"apiVersion": "v1", "kind": "List", "items": [
        _policy("gate-checks", namespace, {}, [{"from": [
            {"podSelector": {"matchLabels": {CHECK_LABEL: "true"}}}]}]),
        _policy("gate-recorder-postgres", namespace, {"matchLabels": INSTANCES}, [{
            "ports": [{"protocol": "TCP", "port": standins.PG_PORT}],
            "from": [{"namespaceSelector": {"matchLabels": {NAME: env["NS_STANDINS"]}},
                      "podSelector": {"matchLabels": {
                          "app.kubernetes.io/name": "recorder"}}}]}]),
    ]}


def probe_command(namespace, image, name, addresses, timeout=TIMEOUT, labels=None):
    """A short-lived pod in `namespace`, without the check label but with `labels` if given,
    running PROBE (on stdin)."""
    import gate  # gate.py imports this module; its context is read when called.
    flags = [f"--labels={','.join(f'{k}={v}' for k, v in labels.items())}"] if labels else []
    return ["kubectl", "--context", gate.CONTEXT, "-n", namespace, "run", name, *flags,
            f"--image={image}", "--restart=Never", "--rm", "-i", "--quiet",
            "--pod-running-timeout=3m", "--command", "--", "python3", "-", str(timeout),
            *addresses]


def parse_probe(text):
    """{host:port: result} from PROBE's output; other lines are ignored."""
    results = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and ":" in parts[0]:
            results[parts[0]] = parts[1]
    return results


def holds(case, result):
    """An allowed case must connect; a refused one must be dropped or reset."""
    return result == "reached" if case.allowed else result in ("blocked", "refused")


def report(cases, results):
    """(problems, lines) for `results` {source: {target: result}}."""
    problems, lines, leaked = [], [], False
    for case in cases:
        result = results.get(case.source, {}).get(case.target)
        line = (f"{case.source} -> {case.target}: {result or 'no result'} "
                f"({'allowed' if case.allowed else 'refused'})")
        if holds(case, result):
            lines.append(f"ok   {line}")
        else:
            problems.append(f"FAIL {line}")
            leaked |= not case.allowed and result == "reached"
    if leaked:
        problems.append("A connection ADR-048 refuses got through: this cluster does not "
                        "enforce NetworkPolicy. Stop here; a new ADR changes ADR-017's cluster "
                        "with a CNI that does (ADR-048 §6).")
    return problems, lines
