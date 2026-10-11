"""The kind gate (ADR-017, ADR-018, ADR-029): pinned tools, the cluster, the CNPG operator.

  tools BIN_DIR [NAME...]
                  downloads kind, kubectl, helm and kubeconform (or only the NAMEs) from
                  gates/pins.json into BIN_DIR, each checked against its pinned sha256 before
                  anything is written.
  up              creates the kind cluster (one control-plane, two workers, the pinned node
                  image), lets the control-plane take pods, so three nodes hold the three
                  PostgreSQL instances apart, then installs the CNPG operator from its pinned
                  chart archive into NS_CNPG_SYSTEM and waits for it. The operator exists here
                  only because the gate has no operator team (ADR-029); a release never
                  installs it.
  secrets         writes `.env` for the gate (its context, the stand-ins' endpoints and
                  credentials for the S3 stand-in, which is the gate's own, ADR-031), then runs
                  scripts/init_env.py --apply-secrets (ADR-006). Refuses an existing `.env`.
  standins        applies the stand-ins (gates/standins.py: SeaweedFS for S3, OPA with
                  gates/opa/policy.rego) in NS_STANDINS and waits for them (ADR-031). Never
                  part of a release; the install's endpoint check needs them on the gate.
  check-standins  runs gates/check_standins.sh in a short-lived curl pod in Polaris's namespace:
                  an S3 round trip with Polaris's credentials, and every case of
                  gates/opa/cases.json against the policy endpoint (ADR-026, ADR-031).
  install [--fresh] [--dry-run]
                  installs the latest release HEAD contains (released_tags) from a checkout of
                  its tag, then this commit's release over it (ADR-021); with no tag, or with
                  --fresh, only this commit's release. With --dry-run, this commit's release is
                  a server-side dry-run over the installed one (ADR-018). Prints which it did.
  check-polaris-pg
                  compares the running `Cluster` polaris-pg with the values its helm release
                  was installed with, and checks its instances run on different nodes
                  (ADR-016).

Every kubectl and helm call names the gate's context, and a namespace where the object has
one. Nothing here reaches any cluster but the kind cluster it creates (ADR-017).

Usage: gate.py tools BIN_DIR [NAME...] | up | secrets | standins | check-standins | install [--fresh] [--dry-run] | check-polaris-pg
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
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "install"))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import init_env  # noqa: E402
import release  # noqa: E402
import release_tag  # noqa: E402
import standins  # noqa: E402

CLUSTER = "lakehouse-gate"
CONTEXT = f"kind-{CLUSTER}"
TOOLS = ("kind", "kubectl", "helm", "kubeconform")
# The binary inside each tool's archive; the others are plain binaries.
ARCHIVE_MEMBERS = {"helm": "linux-amd64/helm", "kubeconform": "kubeconform"}
# On the gate the S3 stand-in is the gate's own, so the gate makes up its credentials; on a
# test or target cluster they are another team's and typed in by hand (ADR-031).
GATE_ENV_GENERATED = ("POLARIS_S3_ACCESS_KEY_ID", "POLARIS_S3_SECRET_ACCESS_KEY")
# On the gate, the endpoints of other teams' services are the stand-ins' (ADR-031).
STANDIN_REGION = "us-east-1"
STANDIN_BUCKET = "polaris-warehouse"


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


def tools(pins, bin_dir, names=TOOLS, get=download):
    """Fetches and checks every named tool first, then writes them; a bad hash writes nothing."""
    for name in names:
        if name not in TOOLS:
            raise KeyError(f"{name}: not a gate tool ({', '.join(TOOLS)})")
    blobs = {name: fetch(pins[name]["url"], pins[name]["sha256"], get) for name in names}
    for name, member in ARCHIVE_MEMBERS.items():
        if name in blobs:
            with tarfile.open(fileobj=io.BytesIO(blobs[name]), mode="r:gz") as tar:
                blobs[name] = tar.extractfile(member).read()
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
        ["kubectl", "--context", CONTEXT, "taint", "nodes", f"{CLUSTER}-control-plane",
         "node-role.kubernetes.io/control-plane:NoSchedule-"],
        ["helm", "--kube-context", CONTEXT, "install", "cnpg", chart_path,
         "--namespace", namespace, "--create-namespace", "--wait", "--timeout", "5m"],
    ]


def standin_endpoints(ns):
    """The `.env` values naming the stand-ins in namespace `ns`."""
    return {
        "S3_ENDPOINT": f"http://seaweedfs.{ns}.svc.cluster.local:{standins.S3_PORT}",
        "S3_REGION": STANDIN_REGION,
        "POLARIS_WAREHOUSE_BUCKET": STANDIN_BUCKET,
        "POLARIS_OPA_URL": (f"http://opa.{ns}.svc.cluster.local:{standins.OPA_PORT}"
                            f"{standins.OPA_PACKAGE_PATH}"),
    }


def gate_env(template_text, gen=init_env.generate):
    """`.env` for the gate: its context, the stand-ins' endpoints and the S3 stand-in's
    credentials; init_env fills the rest."""
    fixed = {"KUBE_CONTEXT": CONTEXT,
             **standin_endpoints(init_env.parse(template_text)["NS_STANDINS"])}
    lines = []
    for line in template_text.splitlines():
        name = line.split("=", 1)[0]
        if name in fixed:
            line = f"{name}={fixed[name]}"
        elif name in GATE_ENV_GENERATED:
            line = f"{name}={gen(name)}"
        lines.append(line)
    return "\n".join(lines) + "\n"


def write_gate_env(path, template_text):
    if os.path.exists(path):
        sys.exit(f"{path} exists: the gate writes its own .env and never overwrites one")
    init_env.write_env(path, gate_env(template_text))


def standins_commands(env):
    """Apply the stand-ins' List (on stdin), then wait until both are ready."""
    return [
        ["kubectl", "--context", CONTEXT, "apply", "--server-side", "--field-manager=gate",
         "-f", "-"],
        ["kubectl", "--context", CONTEXT, "-n", env["NS_STANDINS"], "rollout", "status",
         "deployment/seaweedfs", "deployment/opa", "--timeout=5m"],
    ]


