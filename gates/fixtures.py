"""Create Fixtures (ADR-040): Polaris's own log files across a real UTC hour, as a release writes
them, for the Log Batch Gate (gates/log_batch_gate.py).

  spec-hash       prints the spec hash of this checkout: the SHA-256 over SPEC_FILES and, from
                  VALUES, only Polaris's image, the `quarkus.log.file.*` settings and `TZ`
                  (values_spec), so another change to Polaris's values keeps the fixtures. This
                  file is in the spec; the gate's checks are not, so changing a check makes no new
                  fixtures.
  changes BASE    prints `spec_changed=true|false` and `spec_hash=<this checkout's>`, lines for
                  $GITHUB_OUTPUT: whether the commit BASE (read with `git show`) has another
                  spec hash.
  capture OUT     on the gate's kind cluster with this commit installed (`gate.py up`, `secrets`,
                  `batch-image`, `standins`, `install --fresh`): pauses the batch's CronJob, so
                  nothing moves a file; sends gates/logs_traffic.sh to both Polaris pods (prefix
                  BEFORE), with a catalog on DOWN_ENDPOINT, which does not answer, so each pod
                  answers one 500 and writes `exception`; waits for the next UTC hour plus 60 s;
                  sends the traffic again (prefix AFTER, no 500), which rolls each pod's file to
                  `.yyyy-MM-dd-HH.gz`; then copies every pod's rolls and current file from the
                  claim's directory on the runner (gates/kind.yaml) into OUT with manifest.json:
                  the spec hash, the commit, Polaris's image, the hour crossed, the batch's clock,
                  the pods, the request ID prefixes, and each file's SHA-256, size, mtime and
                  lines. Fails if a pod lacks its roll or its current file.

Every kubectl call names the gate's context and a namespace (ADR-017). Nothing here reaches any
cluster but the kind cluster.

Usage: fixtures.py spec-hash | changes BASE | capture OUT
"""

import datetime as dt
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import gate  # noqa: E402

release = gate.release

SPEC_FILES = ("gates/logs_traffic.sh", ".github/workflows/create-fixtures.yml",
              "gates/fixtures.py")
VALUES = "charts/polaris/values-lakehouse.yaml"
# A closed port in Polaris's own pod: a catalog on it answers a table made with 500.
DOWN_ENDPOINT = "http://127.0.0.1:9"
BEFORE, AFTER = "fx-pre", "fx-post"
COUNT = 2
GRACE = 60
MAX_WAIT = 3600 + GRACE
LOG_FILE_RE = re.compile(r"^polaris-.+\.log(\.\d{4}-\d{2}-\d{2}-\d{2}(\.\d+)?\.gz)?$")


def values_spec(text):
    """The lines of values-lakehouse.yaml the fixtures depend on, comments left out: the image's
    repository and tag, every `quarkus.log.file.*` setting, and the TZ entry with its value."""
    lines = [line for line in text.splitlines() if line.strip()
             and not line.lstrip().startswith("#")]
    spec, image, tz = [], [], []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if line == "image:":
            for sub in lines[i + 1:]:
                if not sub.startswith(" "):
                    break
                if sub.strip().startswith(("repository:", "tag:")):
                    image.append(sub.strip())
        elif stripped.startswith("quarkus.log.file."):
            spec.append(stripped)
        elif stripped == "- name: TZ" and i + 1 < len(lines):
            tz = [stripped, lines[i + 1].strip()]
    if len(image) != 2 or not spec or not tz:
        raise ValueError(f"{VALUES}: no image repository and tag, quarkus.log.file settings or "
                         f"TZ entry")
    return image + spec + tz


def spec_hash(read):
    """The SHA-256 over the spec; `read(path)` gives a file's text, or None when it is missing."""
    h = hashlib.sha256()
    for path in SPEC_FILES:
        text = read(path)
        h.update(f"{path}\0{'-' if text is None else len(text)}\0{text or ''}\0".encode())
    h.update("\n".join(values_spec(read(VALUES) or "")).encode())
    return h.hexdigest()


def git_reader(repo, commit):
    """A reader of the files of `commit`."""
    def read(path):
        done = subprocess.run(["git", "-C", repo, "show", f"{commit}:{path}"],
                              capture_output=True, text=True, check=False)
        return done.stdout if done.returncode == 0 else None
    return read


