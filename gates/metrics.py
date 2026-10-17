"""Polaris's metrics on the gate (ADR-046 §5). Never part of a release.

- **The stand-in's configuration:** `prometheus_config(contract_text, env)` is the scrape
  contract, docs/contracts/polaris-prometheus-scrape.yaml, with Polaris's namespace filled in
  and a short scrape interval in front. `gate.py standins` hands it to the Prometheus stand-in
  (gates/standins.py), so the file the observability team gets is the one the gate runs.
- **check-metrics:** QUERY runs in a short-lived check pod in Polaris's namespace and asks the
  stand-in's HTTP API, until every target is up or TIMEOUT passes, for each of QUERIES and for
  its targets; it prints one JSON line. `problems(answer, pods)` then says whether every running
  pod of each job is scraped (`up` 1), Polaris counted requests, every instance's collector
  is up, and every Polaris pod's heap pools are those of the collector values-lakehouse.yaml's
  `JAVA_OPTS` names (ADR-061 §3).
"""

import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CONTRACT = os.path.join(ROOT, "docs", "contracts", "polaris-prometheus-scrape.yaml")
VALUES = os.path.join(ROOT, "charts", "polaris", "values-lakehouse.yaml")
PLACEHOLDER = "${NS_CATALOG_POLARIS}"
SCRAPE_INTERVAL = "10s"
PORT = 9090
# Seconds the query waits for every target to be up.
TIMEOUT = 180
# The pods each job of the contract scrapes, as kubectl selects them.
JOBS = {"polaris": "app.kubernetes.io/name=polaris,app.kubernetes.io/instance=polaris",
        "polaris-pg": "cnpg.io/cluster=polaris-pg,cnpg.io/podRole=instance"}
QUERIES = {"up": 'up{job=~"polaris|polaris-pg"}',
           "http": 'http_server_requests_seconds_count{job="polaris"}',
           "cnpg": 'cnpg_collector_up{job="polaris-pg"}',
           "heap": 'jvm_memory_used_bytes{job="polaris",area="heap"}'}
# The heap pools each collector's JVM reports in the `id` label, by the flags that choose it.
# Generational ZGC's were read from Polaris 1.6.0's image with ADR-061's JAVA_OPTS on
# 2026-10-17 (OpenJDK 21.0.11). A collector not listed here is refused, not guessed.
HEAP_POOLS = {("-XX:+UseZGC", "-XX:+ZGenerational"): {"ZGC Young Generation",
                                                      "ZGC Old Generation"}}
_JAVA_OPTS = re.compile(r"^\s*-\s*name:\s*JAVA_OPTS\s*$")
_VALUE = re.compile(r'^\s*value:\s*"([^"]*)"\s*$')

# The series the contract lists (docs/contracts/polaris-metrics.md).
SERIES = ("http_server_requests_seconds_count", "cnpg_collector_up")


def java_opts(path=VALUES):
    """The `JAVA_OPTS` of values-lakehouse.yaml's extraEnv, read line by line without a YAML
    parser: the `value:` line after `- name: JAVA_OPTS`."""
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines()
    for name, value in zip(lines, lines[1:]):
        if _JAVA_OPTS.match(name) and _VALUE.match(value):
            return _VALUE.match(value).group(1)
    raise ValueError(f"{path} sets no JAVA_OPTS")


def heap_pools(opts):
    """The heap pools of the one collector `opts` chooses, from HEAP_POOLS."""
    flags = set(opts.split())
    found = [pools for chosen, pools in HEAP_POOLS.items() if flags.issuperset(chosen)]
    if len(found) != 1:
        raise ValueError(f"no known heap pools for JAVA_OPTS {opts!r}")
    return found[0]


def prometheus_config(contract_text, env):
    """The stand-in's prometheus.yml: a global block, then the contract as it is with the
    namespace filled in. Any other placeholder is refused."""
    filled = contract_text.replace(PLACEHOLDER, env["NS_CATALOG_POLARIS"])
    if "${" in filled:
        raise ValueError("the scrape contract has a placeholder other than " + PLACEHOLDER)
    return (f"global:\n  scrape_interval: {SCRAPE_INTERVAL}\n"
            f"  evaluation_interval: {SCRAPE_INTERVAL}\n" + filled)


