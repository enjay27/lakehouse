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
          each failure N times: waits for a healthy cluster (before S3, with the primary on a
          worker), starts a probe pod, takes 60 s of writes, injects the failure while polling
          the cluster each second, waits until both of the probe's modes write again for 10 s
          (at most RUN_TIMEOUT), then reads the probe's table on the primary once every
          instance is back. Each run's record goes to DIR/<run>.json; prints the table.
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


def psql(env, sql, database="postgres"):
    """`sql` on the primary, as postgres through its local socket."""
    return must(kubectl(env["NS_CATALOG_POLARIS"], "exec", primary_of(env), "-c", "postgres",
                        "--", "psql", "-v", "ON_ERROR_STOP=1", "-d", database, "-At", "-F",
                        ",", "-c", sql))


def instances(env):
    return json.loads(must(kubectl(env["NS_CATALOG_POLARIS"], "get", "pods", "-l",
                                   f"cnpg.io/cluster={release.POLARIS_PG},"
                                   "cnpg.io/podRole=instance", "-o", "json")))


def c1_applied(env):
    """C1's settings on the running objects (ADR-016): every instance's readiness probe, and
    the primary's synchronous standbys."""
    want = setups.C1_CLUSTER["probes"]["readiness"]["periodSeconds"]
    for pod in instances(env)["items"]:
        probe = next(c for c in pod["spec"]["containers"] if c["name"] == "postgres")
        if probe.get("readinessProbe", {}).get("periodSeconds") != want:
            return False
    return psql(env, "SHOW synchronous_standby_names").strip().startswith("ANY 1 ")


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
        while not c1_applied(env):
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


def place_primary(env):
    """Before S3: the primary onto a worker, as `kubectl cnpg promote` would move it."""
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
    if scenario == "S3":
        place_primary(env)
    primary = primary_of(env)
    pod = json.loads(must(kubectl(ns, "get", "pod", primary, "-o", "json")))
    node = pod["spec"]["nodeName"]
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
    t0 = now_ms()
    print(f"{run_id}: {scenario} on {primary} ({node}) at {t0}", flush=True)
    must(setups.inject_command(scenario, ns, primary, node))
    try:
        if scenario == "S3":
            resume = threading.Timer(node_down_s, call, [setups.resume_command(node)])
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
        if resume is not None:
            # The node is never left paused: unpausing one already unpaused only fails.
            resume.cancel()
            call(setups.resume_command(node))
        poller.stopping.set()
        poller.join()
    text = probe_logs(ns, name)
    must(kubectl(ns, "delete", "pod", name, "--wait=false"))
    settle(env)
    rows = [line.split(",") for line in
            psql(env, setups.rows_query(run_id), setups.PROBE_DATABASE).split()]
    since = dt.datetime.fromtimestamp(t0 / 1000, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    code, operator_log = call(kubectl(env["NS_CNPG_SYSTEM"], "logs", "-l",
                                      release.OPERATOR_LABEL, "--tail=-1",
                                      f"--since-time={since}"))
    return {"setup": setup, "scenario": scenario, "run": n, "run_id": run_id, "t0": t0,
            "old_primary": primary, "node": node,
            "node_down_s": node_down_s if scenario == "S3" else None,
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
    print(results.markdown(results.aggregate(summaries)))


def report(directory):
    summaries = []
    for path in sorted(glob.glob(os.path.join(directory, "**", "*.json"), recursive=True)):
        with open(path, encoding="utf-8") as f:
            summaries.append(results.summarize(json.load(f)))
    print(results.markdown(results.aggregate(summaries)))


def main(argv):
    parser = argparse.ArgumentParser(prog="driver.py")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("up")
    p.add_argument("--grace", type=int)
    p = sub.add_parser("install")
    p.add_argument("setup", choices=setups.SETUPS)
    p = sub.add_parser("run")
    p.add_argument("setup", choices=setups.SETUPS)
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