def check_standins_command(env, cases, image, name):
    """The pod that runs check_standins.sh (on stdin) with Polaris's S3 credentials."""
    creds = [{"name": var, "valueFrom": {"secretKeyRef": {"name": "polaris-s3", "key": key}}}
             for var, key in (("AWS_ACCESS_KEY_ID", "accessKeyId"),
                              ("AWS_SECRET_ACCESS_KEY", "secretAccessKey"))]
    overrides = {"spec": {"containers": [{"name": name, "image": image, "env": creds,
                                          "stdin": True, "stdinOnce": True}]}}
    argv = ["kubectl", "--context", CONTEXT, "-n", env["NS_CATALOG_POLARIS"], "run", name,
            f"--image={image}", "--restart=Never", "--rm", "-i", "--quiet",
            "--pod-running-timeout=3m", "--override-type=strategic",
            "--overrides", json.dumps(overrides),
            "--command", "--", "sh", "-s", "--", env["S3_ENDPOINT"],
            env["POLARIS_WAREHOUSE_BUCKET"], env["S3_REGION"], env["POLARIS_OPA_URL"]]
    for case in cases:
        argv += [case["name"], "true" if case["allow"] else "false",
                 json.dumps(case["input"], separators=(",", ":"))]
    return argv


def install_plan(previous, previous_dir, fresh, dry_run=False):
    """The commands that install a release on the gate, and the line that says which."""
    this = ["python3", os.path.join(ROOT, "install", "release.py")] + (
        ["--dry-run"] if dry_run else [])
    then = " (server-side dry-run)" if dry_run else ""
    if fresh:
        return [this], f"Fresh install of this commit{then}, as asked."
    if not previous:
        return [this], f"No release to upgrade from: fresh install{then} (ADR-021)."
    return [
        ["git", "-C", ROOT, "worktree", "add", "--detach", previous_dir, previous],
        ["python3", os.path.join(previous_dir, "install", "release.py")],
        this,
    ], f"Upgrade: {previous} installed from its tag, then this commit over it{then} (ADR-021)."


def polaris_pg_commands(namespace):
    """The helm release's values, the Cluster and its instance pods (not Job pods), as JSON."""
    kubectl = ["kubectl", "--context", CONTEXT, "-n", namespace, "get"]
    return [
        ["helm", "--kube-context", CONTEXT, "-n", namespace, "get", "values",
         release.POLARIS_PG_RELEASE, "--all", "-o", "json"],
        kubectl + [f"cluster/{release.POLARIS_PG}", "-o", "json"],
        kubectl + ["pods", "-l", f"cnpg.io/cluster={release.POLARIS_PG},cnpg.io/podRole=instance",
                   "-o", "json"],
    ]


