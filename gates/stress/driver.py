"""The stress run's driver (ADR-053 §2, §4; ADR-054): on the gate's kind cluster with a stress
install (`gate.py up`, `secrets --direct`, `batch-image`, `standins --direct`,
`install --fresh`), it seeds the tables, runs k6 step by step, and checks the result. Every
file it keeps goes under OUT, which stress.yml uploads.

  seed OUT        applies the ConfigMap `stress` (seed.py, smoke.py and its hashed
                  requirements, scenario.js, config.json) in Polaris's namespace; runs seed.py
                  in a short-lived pod on the pinned Python image (the check label, root's
                  credentials, the smoke tests' S3 key); puts its tables into the ConfigMap
                  as tables.json; records the restarts of Polaris's and polaris-pg's
                  containers, and polaris-pg's primary and the time; starts the long-lived
                  k6 pod (pinned, the check label, the CPU
                  limit in config.json) and waits until it is ready.
  ramp PROFILE OUT
                  runs PROFILE's steps (config.json), one k6 run per step at one rate,
                  until a step fails. Around each step it reads every node's cAdvisor
                  through the API server (the CPU of k6, OPA, SeaweedFS, Polaris and
                  polaris-pg, and how often the three limited ones were throttled) and
                  Polaris's /q/metrics on 8182 from the k6 pod. A step passes when k6
                  dropped at most 5% of the requests offered, at most 0.1% failed (409s on
                  the hot set not counted) and each kind's p99 is within its cap. The knee is
                  the last step that passed; a knee decided while k6, OPA or SeaweedFS was
                  throttled over the max is that component's, and the profile is not a
                  measurement of Polaris.
  hold OUT        the mixed profile at 70% of its knee for 10 minutes; skipped, and said so,
                  when the mixed profile did not run or found no knee.
  verify OUT      fails the run on a correctness fault only (ADR-054 §2): a 5xx or any other
                  failed request, a 409 on the hot set aside, in a step at or below the knee
                  or in the hold (the step past the knee is where the load fails requests, so
                  it is left out); a commit answered 200 missing from its table's snapshots,
                  a snapshot the scenario never had answered 200 nor left unknown, or one
                  listed twice (seed.py verify loads every table); a restart or replacement
                  of a Polaris or polaris-pg pod. It also records, without judging, whether
                  polaris-pg's primary changed since the seed and how often its instances'
                  readiness probe failed: CNPG demotes a primary inside its container, so a
                  failover under load restarts no container (ADR-047 §8).
  report OUT [BASELINE]
                  writes summary.json and summary.md (printed too) from what OUT holds: each
                  profile's knee and steps, the hold's percentiles, verify's result and, with
                  BASELINE (an earlier run's OUT), each knee's and the hold's p99's change.
                  Speed is reported, never judged.

Every kubectl call names the gate's context, and a namespace where the object has one. Nothing
here reaches any cluster but the kind cluster (ADR-017).

Usage: driver.py seed OUT | ramp PROFILE OUT | hold OUT | verify OUT | report OUT [BASELINE]
"""

import datetime as dt
import glob
import json
import math
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
GATES = os.path.dirname(HERE)
ROOT = os.path.dirname(GATES)
sys.path.insert(0, GATES)
import gate  # noqa: E402
import network  # noqa: E402

release = gate.release

CONFIG = os.path.join(HERE, "config.json")
KINDS = ("token", "config", "list_namespaces", "list_tables", "head_table", "conditional_load",
         "full_load", "commit", "commit_hot")
CLASSES = ("2xx", "304", "409", "4xx", "5xx", "error")
FAILED = ("4xx", "5xx", "error")
K6_POD, CONFIGMAP = "stress-k6", "stress"
CATALOG = "gate_stress_catalog"
POLARIS_URL = f"http://{release.POLARIS}:{gate.POLARIS_API_PORT}"
# The containers cAdvisor is read for, by (namespace variable, container name); the first three
# run under a CPU limit, so their throttling decides whether a step measured Polaris.
CONTAINERS = {"k6": ("NS_CATALOG_POLARIS", K6_POD), "opa": ("NS_STANDINS", "opa"),
              "seaweedfs": ("NS_STANDINS", "seaweedfs"),
              "polaris": ("NS_CATALOG_POLARIS", release.POLARIS),
              "polaris-pg": ("NS_CATALOG_POLARIS", "postgres")}
