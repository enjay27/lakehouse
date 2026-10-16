"""The failover measurement's driver (ADR-047 §6), on the kind gate only.

  up [--grace SECONDS]
          gates/gate.py's `up` (kind, CNPG, cert-manager, the plugin). With --grace, kind's
          controller manager gets that node grace period, checked on the running pod. Then
          every Deployment of the system namespaces is pinned to the control plane, so that
          S3 takes only an instance's node (ADR-047 §3).
  install SETUP
          after `gate.py secrets` and `gate.py standins`: pins the stand-ins to the control
          plane and makes the backup bucket again (SeaweedFS keeps it in an emptyDir), applies
          the gate's own policies, installs Polaris's network policies and polaris-pg as a
          release's step 1 does (install/release.py), then the setup: C1's `Cluster` patch,
          checked on the running instances, and C2's Pooler. Makes the probe's database, and
          builds the probe's image from gates/failover/probe and the pinned pgjdbc jar, then
          loads it into kind. Adds the setup and the image to STATE, which `up` began with
          the grace period, for `run`.
  run SETUP --out DIR [--scenarios S1,S2,S3] [--runs N] [--node-down SECONDS]
          each failure N times: waits for a healthy cluster (before S3 to S5, with the primary
          on a worker), starts a probe pod, takes 60 s of writes, injects the failure while
          polling the cluster each second, waits until both of the probe's modes write again
          for 10 s (at most RUN_TIMEOUT), then reads the probe's table on the primary once
          every instance is back. S4 and S5, named in --scenarios, pause both workers; S5
          first cuts the survivor off from the primary for CUT_S and checks it fell behind
          (ADR-058 §1). Each run's record goes to DIR/<run>.json; prints the table.
  report DIR
          the table of every record in DIR (results.py).
  matrix [--setups C0,..] [--scenarios S1,..] [--grace-job true|false]
          failover.yml's jobs as a matrix, from its inputs.

Every kubectl call names the gate's context and a namespace. Nothing here reaches any cluster
but the kind cluster gate.py creates (ADR-017).
"""

import argparse
import base64
import datetime as dt
import glob
import json
import os
import subprocess
import sys
import tempfile
import threading
import time

import results
import setups

gate, release, network = setups.gate, setups.release, setups.network

STATE = "/tmp/lakehouse-failover.json"
BASELINE_S = 60
SETTLE_MS = 10_000
RUN_TIMEOUT_S = 900
POLL_S = 1
CHECK_S = 5
SWITCHOVER_TIMEOUT_S = 300
# How long a standby has to stream from the primary again after a run before it is rebuilt.
STREAM_TIMEOUT_S = 120


def now_ms():
    return int(time.time() * 1000)


def call(argv, stdin=None):
    """(exit code, output) as install/release.py's call: stderr then stdout on a failure."""
    return release.call(argv, stdin)


def must(argv, stdin=None):
    code, out = call(argv, stdin)
    if code != 0:
        sys.exit(f"{' '.join(argv[:8])}... failed: {out.strip()}")
    return out


def kubectl(namespace, *args):
    return ["kubectl", "--context", gate.CONTEXT, "-n", namespace, *args]


def env_for_up():
    return {"NS_CNPG_SYSTEM": os.environ.get("NS_CNPG_SYSTEM") or "cnpg-system"}


def pin(namespace):
    """Every Deployment of `namespace` onto the control plane, each waited for."""
    names = must(kubectl(namespace, "get", "deployments", "-o", "name")).split()
    for name in names:
        must(kubectl(namespace, "patch", name, "--type", "merge", "-p",
                     json.dumps(setups.pin_patch())))
    for name in names:
        must(kubectl(namespace, "rollout", "status", name, "--timeout=5m"))
    print(f"{namespace}: {len(names)} Deployments pinned to {setups.CONTROL_PLANE}")


def up(grace_s):
    pins = gate.load_pins()
    with open(gate.KIND_CONFIG, encoding="utf-8") as f:
        config = setups.kind_config(f.read(), grace_s)
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(config)
    try:
        gate.up(pins, kind_config=f.name)
    finally:
        os.unlink(f.name)
    if grace_s is not None:
        pod = f"kube-controller-manager-{setups.CONTROL_PLANE}"
        command = json.loads(must(kubectl("kube-system", "get", "pod", pod, "-o",
                                          "jsonpath={.spec.containers[0].command}")))
        if not setups.grace_applied(command, grace_s):
            sys.exit(f"the controller manager runs without --node-monitor-grace-period="
                     f"{grace_s}s: kind did not apply the patch")
        print(f"The controller manager runs with --node-monitor-grace-period={grace_s}s.")
    for namespace in setups.system_namespaces(env_for_up()):
        pin(namespace)
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump({"grace_s": grace_s}, f)


