"""The Log Batch Gate (ADR-040): the batch over Create Fixtures' files (gates/fixtures.py),
shipped by a stand-in Fluent Bit into an OpenSearch stand-in, then read back.

  verify DIR      refuses an artifact whose manifest's spec hash differs from this checkout's, or
                  whose files differ from the manifest (missing, unknown, another SHA-256 or
                  size); restores each file's mtime from the manifest, which an artifact loses.
  run DIR         verifies DIR, then on the runner's Docker, without a cluster:
                  1. the batch's image, built from this checkout (`gate.py batch-image`'s build),
                     runs twice over a copy, with no network, no pod list and its clock at the
                     manifest's `now`, past the second hour captured: both hours are published,
                     the first from the rolls, the second from the current files;
                  2. OpenSearch (gates/pins.json, one node, security off, its defaults: dynamic
                     mapping, no index template, #9) and the stand-in Fluent Bit (pinned, the
                     outputs read-only, as a user neither root nor the batch's), which ships
                     processed-logs/ and aggregated-logs/ into one index each (INDICES);
                  3. every document read back, then checked: gate.py's logs_problems, as
                     check-logs runs it on smoke, with the documents for what Fluent Bit read;
                     each request in the hour of its timestamp across the roll, both hours
                     published, each pod's roll read; each 500 kept with its ERROR line and
                     `exception`; `mdc`, `http_status` and `exception` one way in the index, and
                     no error from Fluent Bit (a refused document).
                  Containers and the network are removed whatever the outcome.

ADR-031: these stand-ins are never part of a release, are pinned, and run on the runner only,
outside any cluster; OpenSearch listens on the runner's Docker network and on 127.0.0.1.

Usage: log_batch_gate.py verify DIR | run DIR
"""

import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import fixtures  # noqa: E402
import gate  # noqa: E402

INDICES = {"processed-logs": "polaris-processed", "aggregated-logs": "polaris-aggregated"}
OPENSEARCH_PORT = 9200
OPENSEARCH_TIMEOUT = 300
SEARCH_SIZE = 10000
# Fields and the mapping each must have in the processed index.
MAPPED = (("mdc", "object"), ("http_status", "number"), ("exception", "object"))
NUMBER_TYPES = ("long", "integer", "short", "byte", "double", "float")


def verify(directory):
    """The manifest, once the files match it; each file gets its mtime back."""
    path = os.path.join(directory, "manifest.json")
    if not os.path.exists(path):
        raise ValueError(f"no manifest.json in {directory}")
    with open(path, encoding="utf-8") as f:
        manifest = json.load(f)
    want = fixtures.spec_hash(fixtures.checkout_reader())
    if manifest.get("spec_hash") != want:
        raise ValueError(f"the artifact's spec hash {manifest.get('spec_hash')} is not this "
                         f"checkout's {want}: run Create Fixtures")
    files = manifest.get("files", {})
    present = set(fixtures.log_files(directory))
    problems = [f"{n}: missing" for n in sorted(set(files) - present)]
    problems += [f"{n}: not in the manifest" for n in sorted(present - set(files))]
    for name in sorted(set(files) & present):
        have = fixtures.file_entry(os.path.join(directory, name))
        for key in ("sha256", "size"):
            if have[key] != files[name][key]:
                problems.append(f"{name}: {key} {have[key]}, the manifest says "
                                f"{files[name][key]}")
    if problems:
        raise ValueError("the artifact differs from its manifest: " + "; ".join(problems))
    for name, entry in files.items():
        os.utime(os.path.join(directory, name), (entry["mtime"], entry["mtime"]))
    return manifest


def stage(directory, manifest, work):
    """A copy of the files the batch may move and write next to, as the claim is."""
    os.makedirs(work, exist_ok=True)
    os.chmod(work, 0o777)
    for name, entry in manifest["files"].items():
        shutil.copyfile(os.path.join(directory, name), os.path.join(work, name))
        os.utime(os.path.join(work, name), (entry["mtime"], entry["mtime"]))


def listing(work):
    out = subprocess.run(["sh", "-s", "--", work], input=gate.LOGS_LISTING,
                         capture_output=True, text=True, check=True).stdout
    return gate.parse_listing(out)


def replay(work, manifest, run_batch):
    """The batch twice at the manifest's clock -> (events per run, listing after each run).
    `run_batch(work, now)` returns the batch's stdout."""
    events, listings = [], []
    for _ in range(2):
        out = run_batch(work, manifest["now"])
        print(out, end="", flush=True)
        events.append(gate.batch_events(out))
        listings.append(listing(work))
    return events, listings[0], listings[1]


def documents(docs):
    """Every document's source, without Fluent Bit's @timestamp, as check-logs reads lines."""
    return [{k: v for k, v in d.items() if k != "@timestamp"}
            for index in sorted(docs) for d in docs[index]]