LIMITED = ("k6", "opa", "seaweedfs")
CPU_SERIES = {"container_cpu_usage_seconds_total": "usage",
              "container_cpu_cfs_periods_total": "periods",
              "container_cpu_cfs_throttled_periods_total": "throttled"}
PG_POD_LABELS = f"cnpg.io/cluster={release.POLARIS_PG},cnpg.io/podRole=instance"
COMMIT_RE = re.compile(r'msg="C (ok|conflict|unknown|failed) (\S+) (\d+)"')
SERIES_RE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(.*)\})?\s+(\S+)(?:\s+\S+)?$")
LABEL_RE = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"')
RESULT = "RESULT "


def load_config(path=CONFIG):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def profiles(config):
    return sorted(p for p in config["profiles"] if not p.startswith("_"))


def rates(config, profile):
    if profile.startswith("_") or profile not in config["profiles"]:
        raise ValueError(f"unknown profile {profile}: one of {profiles(config)}")
    p = config["profiles"][profile]
    return [p["start"] + i * p["step"] for i in range(p["max_steps"])]


def _num(value):
    return f"{round(value, 1):g}"


def step_result(summary, config):
    """Whether one k6 run passed ADR-053 §4's three rules, with its counts."""
    rules, requests = config["pass"], summary["requests"]
    total = sum(sum(c.values()) for c in requests.values())
    server = sum(c.get("5xx", 0) for c in requests.values())
    exhausted = summary.get("commits", {}).get("commit", {}).get("conflict", 0)
    errors = sum(c.get(k, 0) for c in requests.values() for k in FAILED) + exhausted
    answered = 1 - summary["dropped"] / summary["offered"] if summary["offered"] else 0.0
    error_rate = errors / total if total else 0.0
    p99 = {kind: lat["p99"] for kind, lat in summary["latency_ms"].items()}
    reasons = []
    if not total:
        reasons.append("no request was sent")
    if answered < rules["answered_min"]:
        reasons.append(f"answered {answered:.1%} < {rules['answered_min']:.0%}")
    if error_rate > rules["error_max"]:
        reasons.append(f"errors {error_rate:.2%} > {rules['error_max']:.1%}")
    for kind, cap in sorted(rules["p99_ms"].items()):
        if kind in p99 and p99[kind] > cap:
            reasons.append(f"{kind} p99 {_num(p99[kind])} ms > {cap} ms")
    return {"rate": summary["rate"], "passed": not reasons, "reasons": reasons,
            "answered": answered, "error_rate": error_rate, "errors": errors,
            "server_errors": server, "requests": total, "dropped": summary["dropped"],
            "p99_ms": p99, "latency_ms": summary["latency_ms"],
            "commits": summary.get("commits", {})}


def knee(steps):
    """The last step that passed, the step that decided it, and whether a step failed."""
    for i, step in enumerate(steps):
        if not step["passed"]:
            return {"knee": steps[i - 1]["rate"] if i else 0, "deciding": i, "reached": True}
    return {"knee": steps[-1]["rate"] if steps else 0,
            "deciding": len(steps) - 1 if steps else None, "reached": False}


def verdict(steps):
    k = knee(steps)
    limited = list(steps[k["deciding"]].get("throttled", [])) if steps else []
    return {**k, "limited_by": limited, "valid": bool(steps) and not limited}


def _unescape(value):
    return re.sub(r"\\(.)", lambda m: "\n" if m.group(1) == "n" else m.group(1), value)


def parse_metrics(text):
    """Prometheus text: {(name, ((label, value), ...)): value}."""
    series = {}
    for line in text.splitlines():
        match = SERIES_RE.match(line.strip())
        if not match or line.startswith("#"):
            continue
        name, labels, value = match.groups()
        key = tuple(sorted((k, _unescape(v)) for k, v in LABEL_RE.findall(labels or "")))
        try:
            series[(name, key)] = float(value)
        except ValueError:
            continue
    return series