def polaris_pg_problems(values, cluster, pods):
    """The running Cluster against the values it was installed with (ADR-016)."""
    spec, problems = cluster["spec"], []
    for field, running, wanted in (
            ("instances", spec["instances"], values["instances"]),
            ("image", spec["imageName"], values["image"]),
            ("podAntiAffinityType", spec["affinity"]["podAntiAffinityType"],
             values["podAntiAffinityType"])):
        if running != wanted:
            problems.append(f"{field}: {running} running, {wanted} in the values")
    ready = cluster.get("status", {}).get("readyInstances", 0)
    if ready != values["instances"]:
        problems.append(f"{ready} instances ready of {values['instances']}")
    nodes = sorted({p["spec"].get("nodeName") for p in pods["items"]})
    if len(nodes) != len(pods["items"]) or len(nodes) != values["instances"]:
        problems.append(f"{len(pods['items'])} instances on {len(nodes)} nodes: "
                        + ", ".join(str(n) for n in nodes))
    anti = spec["affinity"]["podAntiAffinityType"]
    return problems, [f"Cluster {release.POLARIS_PG}: {ready} instances ready on "
                      f"{len(nodes)} nodes ({', '.join(str(n) for n in nodes)}), image "
                      f"{spec['imageName']}, anti-affinity {anti}."]


def _git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, check=False)


def released_tags(repo=ROOT):
    """The release tags whose content HEAD contains.

    A tag sits on an `rc` merge commit that `main` never contains (ADR-033), so a tag counts
    when it is an ancestor of HEAD (a commit on `rc`) or when its second parent, the `main`
    commit the release gate tested, is (a commit on `main` or a gate branch).
    """
    tags = _git(repo, "tag", "--list", "platform-*").stdout.split()
    return [t for t in tags
            if _git(repo, "merge-base", "--is-ancestor", t, "HEAD").returncode == 0
            or _git(repo, "merge-base", "--is-ancestor", f"{t}^2", "HEAD").returncode == 0]


def run(argv, stdin=None):
    print("+ " + " ".join(argv), flush=True)
    subprocess.run(argv, input=stdin, text=True, check=True)


def main(argv):
    pins = load_pins()
    if len(argv) >= 3 and argv[1] == "tools":
        tools(pins, argv[2], argv[3:] or TOOLS)
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
    if argv[1:] == ["secrets"]:
        with open(os.path.join(ROOT, ".env.template"), encoding="utf-8") as f:
            write_gate_env(os.path.join(ROOT, ".env"), f.read())
        run(["python3", os.path.join(ROOT, "scripts", "init_env.py"), "--apply-secrets"])
        return 0
    if argv[1:] == ["standins"]:
        env = release.load_env(ROOT, os.environ)
        with open(os.path.join(HERE, "opa", "policy.rego"), encoding="utf-8") as f:
            manifest = json.dumps(standins.manifests(env, pins, f.read()))
        apply, wait = standins_commands(env)
        print("+ " + " ".join(apply) + "  (the stand-ins' List on stdin)", flush=True)
        subprocess.run(apply, input=manifest, text=True, check=True)
        run(wait)
        return 0
    if argv[1:] == ["check-standins"]:
        env = release.load_env(ROOT, os.environ)
        cases = standins.load_cases(os.path.join(HERE, "opa", "cases.json"))
        with open(os.path.join(HERE, "check_standins.sh"), encoding="utf-8") as f:
            script = f.read()
        command = check_standins_command(env, cases, release.images()["curl"],
                                         f"check-standins-{os.getpid()}")
        result = subprocess.run(command, input=script, text=True, check=False)
        return 1 if result.returncode else 0
    if argv[1:2] == ["install"] and set(argv[2:]) <= {"--fresh", "--dry-run"} \
            and len(set(argv[2:])) == len(argv[2:]):
        with tempfile.TemporaryDirectory() as d:
            commands, summary = install_plan(release_tag.previous(released_tags()),
                                             os.path.join(d, "previous"),
                                             "--fresh" in argv, "--dry-run" in argv)
            print(summary, flush=True)
            for command in commands:
                run(command)
        return 0
    if argv[1:] == ["check-polaris-pg"]:
        namespace = os.environ.get("NS_CATALOG_POLARIS") or "catalog-polaris"
        answers = [json.loads(subprocess.run(c, capture_output=True, text=True,
                                             check=True).stdout)
                   for c in polaris_pg_commands(namespace)]
        problems, notes = polaris_pg_problems(*answers)
        for line in notes + problems:
            print(line)
        return 1 if problems else 0
    print(__doc__.strip().splitlines()[-1], file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