def checkout_reader(root=ROOT):
    def read(path):
        try:
            with open(os.path.join(root, path), encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            return None
    return read


def changes(base, head):
    """(whether the spec hash differs, head's spec hash). A base whose values lack the spec
    counts as changed."""
    head_hash = spec_hash(head)
    try:
        return spec_hash(base) != head_hash, head_hash
    except ValueError:
        return True, head_hash


def boundary(now):
    """The next UTC hour after `now`."""
    hour = now.astimezone(dt.timezone.utc).replace(minute=0, second=0, microsecond=0)
    return hour + dt.timedelta(hours=1)


def wait_seconds(now):
    """Seconds from `now` to the next UTC hour plus GRACE."""
    return int((boundary(now) - now).total_seconds()) + GRACE


def claim_dir(host_dir, namespace):
    """The directory local-path made for the claim polaris-logs in `namespace`."""
    found = [d for d in sorted(os.listdir(host_dir))
             if d.endswith(f"_{namespace}_{release.POLARIS_LOGS}")
             and os.path.isdir(os.path.join(host_dir, d))]
    if len(found) != 1:
        raise ValueError(f"{len(found)} directories for the claim {release.POLARIS_LOGS} of "
                         f"{namespace} in {host_dir}: {found}")
    return os.path.join(host_dir, found[0])


def log_files(directory):
    """Polaris's files at the top of the claim: each pod's current file and its rolls."""
    return sorted(n for n in os.listdir(directory) if LOG_FILE_RE.match(n)
                  and os.path.isfile(os.path.join(directory, n)))


def file_entry(path):
    with open(path, "rb") as f:
        data = f.read()
    text = gzip.decompress(data) if path.endswith(".gz") else data
    return {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
            "mtime": int(os.stat(path).st_mtime), "lines": text.count(b"\n")}


def build_manifest(directory, **meta):
    return {**meta, "files": {n: file_entry(os.path.join(directory, n))
                              for n in log_files(directory)}}


def capture_problems(files, pods, roll_hour):
    """Each pod must have rolled once for `roll_hour` (YYYY-MM-DD-HH) and kept writing."""
    problems = []
    for pod in pods:
        if f"polaris-{pod}.log.{roll_hour}.gz" not in files:
            problems.append(f"pod {pod}: no roll polaris-{pod}.log.{roll_hour}.gz")
        if f"polaris-{pod}.log" not in files:
            problems.append(f"pod {pod}: no current file polaris-{pod}.log")
    return problems


def suspend_command(env):
    return ["kubectl", "--context", gate.CONTEXT, "-n", env["NS_CATALOG_POLARIS"], "patch",
            f"cronjob/{release.BATCH}", "--type=merge",
            "-p", json.dumps({"spec": {"suspend": True}})]


def traffic(env, targets, prefix, down):
    with open(os.path.join(HERE, "logs_traffic.sh"), encoding="utf-8") as f:
        script = f.read()
    argv = gate.logs_traffic_command(env, release.images()["curl"], f"fixtures-{prefix}",
                                     prefix, COUNT, targets, down=down)
    if subprocess.run(argv, input=script, text=True, check=False).returncode:
        sys.exit(f"the traffic {prefix} failed: see its lines above")


def utcnow():
    return dt.datetime.now(dt.timezone.utc)


def capture(env, out):
    """`capture`: see the module's docstring."""
    ns = env["NS_CATALOG_POLARIS"]
    kubectl = ["kubectl", "--context", gate.CONTEXT, "-n", ns]
    # Paused for good: it would move the rolls at minute 3, and the cluster is thrown away.
    gate.run(suspend_command(env))
    pods = gate._running(json.loads(gate.output(
        kubectl + ["get", "pods", "-l", gate.POLARIS_POD_LABELS, "-o", "json"])))
    if len(pods) < 2:
        sys.exit(f"{len(pods)} Polaris pod(s) running: the capture needs both replicas")
    targets = [(p["metadata"]["name"],
                f"http://{p['status']['podIP']}:{gate.POLARIS_API_PORT}") for p in pods]
    names = [name for name, _ in targets]
    image = pods[0]["spec"]["containers"][0]["image"]
    traffic(env, targets, BEFORE, DOWN_ENDPOINT)
    start = utcnow()
    edge, wait = boundary(start), wait_seconds(start)
    print(f"Waiting {wait} s for the UTC hour {edge:%Y-%m-%dT%H:%M}Z plus {GRACE} s.",
          flush=True)
    end_wait = time.monotonic() + wait
    while time.monotonic() < end_wait:
        time.sleep(min(300, max(0, end_wait - time.monotonic())))
        print(f"{max(0, int(end_wait - time.monotonic()))} s left.", flush=True)
    traffic(env, targets, AFTER, None)
    end = utcnow()
    source = claim_dir(gate.SHARED_HOST_DIR, ns)
    os.makedirs(out, exist_ok=True)
    for name in log_files(source):
        shutil.copy2(os.path.join(source, name), os.path.join(out, name))
    rolled = edge - dt.timedelta(hours=1)
    commit = subprocess.run(["git", "-C", ROOT, "rev-parse", "HEAD"], capture_output=True,
                            text=True, check=True).stdout.strip()
    manifest = build_manifest(
        out, spec_hash=spec_hash(checkout_reader()), commit=commit, polaris_image=image,
        captured=end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        boundary=edge.strftime("%Y-%m-%dT%H:%M:%SZ"), hour=rolled.strftime("%Y%m%d-%H"),
        now=gate.batch_now(end), pods=names, count=COUNT, before=BEFORE, after=AFTER,
        down=DOWN_ENDPOINT)
    with open(os.path.join(out, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
        f.write("\n")
    for name, entry in manifest["files"].items():
        print(f"{entry['sha256']} {entry['lines']:6d} {name}", flush=True)
    problems = capture_problems(manifest["files"], names, rolled.strftime("%Y-%m-%d-%H"))
    if problems:
        sys.exit("The capture is incomplete:\n" + "\n".join(problems))


def main(argv):
    if argv[1:] == ["spec-hash"]:
        print(spec_hash(checkout_reader()))
        return 0
    if len(argv) == 3 and argv[1] == "changes":
        changed, head = changes(git_reader(ROOT, argv[2]), checkout_reader())
        print(f"spec_changed={'true' if changed else 'false'}\nspec_hash={head}")
        return 0
    if len(argv) == 3 and argv[1] == "capture":
        capture(release.load_env(ROOT, os.environ), argv[2])
        return 0
    print(__doc__.strip().splitlines()[-1], file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