def container_cpu(texts, targets):
    """CPU-seconds, CFS periods and throttled periods per target, summed over its pods, from
    every node's cAdvisor text; targets maps a name to (namespace, container)."""
    by_container = {v: k for k, v in targets.items()}
    cpu = {name: {"usage": 0, "periods": 0, "throttled": 0} for name in targets}
    for text in texts:
        for (name, labels), value in parse_metrics(text).items():
            labels = dict(labels)
            target = by_container.get((labels.get("namespace"), labels.get("container")))
            if name in CPU_SERIES and target:
                cpu[target][CPU_SERIES[name]] += value
    return cpu


def throttle(before, after, maximum):
    """The share of CFS periods each limited container was throttled in between, and those
    over `maximum`."""
    ratios = {}
    for name in LIMITED:
        periods = after[name]["periods"] - before[name]["periods"]
        throttled = after[name]["throttled"] - before[name]["throttled"]
        ratios[name] = round(throttled / periods, 4) if periods > 0 else 0.0
    return ratios, [name for name in LIMITED if ratios[name] > maximum]


def server_requests(before, after):
    """Polaris's own count and mean duration per method, endpoint and status between two reads
    of every pod's /q/metrics."""
    def totals(texts):
        sums = {}
        for text in texts:
            for (name, labels), value in parse_metrics(text).items():
                if name.startswith("http_server_requests_seconds_"):
                    labels = dict(labels)
                    key = f"{labels.get('method')} {labels.get('uri')} {labels.get('status')}"
                    part = name.rsplit("_", 1)[1]
                    sums.setdefault(key, {"count": 0, "sum": 0})
                    if part in ("count", "sum"):
                        sums[key][part] += value
        return sums
    start, end = totals(before), totals(after)
    delta = {}
    for key, v in end.items():
        count = v["count"] - start.get(key, {}).get("count", 0)
        if count > 0:
            seconds = v["sum"] - start.get(key, {}).get("sum", 0)
            delta[key] = {"count": int(count), "mean_ms": round(seconds / count * 1000, 1)}
    return delta


def commit_records(text):
    """(outcome, table, snapshot id) for each commit the scenario logged."""
    return [m.groups() for m in COMMIT_RE.finditer(text)]


def accounting(records, found):
    """ADR-054 §2's commit checks against seed.py verify's tables."""
    ok, unknown = {}, {}
    for outcome, table, snapshot in records:
        if outcome == "ok":
            ok.setdefault(table, set()).add(snapshot)
        elif outcome == "unknown":
            unknown.setdefault(table, set()).add(snapshot)
    problems = []
    for table in sorted(set(found) | set(ok)):
        if table not in found:
            problems.append(f"{table}: not loaded after the run")
            continue
        new = set(found[table]["new"])
        for snapshot in sorted(ok.get(table, set()) - new):
            problems.append(f"{table}: commit {snapshot} answered 200 but is missing from its "
                            "snapshots")
        for snapshot in sorted(new - ok.get(table, set()) - unknown.get(table, set())):
            problems.append(f"{table}: snapshot {snapshot} was not answered 200 nor left "
                            "unknown")
        for snapshot in found[table]["duplicates"]:
            problems.append(f"{table}: snapshot {snapshot} is listed twice")
    tally = {"ok": sum(len(s) for s in ok.values()),
             "unknown": sum(len(s) for s in unknown.values()),
             "found": sum(len(f["new"]) for f in found.values())}
    return problems, tally


def correctness(by_profile, hold):
    """Failed requests at or below each knee and in the hold."""
    problems = []
    for profile, steps in sorted(by_profile.items()):
        k = knee(steps)
        for i, step in enumerate(steps):
            if i == k["deciding"] and not step["passed"]:
                continue
            if step["errors"] or step["server_errors"]:
                problems.append(f"{profile} at {step['rate']} req/s: {step['errors']} failed "
                                f"request(s), {step['server_errors']} of them 5xx")
    if hold and (hold.get("errors") or hold.get("server_errors")):
        problems.append(f"hold: {hold['errors']} failed request(s), {hold['server_errors']} "
                        "of them 5xx")
    return problems


