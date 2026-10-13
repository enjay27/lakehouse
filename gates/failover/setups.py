"""What each failover setup and failure is (ADR-047 §2, §3, §5). Pure: commands and objects,
never run here.

- **C0** is `charts/polaris-postgres` as a release installs it, with CNPG's defaults.
- **C1** is C0 tuned: the `Cluster` patch below and pgjdbc's client timeouts. Its values are
  inputs to the measurement, not a decision (ADR-000 rule 11); the results name them.
- **C2** is C1 with CNPG's PgBouncer `Pooler`, in session mode, so pgjdbc's prepared
  statements work as they do without it.
- C3a, C3b and C4 need the target's PG-HA and Pgpool versions (#31) and come in their own PR.

Failures: S1 deletes the primary pod, S2 kills its postmaster, S3 pauses the kind node that
holds it. Everything but the instances runs on the control plane, so that S3 takes only the
database's node: the operator, cert-manager, the plugin, CoreDNS, the provisioner, the
stand-ins, the pooler and the probe.
"""

import hashlib
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
GATES = os.path.dirname(HERE)
sys.path.insert(0, GATES)
sys.path.insert(0, os.path.join(os.path.dirname(GATES), "install"))
import gate  # noqa: E402
import network  # noqa: E402
import release  # noqa: E402

SETUPS = ("C0", "C1", "C2")
SCENARIOS = ("S1", "S2", "S3")
CONTROL_PLANE = f"{gate.CLUSTER}-control-plane"
HOSTNAME = "kubernetes.io/hostname"
PROBE_DIR = os.path.join(HERE, "probe")
PROBE_REPOSITORY = "lakehouse/failover-probe"
PROBE_DATABASE = "failover_probe"
PROBE_PERIOD_MS = 100
POOLER = f"{release.POLARIS_PG}-pooler-rw"
PGDATA = "/var/lib/postgresql/data/pgdata"
SWITCHOVER_PHASE = "Switchover in progress"
RUN_ID = re.compile(r"^[A-Za-z0-9-]+$")

# C1, CNPG's side. failoverDelay is CNPG's default, written out. The readiness probe fails
# within 3 s of PostgreSQL stopping to answer, instead of 30 s. A primary being shut down
# goes straight to a fast shutdown instead of letting open sessions commit for 180 s.
# Synchronous replication: a commit returns once one standby has it, and waits while none
# can (ADR-047 §2). The liveness probe, its isolation check and the lease stay CNPG's.
C1_CLUSTER = {
    "failoverDelay": 0,
    "smartShutdownTimeout": 0,
    "probes": {"readiness": {"periodSeconds": 1, "timeoutSeconds": 1, "failureThreshold": 3}},
    "postgresql": {"synchronous": {"method": "any", "number": 1,
                                   "dataDurability": "required"}},
}
# C1, the client's side: pgjdbc gives up on a connect after 2 s, on a login after 3 s and on
# a read after 5 s, and TCP keepalives are on. Without them a socket to a lost node waits for
# TCP's own timeout.
C1_CLIENT = {"connectTimeout": "2", "socketTimeout": "5", "loginTimeout": "3",
             "tcpKeepAlive": "true"}


def check(setup, scenario=None):
    if setup not in SETUPS:
        raise ValueError(f"{setup}: not a setup of this harness ({', '.join(SETUPS)})")
    if scenario is not None and scenario not in SCENARIOS:
        raise ValueError(f"{scenario}: not a failure ({', '.join(SCENARIOS)})")


def cluster_patch(setup):
    """The merge patch on the `Cluster` polaris-pg, or None for the chart as released."""
    check(setup)
    return None if setup == "C0" else {"spec": C1_CLUSTER}


def pooler(setup, namespace):
    check(setup)
    if setup != "C2":
        return None
    return {"apiVersion": "postgresql.cnpg.io/v1", "kind": "Pooler",
            "metadata": {"name": POOLER, "namespace": namespace},
            "spec": {"cluster": {"name": release.POLARIS_PG}, "instances": 1, "type": "rw",
                     "pgbouncer": {"poolMode": "session"},
                     "template": {"metadata": {"labels": {release.PG_CLIENT_LABEL: "true"}},
                                  "spec": {"nodeSelector": {HOSTNAME: CONTROL_PLANE},
                                           "containers": []}}}}


def _polaris_params():
    """The query of Polaris's own JDBC URL (install/release.py)."""
    return release.jdbc_url({}).split("?", 1)[1]


def jdbc_url(setup):
    check(setup)
    host = POOLER if setup == "C2" else release.POLARIS_PG_SERVICE
    params = _polaris_params()
    if setup != "C0":
        params += "".join(f"&{k}={v}" for k, v in C1_CLIENT.items())
    return f"jdbc:postgresql://{host}:5432/{PROBE_DATABASE}?{params}"