def primary_of(env):
    ns = env["NS_CATALOG_POLARIS"]
    cluster = json.loads(must(kubectl(ns, "get", f"cluster/{release.POLARIS_PG}", "-o",
                                      "json")))
    return cluster["status"]["currentPrimary"]


def psql_command(env, sql, database="postgres", pod=None):
    """`sql` on the primary, or on the instance `pod`, as postgres through its local
    socket."""
    return kubectl(env["NS_CATALOG_POLARIS"], "exec", pod or primary_of(env), "-c", "postgres",
                   "--", "psql", "-v", "ON_ERROR_STOP=1", "-d", database, "-At", "-F", ",",
                   "-c", sql)


def psql(env, sql, database="postgres"):
    return must(psql_command(env, sql, database))


def instances(env):
    return json.loads(must(kubectl(env["NS_CATALOG_POLARIS"], "get", "pods", "-l",
                                   f"cnpg.io/cluster={release.POLARIS_PG},"
                                   "cnpg.io/podRole=instance", "-o", "json")))


def c1_applied(env, setup="C1"):
    """C1's settings on the running objects (ADR-016): every instance's readiness probe, and
    the primary's synchronous standbys; for C1q also the failover quorum the primary reports.
    The patch restarts the instances, so a primary that does not answer yet is not applied
    yet."""
    want = setups.C1_CLUSTER["probes"]["readiness"]["periodSeconds"]
    for pod in instances(env)["items"]:
        probe = next(c for c in pod["spec"]["containers"] if c["name"] == "postgres")
        if probe.get("readinessProbe", {}).get("periodSeconds") != want:
            return False
    code, out = call(psql_command(env, "SHOW synchronous_standby_names"))
    if code != 0 or not out.strip().startswith("ANY 1 "):
        return False
    sync = setups.cluster_patch(setup)["spec"]["postgresql"]["synchronous"]
    return not sync.get("failoverQuorum") or quorum_applied(env, sync["number"])


def quorum_applied(env, number):
    """CNPG's FailoverQuorum for polaris-pg, which the primary's instance manager updates:
    quorum-based (`any`) over `number` standbys."""
    code, out = call(kubectl(env["NS_CATALOG_POLARIS"], "get",
                             f"failoverquorum/{release.POLARIS_PG}", "-o", "json"))
    if code != 0:
        return False
    status = json.loads(out).get("status", {})
    return (str(status.get("method", "")).lower() == "any"
            and status.get("standbyNumber") == number)


def settle(env):
    problem = release.settle(call, env["KUBE_CONTEXT"], env["NS_CATALOG_POLARIS"])
    if problem:
        sys.exit(problem)


def install(setup):
    setups.check(setup)
    env = release.load_env(gate.ROOT, os.environ)
    ns = env["NS_CATALOG_POLARIS"]
    pin(env["NS_STANDINS"])
    # Pinning moved SeaweedFS, whose buckets live in an emptyDir: make the backup bucket
    # again and check it, as `gate.py standins` does after applying it.
    for argv, stdin in gate.standins_commands(env, "")[2:]:
        must(argv, stdin)
    must(["python3", os.path.join(gate.HERE, "gate.py"), "gate-policies"])
    for step in (release.network_policies, release.polaris_postgres):
        problems, notes = step(env, call)
        if problems:
            sys.exit("\n".join(problems))
        print("\n".join(notes))
    patch = setups.cluster_patch(setup)
    if patch:
        must(kubectl(ns, "patch", f"cluster/{release.POLARIS_PG}", "--type", "merge", "-p",
                     json.dumps(patch)))
        end = time.monotonic() + release.SETTLE_TIMEOUT
        while not c1_applied(env, setup):
            if time.monotonic() > end:
                sys.exit(f"{setup}'s settings did not reach the running instances")
            time.sleep(CHECK_S)
        print(f"{setup}: the Cluster patch runs on every instance.")
    settle(env)
    pooler = setups.pooler(setup, ns)
    if pooler:
        must(kubectl(ns, "apply", "-f", "-"), json.dumps(pooler))
        end = time.monotonic() + 300
        while call(kubectl(ns, "get", f"deployment/{setups.POOLER}"))[0] != 0:
            if time.monotonic() > end:
                sys.exit(f"the Pooler made no Deployment {setups.POOLER}")
            time.sleep(CHECK_S)
        must(kubectl(ns, "rollout", "status", f"deployment/{setups.POOLER}", "--timeout=5m"))
        print(f"{setup}: Pooler {setups.POOLER} ready on {setups.CONTROL_PLANE}.")
    owner = base64.b64decode(must(kubectl(ns, "get", "secret", release.POLARIS_DB_SECRET, "-o",
                                          "jsonpath={.data.username}"))).decode()
    if not psql(env, "SELECT 1 FROM pg_database WHERE datname = "
                     f"'{setups.PROBE_DATABASE}'").strip():
        psql(env, f'CREATE DATABASE {setups.PROBE_DATABASE} OWNER "{owner}"')
    print(f"Database {setups.PROBE_DATABASE}, owned by Polaris's login.")
    image = build_probe()
    with open(STATE, encoding="utf-8") as f:
        state = json.load(f)
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump({**state, "setup": setup, "image": image}, f)