def restarts(pods):
    return {p["metadata"]["name"]: sum(c.get("restartCount", 0)
                                       for c in p.get("status", {}).get("containerStatuses", []))
            for p in pods.get("items", [])}


def restart_problems(before, after):
    problems = []
    gone, new = sorted(set(before) - set(after)), sorted(set(after) - set(before))
    if gone or new:
        parts = ([f"{', '.join(gone)} is gone"] if gone else []) + \
                ([f"{', '.join(new)} is new"] if new else [])
        problems.append(f"{', '.join(parts)}: a pod was replaced")
    for pod in sorted(set(before) & set(after)):
        if after[pod] > before[pod]:
            problems.append(f"{pod} restarted {after[pod] - before[pod]} time(s) during the run")
    return problems


# polaris-pg's instance pods, not its Job pods (`polaris-pg-1-initdb-...`) nor Polaris's.
PG_INSTANCE = re.compile(rf"^{re.escape(release.POLARIS_PG)}-\d+$")


def pg_before(cluster, since):
    return {"primary": cluster.get("status", {}).get("currentPrimary"), "since": since}


def pg_events_command(env):
    return _kubectl(env) + ["get", "events",
                            "--field-selector=involvedObject.kind=Pod,reason=Unhealthy",
                            "-o", "json"]


def _when(text):
    return dt.datetime.fromisoformat(text.replace("Z", "+00:00")) if text else None


def pg_watch(before, cluster, events):
    """polaris-pg's primary at the seed and now, and its instances' readiness probe failures
    last seen since the seed, by pod."""
    since, failures = _when(before["since"]), {}
    for e in events.get("items", []):
        pod = e.get("involvedObject", {}).get("name", "")
        last = _when(e.get("lastTimestamp") or e.get("eventTime"))
        if (e.get("reason") == "Unhealthy" and PG_INSTANCE.match(pod)
                and str(e.get("message", "")).startswith("Readiness probe failed")
                and last is not None and last >= since):
            failures[pod] = failures.get(pod, 0) + (e.get("count") or 1)
    return {"primary_before": before["primary"],
            "primary_after": cluster.get("status", {}).get("currentPrimary"),
            "readiness_failures": dict(sorted(failures.items()))}


def pg_line(watch):
    if not watch:
        return "polaris-pg: not recorded"
    before, after = watch["primary_before"], watch["primary_after"]
    primary = (f"{before} primary throughout" if before == after
               else f"the primary changed from {before} to {after}")
    failures = watch["readiness_failures"]
    probe = (f"{sum(failures.values())} readiness probe failure(s): "
             + ", ".join(f"{pod} {n}" for pod, n in failures.items())
             if failures else "no readiness probe failure")
    return f"polaris-pg: {primary}; {probe}"


def _pg_cluster(env):
    return json.loads(gate.output(_kubectl(env) + ["get", f"cluster/{release.POLARIS_PG}",
                                                   "-o", "json"]))


def hold_rate(config, mixed_knee):
    if not mixed_knee:
        return None
    return math.floor(mixed_knee * config["hold"]["fraction"] + 1e-9)


def compare(now, base):
    """Each knee and the hold's p99 per kind against a baseline run's summary."""
    def change(value, old):
        return f" ({(value - old) / old:+.1%})" if old else ""
    lines = []
    for profile in sorted(now.get("profiles", {})):
        value = now["profiles"][profile]["knee"]
        old = base.get("profiles", {}).get(profile)
        if old is None:
            lines.append(f"{profile} knee: {value} req/s, not in the baseline")
        else:
            lines.append(f"{profile} knee: {value} req/s against {old['knee']}"
                         f"{change(value, old['knee'])}")
    base_hold = (base.get("hold") or {}).get("latency_ms", {})
    for kind, lat in sorted(((now.get("hold") or {}).get("latency_ms") or {}).items()):
        if kind in base_hold:
            old = base_hold[kind]["p99"]
            lines.append(f"hold {kind} p99: {_num(lat['p99'])} ms against {_num(old)}"
                         f"{change(lat['p99'], old)}")
    return lines


def _kubectl(env, var="NS_CATALOG_POLARIS"):
    return ["kubectl", "--context", gate.CONTEXT, "-n", env[var]]