def settings(setup, grace_s=None):
    """Every input of a setup, for its results."""
    patch = cluster_patch(setup)
    return {"cluster": patch["spec"] if patch else {}, "pooler": pooler(setup, "-") is not None,
            "jdbc_url": jdbc_url(setup), "node_monitor_grace_period_s": grace_s}


def probe_pod(setup, namespace, image, name, run_id):
    if not RUN_ID.match(run_id):
        raise ValueError(f"{run_id}: a run's name is letters, digits and dashes")
    secret = {"name": release.POLARIS_DB_SECRET}
    env = [{"name": "PGUSER", "valueFrom": {"secretKeyRef": {**secret, "key": "username"}}},
           {"name": "PGPASSWORD", "valueFrom": {"secretKeyRef": {**secret, "key": "password"}}},
           {"name": "PROBE_URL", "value": jdbc_url(setup)},
           {"name": "PROBE_RUN", "value": run_id},
           {"name": "PROBE_PERIOD_MS", "value": str(PROBE_PERIOD_MS)}]
    return {"apiVersion": "v1", "kind": "Pod",
            "metadata": {"name": name, "namespace": namespace,
                         "labels": {network.CHECK_LABEL: "true"}},
            "spec": {"restartPolicy": "Never", "nodeSelector": {HOSTNAME: CONTROL_PLANE},
                     "containers": [{"name": "probe", "image": image,
                                     "imagePullPolicy": "Never", "env": env}]}}


def kind_config(base, grace_s):
    """gates/kind.yaml, with the controller manager's node grace period set when `grace_s`
    is given (ADR-047 §3's informational job)."""
    if grace_s is None:
        return base
    patch = ("kubeadmConfigPatches:\n"
             "  - |\n"
             "    apiVersion: kubeadm.k8s.io/v1beta4\n"
             "    kind: ClusterConfiguration\n"
             "    controllerManager:\n"
             "      extraArgs:\n"
             "        - name: node-monitor-grace-period\n"
             f"          value: {grace_s}s\n")
    head, sep, tail = base.partition("\nnodes:\n")
    if not sep:
        raise ValueError("the kind config has no `nodes:` list to patch before")
    return f"{head}\n{patch}nodes:\n{tail}"


def grace_applied(command, grace_s):
    """Whether the running controller manager's `command` has the grace period."""
    return f"--node-monitor-grace-period={grace_s}s" in command


def system_namespaces(env):
    """The namespaces whose Deployments `up` pins to the control plane; the stand-ins'
    are pinned after they are applied."""
    return ["kube-system", "local-path-storage", gate.CERT_MANAGER_NAMESPACE,
            env["NS_CNPG_SYSTEM"]]


def pin_patch():
    return {"spec": {"template": {"spec": {"nodeSelector": {HOSTNAME: CONTROL_PLANE}}}}}


def inject_command(scenario, namespace, primary, node):
    """The failure, on the primary `primary` running on `node`."""
    check("C0", scenario)
    kubectl = ["kubectl", "--context", gate.CONTEXT, "-n", namespace]
    if scenario == "S1":
        return kubectl + ["delete", "pod", primary, "--wait=false"]
    if scenario == "S2":
        return kubectl + ["exec", primary, "-c", "postgres", "--", "sh", "-c",
                          f"kill -9 $(head -n 1 {PGDATA}/postmaster.pid)"]
    if node == CONTROL_PLANE:
        raise ValueError("S3 pauses a worker only: the control plane holds the API server")
    return ["docker", "pause", node]


def resume_command(node):
    return ["docker", "unpause", node]


def switchover_patch(target, timestamp):
    """The status patch `kubectl cnpg promote` makes (CNPG 1.30.1)."""
    return {"status": {"targetPrimary": target, "targetPrimaryTimestamp": timestamp,
                       "phase": SWITCHOVER_PHASE,
                       "phaseReason": f"Switching over to {target}"}}


def rows_query(run_id):
    if not RUN_ID.match(run_id):
        raise ValueError(f"{run_id}: a run's name is letters, digits and dashes")
    return f"SELECT mode, seq FROM probe_writes WHERE run = '{run_id}' ORDER BY mode, seq"


def observation(t, cluster, slices):
    """One poll: the cluster's phase and primary, and the instances the `-rw` Service's
    endpoint slices name ready."""
    status = cluster.get("status", {})
    ready = sorted(e["targetRef"]["name"] for s in slices.get("items", [])
                   for e in s.get("endpoints") or []
                   if e.get("conditions", {}).get("ready") and e.get("targetRef"))
    return {"t": t, "phase": status.get("phase", ""),
            "current_primary": status.get("currentPrimary", ""),
            "current_primary_timestamp": status.get("currentPrimaryTimestamp", ""),
            "endpoints": ready}


def probe_image(files):
    """The probe image's tag, from the build context's files {name: bytes}."""
    digest = hashlib.sha256()
    for name in sorted(files):
        digest.update(name.encode() + b"\0" + hashlib.sha256(files[name]).digest())
    return f"{PROBE_REPOSITORY}:{digest.hexdigest()[:16]}"