def build_probe():
    pin_ = gate.load_pins()["failover"]["pgjdbc"]
    files = {"postgresql.jar": gate.fetch(pin_["url"], pin_["sha256"])}
    for name in ("Probe.java", "Dockerfile"):
        with open(os.path.join(setups.PROBE_DIR, name), "rb") as f:
            files[name] = f.read()
    image = setups.probe_image(files)
    with tempfile.TemporaryDirectory() as d:
        for name, data in files.items():
            with open(os.path.join(d, name), "wb") as f:
                f.write(data)
        gate.run(["docker", "build", "-t", image, d])
    gate.run(["kind", "load", "docker-image", image, "--name", gate.CLUSTER])
    print(f"Probe image {image}, loaded into kind.")
    return image


class Poller(threading.Thread):
    """The cluster and its `-rw` Service, read each second until stopped."""

    def __init__(self, env):
        super().__init__(daemon=True)
        self.ns = env["NS_CATALOG_POLARIS"]
        self.observations = []
        self.stopping = threading.Event()

    def run(self):
        while not self.stopping.is_set():
            t = now_ms()
            code_c, cluster = call(kubectl(self.ns, "get", f"cluster/{release.POLARIS_PG}",
                                           "-o", "json"))
            code_s, slices = call(kubectl(self.ns, "get", "endpointslices", "-l",
                                          "kubernetes.io/service-name="
                                          f"{release.POLARIS_PG_SERVICE}", "-o", "json"))
            if code_c == 0 and code_s == 0:
                self.observations.append(setups.observation(t, json.loads(cluster),
                                                            json.loads(slices)))
            self.stopping.wait(max(0, POLL_S - (now_ms() - t) / 1000))


def fell_behind(env, standby, survivor):
    """S5, just before the pause: (valid, reason, LSNs). Valid when the survivor has received
    less WAL than the standby `standby` has flushed, so that only the standby paused with the
    primary holds the last confirmed commits (ADR-058 §1)."""
    lsns = {"primary_lsn": None, "standby_lsn": None, "survivor_lsn": None}
    code, out = call(psql_command(env, setups.standby_flush_query(standby)))
    if code != 0:
        return False, f"the primary did not answer: {out.strip()}", lsns
    if not out.strip():
        return False, f"the primary lists no standby {standby}", lsns
    lsns["primary_lsn"], lsns["standby_lsn"] = out.strip().splitlines()[0].split(",")
    if not lsns["standby_lsn"]:
        return False, f"{standby} has reported no flushed LSN", lsns
    code, out = call(psql_command(env, setups.RECEIVED_QUERY, pod=survivor))
    if code != 0:
        return False, f"{survivor} did not answer: {out.strip()}", lsns
    lsns["survivor_lsn"] = out.strip()
    if setups.lsn(lsns["survivor_lsn"]) >= setups.lsn(lsns["standby_lsn"]):
        return False, (f"{survivor} received {lsns['survivor_lsn']}, not behind {standby}'s "
                       f"{lsns['standby_lsn']}"), lsns
    return True, None, lsns


def missing_standbys(env, timeout_s=STREAM_TIMEOUT_S):
    """The standbys not streaming from the primary after `timeout_s` seconds. A readiness
    probe passes on a standby that answers without replicating, so CNPG can call the
    Cluster healthy while one is missing (C1-S5-1, failover 38846398547)."""
    end = time.monotonic() + timeout_s
    while True:
        names = [p["metadata"]["name"] for p in instances(env)["items"]]
        code, out = call(psql_command(env, setups.STREAMING_QUERY))
        missing = setups.not_streaming(names, primary_of(env), out if code == 0 else "")
        if not missing or time.monotonic() > end:
            return missing
        time.sleep(CHECK_S)


