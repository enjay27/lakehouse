"""What each failover setup and failure is (ADR-047 §2, §3, §5). Pure: commands and objects,
never run here.

- **C0** is `charts/polaris-postgres` as a release installs it, its tuning values included.
- **C1** is C0 tuned: the `Cluster` patch below and pgjdbc's client timeouts. Its values are
  inputs to the measurement, not a decision (ADR-000 rule 11); the results name them.
- **C2** is C1 with CNPG's PgBouncer `Pooler`, in session mode, so pgjdbc's prepared
  statements work as they do without it.
- **C1q** is C1 with CNPG's `failoverQuorum`: a standby is promoted only when the standbys
  CNPG reaches must include one holding every synchronously confirmed commit. A variant,
  run only when named, measured before any release has it (ADR-056 §4).
- C3a, C3b and C4 need the target's PG-HA and Pgpool versions (#31) and come in their own PR.

Failures: S1 deletes the primary pod, S2 kills its postmaster, S3 pauses the kind node that
holds it. S4 pauses both workers, the primary's and a standby's, and leaves the standby on the
control plane; S5 is S4 after that survivor was cut off from the primary, so that it fell
behind (ADR-058 §1). S4 and S5 run only when named. Everything but the instances runs on the
control plane, so that S3 to S5 take only the database's nodes: the operator, cert-manager,
the plugin, CoreDNS, the provisioner, the stand-ins, the pooler and the probe.
"""

import hashlib
import ipaddress
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
# Variants run only when named, so that "all setups" stays ADR-047 §2's (ADR-056 §4).
VARIANTS = ("C1q",)
SCENARIOS = ("S1", "S2", "S3")
# Failures run only when named, so that "all failures" stays ADR-047 §3's (ADR-058 §1).
NAMED_SCENARIOS = ("S4", "S5")
# The failures that pause kind nodes, before which the primary moves to a worker.
NODE_LOSSES = ("S3", "S4", "S5")
# S5: how long the survivor is cut off from the primary before both workers are paused.
CUT_S = 10
RECEIVED_QUERY = "SELECT pg_last_wal_receive_lsn()"
STREAMING_QUERY = ("SELECT application_name FROM pg_stat_replication "
                   "WHERE state = 'streaming'")
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
# The informational job's node grace period, against Kubernetes' 50 s (ADR-047 §3).
GRACE_S = 20

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
# C1q: C1, and CNPG promotes only a standby it can show holds every synchronous commit.
C1Q_CLUSTER = {**C1_CLUSTER, "postgresql": {"synchronous": {
    **C1_CLUSTER["postgresql"]["synchronous"], "failoverQuorum": True}}}
# C1, the client's side: pgjdbc gives up on a connect after 2 s, on a login after 3 s and on
# a read after 5 s, and TCP keepalives are on. Without them a socket to a lost node waits for
# TCP's own timeout.
C1_CLIENT = {"connectTimeout": "2", "socketTimeout": "5", "loginTimeout": "3",
             "tcpKeepAlive": "true"}


def check(setup, scenario=None):
    if setup not in SETUPS + VARIANTS:
        raise ValueError(f"{setup}: not a setup of this harness "
                         f"({', '.join(SETUPS + VARIANTS)})")
    if scenario is not None and scenario not in SCENARIOS + NAMED_SCENARIOS:
        raise ValueError(f"{scenario}: not a failure "
                         f"({', '.join(SCENARIOS + NAMED_SCENARIOS)})")


def matrix(setups_csv, scenarios_csv, grace_job):
    """failover.yml's jobs: one per setup, and C1 under S3 on a cluster with GRACE_S as the
    node grace period (ADR-047 §3), each with its failures. Empty inputs mean all."""
    chosen = setups_csv.split(",") if setups_csv else list(SETUPS)
    failures = scenarios_csv.split(",") if scenarios_csv else list(SCENARIOS)
    for setup in chosen:
        for scenario in failures:
            check(setup, scenario)
    jobs = [{"name": s, "setup": s, "grace": "", "scenarios": ",".join(failures)}
            for s in SETUPS + VARIANTS if s in chosen]
    if grace_job and "C1" in chosen and "S3" in failures:
        jobs.append({"name": f"C1-grace{GRACE_S}", "setup": "C1", "grace": str(GRACE_S),
                     "scenarios": "S3"})
    return jobs


def cluster_patch(setup):
    """The merge patch on the `Cluster` polaris-pg, or None for the chart as released."""
    check(setup)
    if setup == "C0":
        return None
    return {"spec": C1Q_CLUSTER if setup == "C1q" else C1_CLUSTER}


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
    """What Polaris connects with: its JDBC URL's query (install/release.py) and the driver
    properties its values pass to every connection, in order."""
    query = release.jdbc_url({}).split("?", 1)[1]
    params = dict(p.split("=", 1) for p in query.split("&"))
    return {**params, **release.polaris_jdbc_properties()}