# Arguments: the stand-in's base URL, seconds to wait, how many targets must be up. Asks
# QUERIES every 5 s until that many `up` samples are 1 and every query has an answer, then
# prints {"answers": {name: samples}, "targets": [{job, pod, health, lastError}]} on one line.
QUERY = r"""
import json, sys, time, urllib.parse, urllib.request
QUERIES = %s
def get(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.load(r)["data"]
def ask(base):
    answers = {}
    for name, q in QUERIES.items():
        url = base + "/api/v1/query?" + urllib.parse.urlencode({"query": q})
        answers[name] = get(url)["result"]
    return answers
base, timeout, wanted = sys.argv[1], float(sys.argv[2]), int(sys.argv[3])
end, answers = time.monotonic() + timeout, {}
while True:
    try:
        answers = ask(base)
    except OSError as e:
        print("query failed:", e, flush=True)
    up = [s for s in answers.get("up", []) if s["value"][1] == "1"]
    if len(up) >= wanted and all(answers.get(n) for n in QUERIES):
        break
    if time.monotonic() > end:
        break
    time.sleep(5)
targets = []
try:
    for t in get(base + "/api/v1/targets?state=active")["activeTargets"]:
        targets.append({"job": t["labels"].get("job", ""), "pod": t["labels"].get("pod", ""),
                        "health": t.get("health", ""), "lastError": t.get("lastError", "")})
except OSError as e:
    print("targets failed:", e, flush=True)
print(json.dumps({"answers": answers, "targets": targets}), flush=True)
""" % json.dumps(QUERIES)


def query_command(env, image, name, wanted, timeout=TIMEOUT):
    """A short-lived check pod in Polaris's namespace running QUERY (on stdin)."""
    import gate  # gate.py imports this module; its context is read when called.
    import network
    base = f"http://prometheus.{env['NS_STANDINS']}.svc.cluster.local:{PORT}"
    return ["kubectl", "--context", gate.CONTEXT, "-n", env["NS_CATALOG_POLARIS"], "run", name,
            f"--image={image}", "--restart=Never", "--rm", "-i", "--quiet",
            f"--labels={network.CHECK_LABEL}=true", "--pod-running-timeout=3m",
            "--command", "--", "python3", "-", base, str(timeout), str(wanted)]


def parse_answer(text):
    """QUERY's JSON line (the last one that parses), or None."""
    for line in reversed(text.splitlines()):
        try:
            return json.loads(line)
        except ValueError:
            continue
    return None


def _values(samples, job):
    """{pod: value} of the samples of `job`."""
    return {s["metric"].get("pod", ""): s["value"][1] for s in samples
            if s["metric"].get("job") == job}


def problems(answer, pods, pools=None):
    """(problems, notes): every running pod of each job up, Polaris's request count above 0,
    every instance's collector up, every Polaris pod's heap pools `pools` (by default those
    of values-lakehouse.yaml's collector). `pods` is {job: [running pod names]}."""
    pools = heap_pools(java_opts()) if pools is None else pools
    if not answer:
        return ["the stand-in gave no answer: see the query's output above"], []
    found, notes = [], []
    answers = answer["answers"]
    for job in JOBS:
        if not pods.get(job):
            found.append(f"no running {job} pod to scrape")
            continue
        up = _values(answers.get("up", []), job)
        for pod in pods[job]:
            if up.get(pod) == "1":
                notes.append(f"{job} {pod}: up")
            else:
                found.append(f"{job} {pod}: up is {up.get(pod, 'missing')}, not 1")
    if pods.get("polaris"):
        counts = [float(v) for v in _values(answers.get("http", []), "polaris").values()]
        if not any(c > 0 for c in counts):
            found.append("polaris: no http_server_requests_seconds_count above 0")
        else:
            notes.append(f"polaris: http_server_requests_seconds_count in {len(counts)} series")
    for pod in pods.get("polaris", []):
        ids = {s["metric"].get("id", "") for s in answers.get("heap", [])
               if s["metric"].get("job") == "polaris" and s["metric"].get("pod") == pod}
        if ids != pools:
            found.append(f"polaris {pod}: heap pools {', '.join(sorted(ids)) or 'missing'}, "
                         f"not {', '.join(sorted(pools))} (ADR-061)")
    collectors = _values(answers.get("cnpg", []), "polaris-pg")
    for pod in pods.get("polaris-pg", []):
        if collectors.get(pod) != "1":
            found.append(f"polaris-pg {pod}: cnpg_collector_up is "
                         f"{collectors.get(pod, 'missing')}, not 1")
    if found:
        notes += [f"target {t['job']} {t['pod']} {t['health']}: {t['lastError']}"
                  for t in answer.get("targets", [])]
    return found, notes