def rebuild(env, names):
    """Each standby in `names` deleted with its volume, as gate.py rebuild-replica does, so
    that CNPG clones the primary into a new one; then every standby streams again."""
    ns = env["NS_CATALOG_POLARIS"]
    for name in names:
        for argv in gate.rebuild_commands(ns, name):
            must(argv)
        print(f"{name} did not stream from the primary: deleted with its volume.", flush=True)
    settle(env)
    missing = missing_standbys(env)
    if missing:
        sys.exit(f"{', '.join(missing)} not streaming from the primary after the rebuild")


def place_primary(env):
    """Before S3 to S5: the primary onto a worker, as `kubectl cnpg promote` would move it."""
    ns = env["NS_CATALOG_POLARIS"]
    primary = primary_of(env)
    target = setups.switchover_target(instances(env), primary)
    if target is None:
        return
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    must(kubectl(ns, "patch", f"cluster/{release.POLARIS_PG}", "--subresource=status",
                 "--type", "merge", "-p", json.dumps(setups.switchover_patch(target, stamp))))
    end = time.monotonic() + SWITCHOVER_TIMEOUT_S
    while primary_of(env) != target:
        if time.monotonic() > end:
            sys.exit(f"the switchover to {target} did not complete in {SWITCHOVER_TIMEOUT_S} s")
        time.sleep(CHECK_S)
    print(f"Switched over from {primary} on the control plane to {target}.")
    settle(env)


def probe_logs(ns, name):
    code, out = call(kubectl(ns, "logs", name))
    return out if code == 0 else ""