def _secret_env(pairs):
    return [{"name": var, "valueFrom": {"secretKeyRef": {"name": secret, "key": key}}}
            for var, secret, key in pairs]


ROOT_ENV = (("ROOT_CLIENT_ID", release.POLARIS_ROOT_SECRET, "clientId"),
            ("ROOT_CLIENT_SECRET", release.POLARIS_ROOT_SECRET, "clientSecret"))
VOLUME = [{"name": "stress", "configMap": {"name": CONFIGMAP}}]
MOUNT = [{"name": "stress", "mountPath": "/stress", "readOnly": True}]


def configmap(env, tables):
    files = {"seed.py": os.path.join(HERE, "seed.py"),
             "smoke.py": os.path.join(GATES, "iceberg", "smoke.py"),
             "requirements.txt": os.path.join(GATES, "iceberg", "requirements.txt"),
             "scenario.js": os.path.join(HERE, "scenario.js"), "config.json": CONFIG}
    data = {}
    for name, path in files.items():
        with open(path, encoding="utf-8") as f:
            data[name] = f.read()
    data["tables.json"] = json.dumps(tables, separators=(",", ":"))
    return {"apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": CONFIGMAP, "namespace": env["NS_CATALOG_POLARIS"]},
            "data": data}


def seed_pod(env, image, name, mode):
    """The pod that runs seed.py `mode` (seed or verify), its output on stdout."""
    settings = [{"name": "PYTHONUNBUFFERED", "value": "1"},
                {"name": "PIP_DISABLE_PIP_VERSION_CHECK", "value": "1"},
                {"name": "PIP_NO_CACHE_DIR", "value": "1"},
                {"name": "PIP_ROOT_USER_ACTION", "value": "ignore"}]
    creds = ROOT_ENV + (("S3_ACCESS_KEY_ID", "smoke-s3", "accessKeyId"),
                        ("S3_SECRET_ACCESS_KEY", "smoke-s3", "secretAccessKey"))
    if mode == "seed":
        script = ("pip install --quiet --require-hashes --only-binary :all: "
                  "-r /stress/requirements.txt && exec python3 /stress/seed.py \"$@\"")
        args = [POLARIS_URL, release.POLARIS_REALM, env["S3_ENDPOINT"], env["S3_REGION"],
                env["POLARIS_WAREHOUSE_BUCKET"]]
    else:
        script, args, creds = "exec python3 /stress/seed.py \"$@\"", \
            [POLARIS_URL, release.POLARIS_REALM], ROOT_ENV
    overrides = {"spec": {
        "containers": [{"name": name, "image": image, "env": _secret_env(creds) + settings,
                        "volumeMounts": MOUNT}],
        "volumes": VOLUME}}
    return _kubectl(env) + [
        "run", name, gate.CHECK_POD_LABEL, f"--image={image}", "--restart=Never", "--rm",
        "--attach", "--quiet", "--pod-running-timeout=3m", "--override-type=strategic",
        "--overrides", json.dumps(overrides), "--command", "--", "sh", "-c", script, "seed",
        mode, *args]


def k6_pod(env, image, config):
    return {"apiVersion": "v1", "kind": "Pod",
            "metadata": {"name": K6_POD, "namespace": env["NS_CATALOG_POLARIS"],
                         "labels": {network.CHECK_LABEL: "true",
                                    "app.kubernetes.io/name": K6_POD}},
            "spec": {"restartPolicy": "Never", "terminationGracePeriodSeconds": 0,
                     "containers": [{"name": K6_POD, "image": image,
                                     "command": ["sleep", "infinity"],
                                     "resources": {"limits": {"cpu": config["k6"]["cpu"]}},
                                     "env": _secret_env(ROOT_ENV), "volumeMounts": MOUNT}],
                     "volumes": VOLUME}}


def k6_run(env, name, profile, rate, seconds):
    settings = {"PROFILE": profile, "RATE": rate, "SECONDS": seconds, "NAME": name,
                "POLARIS": POLARIS_URL, "REALM": release.POLARIS_REALM, "CATALOG": CATALOG}
    argv = _kubectl(env) + ["exec", K6_POD, "--", "k6", "run", "--quiet", "--no-color",
                            f"--console-output=/tmp/{name}.log"]
    for key, value in settings.items():
        argv += ["-e", f"{key}={value}"]
    return argv + ["/stress/scenario.js"]