def sent(manifest):
    """Both rounds of traffic: the first with the 500s (fixtures.capture)."""
    out = gate.logs_request_ids(manifest["before"], manifest["count"], manifest["pods"],
                                failing=bool(manifest.get("down")))
    out.update(gate.logs_request_ids(manifest["after"], manifest["count"], manifest["pods"]))
    return out


def next_hour(hour):
    """`YYYYMMDD-HH` plus one hour."""
    t = dt.datetime.strptime(hour, "%Y%m%d-%H") + dt.timedelta(hours=1)
    return t.strftime("%Y%m%d-%H")


def roll_problems(manifest, events, recs):
    """Across the roll: both hours published, each pod's roll read for the first, and each
    request kept in the hour of its round."""
    problems = []
    first, second = manifest["hour"], next_hour(manifest["hour"])
    published = {e["hour"]: e for e in events[0] if e.get("event") == "published"}
    for hour in (first, second):
        if hour not in published:
            problems.append(f"the batch did not publish hour {hour}")
    roll = f"{first[:4]}-{first[4:6]}-{first[6:8]}-{first[9:]}"
    read = {s.get("file") for s in published.get(first, {}).get("sources", [])}
    for pod in manifest["pods"]:
        name = f"polaris-{pod}.log.{roll}.gz"
        if first in published and name not in read:
            problems.append(f"hour {first}: the batch did not read the roll {name}")
    hours = {manifest["before"]: first, manifest["after"]: second}
    for record in recs:
        rid = record.get("mdc", {}).get("requestId", "")
        path = record.get("source_file", "")
        if not path.startswith(f"{gate.LOGS_MOUNT}/processed-logs/"):
            continue
        for prefix, hour in hours.items():
            if rid.startswith(prefix + "-") and not path.endswith(f"/{hour}.jsonl"):
                problems.append(f"request {rid}: kept in {path}, not in hour {hour}")
    return problems


def mapping_problems(mappings):
    """`mdc` and `exception` objects and `http_status` a number in the processed index."""
    index = INDICES["processed-logs"]
    problems = [f"index {i}: missing" for i in INDICES.values() if i not in mappings]
    props = mappings.get(index, {}).get("mappings", {}).get("properties", {})
    for field, kind in MAPPED:
        m = props.get(field)
        if m is None:
            if index in mappings:
                problems.append(f"index {index}: no mapping for {field}")
            continue
        ok = ("properties" in m and m.get("type", "object") in ("object", "nested")
              if kind == "object" else m.get("type") in NUMBER_TYPES)
        if not ok:
            problems.append(f"index {index}: {field} is mapped as {m.get('type', 'object')}, "
                            f"want {'an object' if kind == 'object' else 'a number'}")
    return problems


def fluent_bit_problems(log):
    errors = [line for line in log.splitlines() if "[error]" in line]
    return [f"Fluent Bit reported {len(errors)} error(s), first: {errors[0][:300]}"] if errors \
        else []


def checks(manifest, events, before, after, records, mappings, fluent_bit_log):
    """Every check of `run` over what it read -> (problems, notes)."""
    problems, notes = gate.logs_problems(sent(manifest), manifest["pods"], events, before,
                                         after, records)
    problems = problems + roll_problems(manifest, events, records) \
        + mapping_problems(mappings) + fluent_bit_problems(fluent_bit_log)
    if problems:
        return problems, []
    first = manifest["hour"]
    return [], notes + [
        f"Across the roll at {manifest['boundary']}: hours {first} and {next_hour(first)} "
        f"published, each pod's roll read, each request kept in the hour of its round.",
        f"OpenSearch held every line as one document, `mdc`, `http_status` and `exception` "
        f"mapped one way, nothing refused."]


def batch_command(image, work, now):
    return ["docker", "run", "--rm", "--network", "none", "-v", f"{work}:{gate.LOGS_MOUNT}",
            image, "--log-dir", gate.LOGS_MOUNT, "--no-pod-list", "--now", now]


def opensearch_command(image, name, network):
    return ["docker", "run", "-d", "--name", name, "--network", network,
            "-p", f"127.0.0.1:{OPENSEARCH_PORT}:9200",
            "-e", "discovery.type=single-node", "-e", "DISABLE_SECURITY_PLUGIN=true",
            "-e", "DISABLE_INSTALL_DEMO_CONFIG=true",
            "-e", "OPENSEARCH_JAVA_OPTS=-Xms512m -Xmx512m", image]