def one_run(env, state, scenario, n, node_down_s):
    ns, setup = env["NS_CATALOG_POLARIS"], state["setup"]
    settle(env)
    missing = missing_standbys(env)
    if missing:
        rebuild(env, missing)
    if scenario in setups.NODE_LOSSES:
        place_primary(env)
    primary = primary_of(env)
    pods = instances(env)
    by_name = {p["metadata"]["name"]: p for p in pods["items"]}
    node = by_name[primary]["spec"]["nodeName"]
    placed = setups.placement(pods, primary) if scenario in ("S4", "S5") else None
    standby_node = placed["standby_node"] if placed else None
    nodes = [node] + ([standby_node] if placed else [])
    run_id = f"{setup}-{scenario}-{n}"
    name = f"failover-probe-{run_id.lower()}"
    must(kubectl(ns, "apply", "-f", "-"),
         json.dumps(setups.probe_pod(setup, ns, state["image"], name, run_id)))
    must(kubectl(ns, "wait", "--for=condition=Ready", f"pod/{name}", "--timeout=180s"))
    end = time.monotonic() + 120
    while '"event":"ready"' not in probe_logs(ns, name):
        if time.monotonic() > end:
            sys.exit(f"{name} did not start writing: {probe_logs(ns, name)[-2000:]}")
        time.sleep(1)
    time.sleep(BASELINE_S)
    poller = Poller(env)
    poller.start()
    resume = None
    cut, uncut, valid, reason = None, [], True, None
    try:
        if scenario == "S5":
            add, uncut = setups.cut_commands(by_name[placed["survivor"]]["status"]["podIP"],
                                             by_name[primary]["status"]["podIP"])
            cut = {"start": now_ms()}
            for argv in add:
                must(argv)
            print(f"{run_id}: {placed['survivor']} cut off from {primary} at {cut['start']}",
                  flush=True)
            time.sleep(setups.CUT_S)
            valid, reason, lsns = fell_behind(env, placed["standby"], placed["survivor"])
            cut.update(lsns)
            if not valid:
                print(f"{run_id}: not counted: {reason}", flush=True)
        t0 = now_ms()
        print(f"{run_id}: {scenario} on {primary} ({', '.join(nodes)}) at {t0}", flush=True)
        must(setups.inject_command(scenario, ns, primary, node, standby_node))
        if uncut:
            # Both workers are frozen: nothing reaches the survivor until they are unpaused,
            # and the instances must reach each other again once they are.
            for argv in uncut:
                must(argv)
            cut["end"] = now_ms()
            uncut = []
        if scenario in setups.NODE_LOSSES:
            resume = threading.Timer(node_down_s, call, [setups.resume_command(*nodes)])
            resume.start()
        end = time.monotonic() + RUN_TIMEOUT_S
        timed_out = False
        while True:
            time.sleep(CHECK_S)
            records = results.parse_records(probe_logs(ns, name))
            node_back = resume is None or not resume.is_alive()
            if node_back and results.done(records, t0, SETTLE_MS):
                break
            if time.monotonic() > end:
                timed_out = True
                break
    finally:
        # No filter is left in place and no node is left paused: removing a filter already
        # removed, or unpausing a node already unpaused, only fails.
        for argv in uncut:
            call(argv)
        if resume is not None:
            resume.cancel()
        if scenario in setups.NODE_LOSSES:
            call(setups.resume_command(*nodes))
        poller.stopping.set()
        poller.join()
    text = probe_logs(ns, name)
    must(kubectl(ns, "delete", "pod", name, "--wait=false"))
    settle(env)
    rows = [line.split(",") for line in
            psql(env, setups.rows_query(run_id), setups.PROBE_DATABASE).split()]
    not_rejoined = missing_standbys(env)
    if not_rejoined:
        print(f"{run_id}: {', '.join(not_rejoined)} did not stream again", flush=True)
        rebuild(env, not_rejoined)
    since = dt.datetime.fromtimestamp(t0 / 1000, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    code, operator_log = call(kubectl(env["NS_CNPG_SYSTEM"], "logs", "-l",
                                      release.OPERATOR_LABEL, "--tail=-1",
                                      f"--since-time={since}"))
    return {"setup": setup, "scenario": scenario, "run": n, "run_id": run_id, "t0": t0,
            "old_primary": primary, "node": node, "nodes": nodes,
            "survivor": placed["survivor"] if placed else None, "cut": cut,
            "not_rejoined": not_rejoined,
            "valid": valid, "invalid_reason": reason,
            "node_down_s": node_down_s if scenario in setups.NODE_LOSSES else None,
            "timed_out": timed_out, "observations": poller.observations,
            "operator_log": operator_log if code == 0 else "",
            "probe": results.parse_records(text),
            "rows": [[mode, int(seq)] for mode, seq in rows],
            "settings": setups.settings(setup, state.get("grace_s"))}


def run(setup, scenarios, runs, node_down_s, out):
    with open(STATE, encoding="utf-8") as f:
        state = json.load(f)
    if state["setup"] != setup:
        sys.exit(f"the cluster was installed for {state['setup']}, not {setup}")
    env = release.load_env(gate.ROOT, os.environ)
    os.makedirs(out, exist_ok=True)
    summaries = []
    for scenario in scenarios:
        setups.check(setup, scenario)
        for n in range(1, runs + 1):
            record = one_run(env, state, scenario, n, node_down_s)
            with open(os.path.join(out, f"{record['run_id']}.json"), "w",
                      encoding="utf-8") as f:
                json.dump(record, f)
            summary = results.summarize(record)
            summaries.append(summary)
            print(json.dumps({k: summary[k] for k in ("setup", "scenario", "run", "events_s",
                                                      "modes")}), flush=True)
    print(results.markdown(results.aggregate(summaries), results.invalid(summaries),
                           results.not_rejoined(summaries)))


def report(directory):
    summaries = []
    for path in sorted(glob.glob(os.path.join(directory, "**", "*.json"), recursive=True)):
        with open(path, encoding="utf-8") as f:
            summaries.append(results.summarize(json.load(f)))
    print(results.markdown(results.aggregate(summaries), results.invalid(summaries),
                           results.not_rejoined(summaries)))


def main(argv):
    parser = argparse.ArgumentParser(prog="driver.py")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("up")
    p.add_argument("--grace", type=int)
    p = sub.add_parser("install")
    p.add_argument("setup", choices=setups.SETUPS + setups.VARIANTS)
    p = sub.add_parser("run")
    p.add_argument("setup", choices=setups.SETUPS + setups.VARIANTS)
    p.add_argument("--out", required=True)
    p.add_argument("--scenarios", default=",".join(setups.SCENARIOS))
    p.add_argument("--runs", type=int, default=5)
    p.add_argument("--node-down", type=int, default=120)
    p = sub.add_parser("report")
    p.add_argument("directory")
    p = sub.add_parser("matrix")
    p.add_argument("--setups", default="")
    p.add_argument("--scenarios", default="")
    p.add_argument("--grace-job", default="true")
    args = parser.parse_args(argv[1:])
    if args.command == "up":
        up(args.grace)
    elif args.command == "install":
        install(args.setup)
    elif args.command == "run":
        run(args.setup, args.scenarios.split(","), args.runs, args.node_down, args.out)
    elif args.command == "matrix":
        print(json.dumps({"include": setups.matrix(args.setups, args.scenarios,
                                                   args.grace_job == "true")}))
    else:
        report(args.directory)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