def fetch(env, pod, path):
    return _kubectl(env) + ["exec", pod, "--", "cat", path]


def _write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, sort_keys=True)


def _read(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _apply(env, manifest, what):
    argv = ["kubectl", "--context", gate.CONTEXT, "-n", env["NS_CATALOG_POLARIS"], "apply",
            "--server-side", "--force-conflicts", "--field-manager=gate", "-f", "-"]
    print("+ " + " ".join(argv) + f"  ({what} on stdin)", flush=True)
    subprocess.run(argv, input=json.dumps(manifest), text=True, check=True)


def pod_result(argv):
    """Runs a seed.py pod, printing its lines as they come; returns its RESULT, or None."""
    print("+ " + " ".join(argv[:7]) + " ...", flush=True)
    result = None
    with subprocess.Popen(argv, stdout=subprocess.PIPE, text=True) as pod:
        for line in pod.stdout:
            if line.startswith(RESULT):
                result = json.loads(line[len(RESULT):])
            else:
                print(line, end="", flush=True)
    return result if pod.returncode == 0 else None


def _restarts(env):
    pods = {}
    for selector in (gate.POLARIS_POD_LABELS, PG_POD_LABELS):
        pods.update(restarts(json.loads(gate.output(
            _kubectl(env) + ["get", "pods", "-l", selector, "-o", "json"]))))
    return pods


def seed(env, pins, config, out):
    _apply(env, configmap(env, {"tables": []}), f"the ConfigMap {CONFIGMAP}")
    result = pod_result(seed_pod(env, gate.smoke_python_image(pins),
                                 f"stress-seed-{os.getpid()}", "seed"))
    if result is None:
        print("The seed failed: see its lines above", file=sys.stderr)
        return 1
    _write(os.path.join(out, "tables.json"), result)
    _apply(env, configmap(env, result), f"the ConfigMap {CONFIGMAP} with the seed's tables")
    _write(os.path.join(out, "restarts.json"), _restarts(env))
    since = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _write(os.path.join(out, "polaris_pg.json"), pg_before(_pg_cluster(env), since))
    _apply(env, k6_pod(env, pins["load_images"]["k6"]["image"], config), f"pod/{K6_POD}")
    gate.run(_kubectl(env) + ["wait", "--for=condition=Ready", f"pod/{K6_POD}",
                              "--timeout=180s"])
    return 0


def sample(env, nodes, polaris_ips):
    texts = [gate.output(["kubectl", "--context", gate.CONTEXT, "get", "--raw",
                          f"/api/v1/nodes/{node}/proxy/metrics/cadvisor"]) for node in nodes]
    targets = {name: (env[var], container) for name, (var, container) in CONTAINERS.items()}
    metrics = [gate.output(_kubectl(env) + [
        "exec", K6_POD, "--", "wget", "-qO-", "-T", "10",
        f"http://{ip}:{gate.POLARIS_MGMT_PORT}/q/metrics"]) for ip in polaris_ips]
    return {"cpu": container_cpu(texts, targets), "polaris": metrics}


def run_step(env, config, out, name, profile, rate, seconds, nodes, polaris_ips):
    print(f"{name}: {profile} at {rate} req/s for {seconds} s", flush=True)
    before = sample(env, nodes, polaris_ips)
    if subprocess.run(k6_run(env, name, profile, rate, seconds), check=False).returncode:
        sys.exit(f"{name}: k6 failed, see above")
    after = sample(env, nodes, polaris_ips)
    summary = json.loads(gate.output(fetch(env, K6_POD, f"/tmp/{name}.json")))
    console = gate.output(fetch(env, K6_POD, f"/tmp/{name}.log"))
    os.makedirs(os.path.join(out, "console"), exist_ok=True)
    with open(os.path.join(out, "console", f"{name}.log"), "w", encoding="utf-8") as f:
        f.write(console)
    result = step_result(summary, config)
    ratios, over = throttle(before["cpu"], after["cpu"], config["throttle_max"])
    result.update({"name": name, "seconds": seconds, "throttle": ratios, "throttled": over,
                   "cpu_seconds": {k: round(after["cpu"][k]["usage"] - before["cpu"][k]["usage"],
                                            2) for k in CONTAINERS},
                   "server": server_requests(before["polaris"], after["polaris"])})
    print(f"{name}: {'passed' if result['passed'] else 'failed'}; answered "
          f"{result['answered']:.1%}, errors {result['errors']} of {result['requests']}, "
          f"p99 {', '.join(f'{k} {_num(v)} ms' for k, v in sorted(result['p99_ms'].items()))}"
          f"{'; throttled: ' + ', '.join(over) if over else ''}"
          f"{'; ' + '; '.join(result['reasons']) if result['reasons'] else ''}", flush=True)
    return result


def _targets(env):
    nodes = [n.split("/", 1)[1] for n in gate.output(
        ["kubectl", "--context", gate.CONTEXT, "get", "nodes", "-o", "name"]).split()]
    pods = gate._running(json.loads(gate.output(
        _kubectl(env) + ["get", "pods", "-l", gate.POLARIS_POD_LABELS, "-o", "json"])))
    return nodes, [p["status"]["podIP"] for p in pods]


def ramp(env, config, profile, out):
    nodes, ips = _targets(env)
    steps = []
    for i, rate in enumerate(rates(config, profile)):
        name = f"{profile}-{i:02d}"
        steps.append(run_step(env, config, out, name, profile, rate, config["step_seconds"],
                              nodes, ips))
        _write(os.path.join(out, "steps", f"{name}.json"), steps[-1])
        if not steps[-1]["passed"]:
            break
    result = {"steps": steps, **verdict(steps)}
    _write(os.path.join(out, "profiles", f"{profile}.json"), result)
    print(f"{profile}: knee {result['knee']} req/s"
          f"{'' if result['reached'] else ' (not reached: the ramp ended first)'}"
          f"{'; limited by ' + ', '.join(result['limited_by']) if result['limited_by'] else ''}",
          flush=True)
    return 0


def hold(env, config, out):
    h = config["hold"]
    mixed = _read(os.path.join(out, "profiles", f"{h['profile']}.json"))
    rate = hold_rate(config, mixed and mixed["knee"])
    if rate is None:
        print(f"No hold: the {h['profile']} profile has no knee in {out}", flush=True)
        _write(os.path.join(out, "hold.json"), {"skipped": f"no {h['profile']} knee"})
        return 0
    nodes, ips = _targets(env)
    result = run_step(env, config, out, "hold", h["profile"], rate, h["seconds"], nodes, ips)
    _write(os.path.join(out, "hold.json"), result)
    return 0


def verify(env, pins, out):
    seeded = _read(os.path.join(out, "tables.json"))
    if seeded is None:
        print(f"No seed in {out}: nothing to verify", file=sys.stderr)
        return 1
    found = pod_result(seed_pod(env, gate.smoke_python_image(pins),
                                f"stress-verify-{os.getpid()}", "verify"))
    problems, tally = [], {}
    if found is None:
        problems.append("seed.py verify failed: see its lines above")
    else:
        records = []
        for path in sorted(glob.glob(os.path.join(out, "console", "*.log"))):
            with open(path, encoding="utf-8") as f:
                records += commit_records(f.read())
        lost, tally = accounting(records, found["tables"])
        problems += lost
    by_profile = {os.path.basename(p)[:-5]: _read(p)["steps"]
                  for p in glob.glob(os.path.join(out, "profiles", "*.json"))}
    held = _read(os.path.join(out, "hold.json"))
    problems += correctness(by_profile, held if held and "skipped" not in held else None)
    problems += restart_problems(_read(os.path.join(out, "restarts.json"), {}), _restarts(env))
    before = _read(os.path.join(out, "polaris_pg.json"))
    watch = before and pg_watch(before, _pg_cluster(env),
                                json.loads(gate.output(pg_events_command(env))))
    _write(os.path.join(out, "verify.json"), {"problems": problems, "commits": tally,
                                              "polaris_pg": watch})
    print(pg_line(watch), flush=True)
    print(f"Commits: {tally.get('ok', 0)} answered 200, {tally.get('unknown', 0)} unknown, "
          f"{tally.get('found', 0)} new snapshots found", flush=True)
    for line in problems or ["No correctness fault."]:
        print(line, flush=True)
    return 1 if problems else 0


def polaris_version():
    with open(release.POLARIS_VALUES, encoding="utf-8") as f:
        match = re.search(r'^\s*tag:\s*"?([^"\s@]+)', f.read(), re.M)
    return match.group(1) if match else "unknown"


def report(out, pins, baseline=None):
    by_profile = {os.path.basename(p)[:-5]: _read(p)
                  for p in sorted(glob.glob(os.path.join(out, "profiles", "*.json")))}
    commit = subprocess.run(["git", "-C", ROOT, "rev-parse", "HEAD"], capture_output=True,
                            text=True, check=False).stdout.strip()
    summary = {
        "commit": commit, "polaris": polaris_version(), "k6": pins["load_images"]["k6"]["image"],
        "profiles": {name: {k: p[k] for k in ("knee", "reached", "limited_by", "valid")}
                     | {"steps": [{k: s[k] for k in ("rate", "passed", "answered", "error_rate",
                                                     "p99_ms", "throttle", "reasons")}
                                  for s in p["steps"]]}
                     for name, p in by_profile.items()},
        "hold": _read(os.path.join(out, "hold.json")),
        "verify": _read(os.path.join(out, "verify.json")),
    }
    if baseline:
        summary["comparison"] = compare(summary, _read(os.path.join(baseline, "summary.json"),
                                                       {}))
    _write(os.path.join(out, "summary.json"), summary)
    lines = [f"## Stress run on `{commit[:12]}`, Polaris {summary['polaris']}", "",
             "| Profile | Knee (req/s) | Steps | Limited by |", "|---|---|---|---|"]
    for name, p in summary["profiles"].items():
        lines.append(f"| {name} | {p['knee']}{'' if p['reached'] else ' (not reached)'} | "
                     f"{len(p['steps'])} | {', '.join(p['limited_by']) or 'Polaris'} |")
    held = summary["hold"] or {"skipped": "not run"}
    if "skipped" in held:
        lines += ["", f"Hold: skipped ({held['skipped']})."]
    else:
        lines += ["", f"Hold at {held['rate']} req/s for {held['seconds']} s: answered "
                      f"{held['answered']:.1%}, {held['errors']} failed of {held['requests']}.",
                  "", "| Kind | p50 ms | p95 ms | p99 ms |", "|---|---|---|---|"]
        lines += [f"| {k} | {_num(v['p50'])} | {_num(v['p95'])} | {_num(v['p99'])} |"
                  for k, v in sorted(held["latency_ms"].items())]
    checked = summary["verify"]
    lines += ["", "Correctness: " + ("not verified" if checked is None else
                                     "; ".join(checked["problems"]) or "no fault")]
    if checked is not None:
        lines += ["", pg_line(checked.get("polaris_pg"))]
    if baseline:
        lines += ["", "Against the baseline run:", ""] + [f"- {x}" for x in
                                                         summary["comparison"]]
    text = "\n".join(lines) + "\n"
    with open(os.path.join(out, "summary.md"), "w", encoding="utf-8") as f:
        f.write(text)
    print(text, end="")
    return 0


def main(argv):
    config = load_config()
    args = argv[1:]
    usage = len(args) < 2
    if not usage and args[0] == "ramp":
        usage = len(args) != 3 or args[1] not in profiles(config)
    elif not usage:
        usage = (args[0] not in ("seed", "hold", "verify", "report")
                 or len(args) > (3 if args[0] == "report" else 2))
    if usage:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    command, out = args[0], args[-1] if args[0] == "ramp" else args[1]
    pins = gate.load_pins()
    if command == "report":
        return report(out, pins, args[2] if len(args) == 3 else None)
    env = release.load_env(ROOT, os.environ)
    if command == "seed":
        return seed(env, pins, config, out)
    if command == "ramp":
        return ramp(env, config, args[1], out)
    if command == "hold":
        return hold(env, config, out)
    return verify(env, pins, out)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