def fluent_bit_command(image, work, network, host):
    """The stand-in for the observability team's Fluent Bit (ADR-024): one tail over both
    output directories, tagged by path, one opensearch output per directory."""
    paths = ",".join(f"{gate.LOGS_MOUNT}/{d}/*.jsonl" for d in gate.OUTPUT_DIRS)
    argv = ["docker", "run", "--rm", "--network", network, "--read-only",
            "--user", f"{gate.READER_UID}:{gate.READER_UID}",
            "-v", f"{work}:{gate.LOGS_MOUNT}:ro", image, "/fluent-bit/bin/fluent-bit",
            "-R", "/fluent-bit/etc/parsers.conf",
            "-i", "tail", "-t", "lb.*", "-p", f"path={paths}", "-p", "read_from_head=true",
            "-p", "exit_on_eof=true", "-p", "parser=json", "-p", "path_key=source_file",
            "-p", "buffer_max_size=1M"]
    for sub, index in INDICES.items():
        argv += ["-o", "opensearch", "-m", f"lb.logs.{sub}.*", "-p", f"host={host}",
                 "-p", "port=9200", "-p", f"index={index}", "-p", "suppress_type_name=on",
                 "-p", "trace_error=on", "-p", "buffer_size=false"]
    return argv + ["-f", "1"]


def http(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{OPENSEARCH_PORT}{path}", data=data,
                                 method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read() or b"{}")


def wait_healthy():
    end = time.monotonic() + OPENSEARCH_TIMEOUT
    while True:
        try:
            if http("GET", "/_cluster/health").get("status") in ("green", "yellow"):
                return
        except OSError:
            pass
        if time.monotonic() > end:
            sys.exit(f"OpenSearch not healthy after {OPENSEARCH_TIMEOUT} s")
        time.sleep(3)


def read_back():
    """{index: [document source]} and the indices' mappings."""
    http("POST", "/_refresh")
    docs = {}
    for index in INDICES.values():
        try:
            found = http("POST", f"/{index}/_search",
                         {"size": SEARCH_SIZE, "query": {"match_all": {}}})
        except urllib.error.HTTPError as err:
            if err.code == 404:
                continue
            raise
        hits = found["hits"]
        if hits["total"]["value"] >= SEARCH_SIZE:
            sys.exit(f"index {index}: {hits['total']['value']} documents, more than one read")
        docs[index] = [h["_source"] for h in hits["hits"]]
    return docs, http("GET", "/" + ",".join(INDICES.values()) + "/_mapping"
                      "?ignore_unavailable=true")


def docker_batch(image):
    def run_batch(work, now):
        done = subprocess.run(batch_command(image, work, now), capture_output=True, text=True,
                              check=False)
        if done.returncode:
            sys.exit(f"the batch failed ({done.returncode}): {done.stdout}{done.stderr}")
        return done.stdout
    return run_batch


def run(directory):
    """`run`: see the module's docstring. Returns (problems, notes)."""
    manifest = verify(directory)
    pins = gate.load_pins()
    build = gate.batch_image_commands()[0]
    image = build[build.index("--tag") + 1]
    gate.run(build)
    pid = os.getpid()
    network, name = f"log-batch-gate-{pid}", f"log-batch-opensearch-{pid}"
    # The batch writes as uid 10000: what it made may not be removable here, and need not be.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        work = os.path.join(tmp, "logs")
        stage(directory, manifest, work)
        events, before, after = replay(work, manifest, docker_batch(image))
        gate.run(["docker", "network", "create", network])
        try:
            gate.run(opensearch_command(pins["standin_images"]["opensearch"]["image"], name,
                                        network))
            wait_healthy()
            done = subprocess.run(
                fluent_bit_command(pins["standin_images"]["fluent_bit"]["image"], work,
                                   network, name),
                capture_output=True, text=True, check=False, timeout=gate.LOGS_TIMEOUT)
            log = done.stdout + done.stderr
            print(log, end="", flush=True)
            if done.returncode:
                return [f"the stand-in Fluent Bit exited {done.returncode}"], []
            docs, mappings = read_back()
        finally:
            subprocess.run(["docker", "rm", "-f", name], check=False, capture_output=True)
            subprocess.run(["docker", "network", "rm", network], check=False,
                           capture_output=True)
        return checks(manifest, events, before, after, documents(docs), mappings, log)


def main(argv):
    if len(argv) == 3 and argv[1] == "verify":
        try:
            manifest = verify(argv[2])
        except ValueError as err:
            sys.exit(str(err))
        print(f"The artifact matches its manifest: {len(manifest['files'])} files, spec hash "
              f"{manifest['spec_hash']}, captured {manifest.get('captured')}.")
        return 0
    if len(argv) == 3 and argv[1] == "run":
        try:
            problems, notes = run(argv[2])
        except ValueError as err:
            sys.exit(str(err))
        print("\n".join(notes or problems))
        return 1 if problems else 0
    print(__doc__.strip().splitlines()[-1], file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