def jdbc_url(setup):
    check(setup)
    host = POOLER if setup == "C2" else release.POLARIS_PG_SERVICE
    params = _polaris_params()
    if setup != "C0":
        params.update(C1_CLIENT)
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return f"jdbc:postgresql://{host}:5432/{PROBE_DATABASE}?{query}"


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


def inject_command(scenario, namespace, primary, node, standby_node=None):
    """The failure, on the primary `primary` running on `node`; for S4 and S5 also on the
    standby's node `standby_node`, paused in the same command."""
    check("C0", scenario)
    kubectl = ["kubectl", "--context", gate.CONTEXT, "-n", namespace]
    if scenario == "S1":
        return kubectl + ["delete", "pod", primary, "--wait=false"]
    if scenario == "S2":
        return kubectl + ["exec", primary, "-c", "postgres", "--", "sh", "-c",
                          f"kill -9 $(head -n 1 {PGDATA}/postmaster.pid)"]
    nodes = [node] if scenario == "S3" else [node, standby_node]
    if CONTROL_PLANE in nodes:
        raise ValueError(f"{scenario} pauses workers only: the control plane holds the API "
                         "server")
    if None in nodes or len(set(nodes)) != len(nodes):
        raise ValueError(f"{scenario} pauses two workers: the primary's and a standby's")
    return ["docker", "pause", *nodes]


def resume_command(*nodes):
    return ["docker", "unpause", *nodes]


def cut_commands(survivor_ip, primary_ip):
    """S5: (add, remove), the commands that drop and then let through every packet between
    the survivor's pod and the primary's, both ways. The survivor runs on the control plane,
    so its traffic to and from a worker passes that node's FORWARD chain."""
    ips = [str(ipaddress.ip_address(ip)) for ip in (survivor_ip, primary_ip)]
    if ips[0] == ips[1]:
        raise ValueError("the survivor and the primary are one pod")
    iptables = ["docker", "exec", CONTROL_PLANE, "iptables"]
    pairs = [(ips[0], ips[1]), (ips[1], ips[0])]
    add = [iptables + ["-I", "FORWARD", "1", "-s", a, "-d", b, "-j", "DROP"] for a, b in pairs]
    remove = [iptables + ["-D", "FORWARD", "-s", a, "-d", b, "-j", "DROP"] for a, b in pairs]
    return add, remove


def lsn(text):
    """A PostgreSQL LSN, `X/Y` in hex, as a number."""
    high, sep, low = text.strip().partition("/")
    if not sep:
        raise ValueError(f"{text!r}: not an LSN")
    return (int(high, 16) << 32) + int(low, 16)


def standby_flush_query(standby):
    """On the primary: its current LSN and what the standby `standby` has flushed."""
    if not RUN_ID.match(standby):
        raise ValueError(f"{standby}: an instance's name is letters, digits and dashes")
    return ("SELECT pg_current_wal_lsn(), flush_lsn FROM pg_stat_replication "
            f"WHERE application_name = '{standby}'")


def switchover_patch(target, timestamp):
    """The status patch `kubectl cnpg promote` makes (CNPG 1.30.1)."""
    return {"status": {"targetPrimary": target, "targetPrimaryTimestamp": timestamp,
                       "phase": SWITCHOVER_PHASE,
                       "phaseReason": f"Switching over to {target}"}}


def _ready(pod):
    return any(c["type"] == "Ready" and c["status"] == "True"
               for c in pod.get("status", {}).get("conditions", []))


def switchover_target(pods, primary):
    """Before S3 to S5: None when the primary already runs on a worker; else a ready standby
    on a worker to switch over to."""
    placed = {p["metadata"]["name"]: p for p in pods["items"]}
    if placed[primary]["spec"].get("nodeName") != CONTROL_PLANE:
        return None
    for name, pod in sorted(placed.items()):
        if name != primary and pod["spec"].get("nodeName") != CONTROL_PLANE and _ready(pod):
            return name
    raise ValueError(f"no ready standby on a worker to take over from {primary}")


def not_streaming(names, primary, streaming):
    """The instances in `names`, but the primary, absent from STREAMING_QUERY's output."""
    up = set(streaming.split())
    return sorted(n for n in names if n != primary and n not in up)


def placement(pods, primary):
    """Before S4 and S5, with the primary on a worker: the standby on the other worker, lost
    with it, and the survivor on the control plane."""
    nodes = {p["metadata"]["name"]: p["spec"].get("nodeName") for p in pods["items"]}
    if nodes.get(primary) in (None, CONTROL_PLANE):
        raise ValueError(f"{primary}, the primary, is not on a worker")
    if len(set(nodes.values())) != len(nodes):
        raise ValueError(f"the instances do not each have a node of their own: {nodes}")
    standbys = sorted(n for n in nodes if n != primary and nodes[n] != CONTROL_PLANE)
    survivors = sorted(n for n in nodes if nodes[n] == CONTROL_PLANE)
    if len(standbys) != 1 or len(survivors) != 1:
        raise ValueError(f"want one standby on the other worker and one on the control "
                         f"plane: {nodes}")
    return {"standby": standbys[0], "standby_node": nodes[standbys[0]],
            "survivor": survivors[0]}


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
