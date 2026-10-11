"""Tests for gate.py. Run: python3 -m unittest discover -s gates"""

import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import gate  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def sha(data):
    return hashlib.sha256(data).hexdigest()


def tarball(member, binary):
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        info = tarfile.TarInfo(member)
        info.size = len(binary)
        tar.addfile(info, io.BytesIO(binary))
    return out.getvalue()


HELM_TGZ = tarball("linux-amd64/helm", b"helm-binary")
KUBECONFORM_TGZ = tarball("kubeconform", b"kubeconform-binary")
BLOBS = {"https://k/kind": b"kind-binary", "https://k/kubectl": b"kubectl-binary",
         "https://k/helm.tgz": HELM_TGZ, "https://k/kubeconform.tgz": KUBECONFORM_TGZ}
TOOL_PINS = {"kind": {"url": "https://k/kind", "sha256": sha(b"kind-binary")},
             "kubectl": {"url": "https://k/kubectl", "sha256": sha(b"kubectl-binary")},
             "helm": {"url": "https://k/helm.tgz", "sha256": sha(HELM_TGZ)},
             "kubeconform": {"url": "https://k/kubeconform.tgz", "sha256": sha(KUBECONFORM_TGZ)}}


class Pins(unittest.TestCase):
    def test_the_committed_pins_are_complete(self):
        pins = gate.load_pins(os.path.join(HERE, "pins.json"))
        for tool in ("kind", "kubectl", "helm", "kubeconform", "cnpg_chart"):
            with self.subTest(tool=tool):
                self.assertRegex(pins[tool]["sha256"], r"^[0-9a-f]{64}$")
                self.assertTrue(pins[tool]["url"].startswith("https://"))
        self.assertRegex(pins["node_image"]["image"],
                         r"^kindest/node:v1\.\d+\.\d+@sha256:[0-9a-f]{64}$")

    def test_the_operator_pin_is_supported_by_the_install_script(self):
        pins = gate.load_pins(os.path.join(HERE, "pins.json"))
        with open(os.path.join(HERE, "..", "install", "supported.json"), encoding="utf-8") as f:
            supported = json.load(f)["cnpg_operator"]
        minor = ".".join(pins["cnpg_chart"]["operator_version"].split(".")[:2])
        self.assertIn(minor, supported)


class Fetch(unittest.TestCase):
    def test_a_matching_hash_returns_the_bytes(self):
        data = b"payload"
        self.assertEqual(gate.fetch("https://x.invalid/a", sha(data), get=lambda u: data), data)

    def test_a_wrong_hash_raises_and_names_the_url(self):
        with self.assertRaises(ValueError) as caught:
            gate.fetch("https://x.invalid/a", "0" * 64, get=lambda u: b"payload")
        self.assertIn("https://x.invalid/a", str(caught.exception))


class Tools(unittest.TestCase):
    def test_tools_are_written_executable_after_their_hashes_match(self):
        with tempfile.TemporaryDirectory() as d:
            gate.tools(TOOL_PINS, d, get=BLOBS.__getitem__)
            for name, content in (("kind", b"kind-binary"), ("kubectl", b"kubectl-binary"),
                                  ("helm", b"helm-binary"),
                                  ("kubeconform", b"kubeconform-binary")):
                with self.subTest(tool=name):
                    path = os.path.join(d, name)
                    with open(path, "rb") as f:
                        self.assertEqual(f.read(), content)
                    self.assertTrue(os.access(path, os.X_OK))

    def test_only_the_named_tools_are_fetched(self):
        fetched = []

        def get(url):
            fetched.append(url)
            return BLOBS[url]
        with tempfile.TemporaryDirectory() as d:
            gate.tools(TOOL_PINS, d, ["helm", "kubeconform"], get=get)
            self.assertEqual(sorted(os.listdir(d)), ["helm", "kubeconform"])
        self.assertEqual(fetched, ["https://k/helm.tgz", "https://k/kubeconform.tgz"])

    def test_an_unknown_tool_is_refused(self):
        with self.assertRaises(KeyError):
            gate.tools(TOOL_PINS, "/nonexistent", ["kubeconfrom"], get=BLOBS.__getitem__)

    def test_a_bad_hash_writes_nothing(self):
        pins = {name: {"url": pin["url"], "sha256": "0" * 64} for name, pin in TOOL_PINS.items()}
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                gate.tools(pins, d, get=lambda u: b"x")
            self.assertEqual(os.listdir(d), [])


class Commands(unittest.TestCase):
    PINS = {"node_image": {"image": "kindest/node:v1.36.4@sha256:" + "a" * 64}}

    def test_up_creates_the_three_node_cluster_then_installs_cnpg(self):
        create, _, helm = gate.up_commands(self.PINS, "/tmp/chart.tgz", "cnpg-system")
        self.assertEqual(create[:4], ["kind", "create", "cluster", "--name"])
        self.assertEqual(create[create.index("--name") + 1], gate.CLUSTER)
        self.assertEqual(create[create.index("--image") + 1], self.PINS["node_image"]["image"])
        self.assertTrue(create[create.index("--config") + 1].endswith("gates/kind.yaml"))
        self.assertEqual(helm[:3], ["helm", "--kube-context", gate.CONTEXT])
        self.assertIn("/tmp/chart.tgz", helm)
        self.assertEqual(helm[helm.index("--namespace") + 1], "cnpg-system")
        self.assertIn("--wait", helm)

    def test_up_lets_the_control_plane_take_pods_before_cnpg(self):
        _, taint, _ = gate.up_commands(self.PINS, "/tmp/chart.tgz", "cnpg-system")
        self.assertEqual(taint, ["kubectl", "--context", gate.CONTEXT, "taint", "nodes",
                                 f"{gate.CLUSTER}-control-plane",
                                 "node-role.kubernetes.io/control-plane:NoSchedule-"])

    def test_the_kind_config_has_one_control_plane_and_two_workers(self):
        with open(os.path.join(HERE, "kind.yaml"), encoding="utf-8") as f:
            roles = [line.split(":", 1)[1].strip() for line in f if "role:" in line]
        self.assertEqual(roles, ["control-plane", "worker", "worker"])


class Install(unittest.TestCase):
    ROOT = os.path.dirname(HERE)
    THIS = ["python3", os.path.join(os.path.dirname(HERE), "install", "release.py")]

    def test_with_a_release_installs_it_from_its_tag_then_this_commit(self):
        commands, summary = gate.install_plan("platform-2026.10.10", "/tmp/prev", fresh=False)
        self.assertEqual(commands, [
            ["git", "-C", self.ROOT, "worktree", "add", "--detach", "/tmp/prev",
             "platform-2026.10.10"],
            ["python3", "/tmp/prev/install/release.py"],
            self.THIS,
        ])
        self.assertIn("platform-2026.10.10", summary)
        self.assertIn("ADR-021", summary)

    def test_without_a_release_installs_this_commit_and_says_so(self):
        commands, summary = gate.install_plan(None, "/tmp/prev", fresh=False)
        self.assertEqual(commands, [self.THIS])
        self.assertEqual(summary, "No release to upgrade from: fresh install (ADR-021).")

    def test_fresh_ignores_the_release(self):
        commands, summary = gate.install_plan("platform-2026.10.10", "/tmp/prev", fresh=True)
        self.assertEqual(commands, [self.THIS])
        self.assertIn("Fresh install", summary)

    def test_dry_run_installs_the_release_then_dry_runs_this_commit_over_it(self):
        commands, summary = gate.install_plan("platform-2026.10.10", "/tmp/prev", fresh=False,
                                              dry_run=True)
        self.assertEqual(commands[1], ["python3", "/tmp/prev/install/release.py"])
        self.assertEqual(commands[2], self.THIS + ["--dry-run"])
        self.assertIn("server-side dry-run", summary)

    def test_dry_run_without_a_release_dry_runs_this_commit(self):
        commands, summary = gate.install_plan(None, "/tmp/prev", fresh=False, dry_run=True)
        self.assertEqual(commands, [self.THIS + ["--dry-run"]])
        self.assertIn("No release to upgrade from", summary)

    def test_an_unknown_install_option_exits_2(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(gate.main(["gate.py", "install", "--upgrade"]), 2)


TEMPLATE = """KUBE_CONTEXT=
NS_CATALOG_POLARIS=catalog-polaris
NS_STANDINS=standins
S3_ENDPOINT=
S3_REGION=
POLARIS_WAREHOUSE_BUCKET=
POLARIS_OPA_URL=
POLARIS_S3_ACCESS_KEY_ID=
POLARIS_S3_SECRET_ACCESS_KEY=
POLARIS_DB_PASSWORD=
"""


class GateEnv(unittest.TestCase):
    def test_the_gate_context_and_the_stand_in_credentials_are_set(self):
        env = gate.init_env.parse(gate.gate_env(TEMPLATE, gen=lambda name: f"gen-{name}"))
        self.assertEqual(env["KUBE_CONTEXT"], gate.CONTEXT)
        self.assertEqual(env["POLARIS_S3_ACCESS_KEY_ID"], "gen-POLARIS_S3_ACCESS_KEY_ID")
        self.assertEqual(env["POLARIS_S3_SECRET_ACCESS_KEY"],
                         "gen-POLARIS_S3_SECRET_ACCESS_KEY")

    def test_the_endpoints_name_the_stand_ins_in_their_namespace(self):
        env = gate.init_env.parse(gate.gate_env(TEMPLATE.replace("NS_STANDINS=standins",
                                                                 "NS_STANDINS=si")))
        self.assertEqual(env["S3_ENDPOINT"], "http://seaweedfs.si.svc.cluster.local:8333")
        self.assertEqual(env["S3_REGION"], "us-east-1")
        self.assertEqual(env["POLARIS_WAREHOUSE_BUCKET"], "polaris-warehouse")
        self.assertEqual(env["POLARIS_OPA_URL"],
                         "http://opa.si.svc.cluster.local:8181/v1/data/polaris/authz")

    def test_everything_else_is_left_to_init_env(self):
        env = gate.init_env.parse(gate.gate_env(TEMPLATE, gen=lambda name: "x"))
        self.assertEqual(env["POLARIS_DB_PASSWORD"], "")
        self.assertEqual(env["NS_CATALOG_POLARIS"], "catalog-polaris")

    def test_the_committed_template_has_every_variable_the_gate_sets(self):
        with open(os.path.join(HERE, "..", ".env.template"), encoding="utf-8") as f:
            env = gate.init_env.parse(gate.gate_env(f.read()))
        for name in gate.GATE_ENV_GENERATED + ("KUBE_CONTEXT", "S3_ENDPOINT", "S3_REGION",
                                               "POLARIS_WAREHOUSE_BUCKET", "POLARIS_OPA_URL"):
            with self.subTest(name=name):
                self.assertTrue(env[name])

    def test_an_existing_env_file_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, ".env")
            with open(path, "w", encoding="utf-8") as f:
                f.write("KUBE_CONTEXT=someone-elses\n")
            with self.assertRaises(SystemExit):
                gate.write_gate_env(path, TEMPLATE)
            with open(path, encoding="utf-8") as f:
                self.assertEqual(f.read(), "KUBE_CONTEXT=someone-elses\n")

    def test_the_env_file_is_written_for_its_owner_only(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, ".env")
            gate.write_gate_env(path, TEMPLATE)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)


IMAGE = "ghcr.io/cloudnative-pg/postgresql:16.15-1-minimal-trixie@sha256:" + "a" * 64
VALUES = {"instances": 3, "image": IMAGE, "podAntiAffinityType": "required"}


def cluster(instances=3, image=IMAGE, ready=3, anti="required"):
    return {"spec": {"instances": instances, "imageName": image,
                     "affinity": {"podAntiAffinityType": anti}},
            "status": {"readyInstances": ready}}


def pods(*nodes):
    return {"items": [{"metadata": {"name": f"polaris-pg-{i + 1}"}, "spec": {"nodeName": n}}
                      for i, n in enumerate(nodes)]}


class PolarisPg(unittest.TestCase):
    """The running Cluster against the chart's values (ADR-016)."""

    def test_as_installed_passes_and_names_the_nodes(self):
        problems, notes = gate.polaris_pg_problems(VALUES, cluster(), pods("a", "b", "c"))
        self.assertEqual(problems, [])
        self.assertIn("3 nodes", "\n".join(notes))

    def test_a_different_instance_count_is_found(self):
        problems, _ = gate.polaris_pg_problems(VALUES, cluster(instances=1, ready=1), pods("a"))
        self.assertIn("instances", "\n".join(problems))

    def test_a_different_image_is_found(self):
        problems, _ = gate.polaris_pg_problems(VALUES, cluster(image="postgres:16"),
                                               pods("a", "b", "c"))
        self.assertIn("image", "\n".join(problems))

    def test_instances_not_ready_are_found(self):
        problems, _ = gate.polaris_pg_problems(VALUES, cluster(ready=2), pods("a", "b", "c"))
        self.assertIn("ready", "\n".join(problems))

    def test_a_different_anti_affinity_is_found(self):
        problems, _ = gate.polaris_pg_problems(VALUES, cluster(anti="preferred"),
                                               pods("a", "b", "c"))
        self.assertIn("podAntiAffinityType", "\n".join(problems))

    def test_two_instances_on_one_node_are_found(self):
        problems, _ = gate.polaris_pg_problems(VALUES, cluster(), pods("a", "a", "c"))
        self.assertIn("nodes", "\n".join(problems))

    def test_the_check_reads_the_release_and_the_cluster_in_its_namespace(self):
        commands = gate.polaris_pg_commands("catalog-polaris")
        self.assertEqual([c[0] for c in commands], ["helm", "kubectl", "kubectl"])
        for argv in commands:
            self.assertEqual(argv[argv.index("-n") + 1], "catalog-polaris")
            self.assertIn(gate.CONTEXT, argv)
        self.assertIn("--all", commands[0])
        self.assertIn("cluster/polaris-pg", commands[1])
        self.assertIn("cnpg.io/cluster=polaris-pg,cnpg.io/podRole=instance", commands[2])


def git(repo, *args):
    return subprocess.run(["git", "-C", repo, "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          capture_output=True, text=True, check=True).stdout.strip()


class ReleasedTags(unittest.TestCase):
    """main -> rc -> tag (ADR-033): a tag sits on an rc merge commit that main never contains."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = r = self.tmp.name
        git(r, "init", "-q", "-b", "main")
        git(r, "commit", "-q", "--allow-empty", "-m", "base")
        git(r, "branch", "rc")
        git(r, "commit", "-q", "--allow-empty", "-m", "tested on main")
        git(r, "checkout", "-q", "rc")
        git(r, "merge", "-q", "--no-ff", "-m", "candidate", "main")
        git(r, "tag", "platform-2026.10.10")
        git(r, "checkout", "-q", "main")

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_later_main_commit_includes_the_release_it_was_tested_as(self):
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "later on main")
        self.assertEqual(gate.released_tags(self.repo), ["platform-2026.10.10"])

    def test_a_commit_on_rc_includes_the_tag_itself(self):
        git(self.repo, "checkout", "-q", "rc")
        self.assertEqual(gate.released_tags(self.repo), ["platform-2026.10.10"])

    def test_a_commit_older_than_the_released_one_does_not(self):
        git(self.repo, "checkout", "-q", "main~1")
        self.assertEqual(gate.released_tags(self.repo), [])

    def test_no_tags_gives_none(self):
        git(self.repo, "tag", "-d", "platform-2026.10.10")
        self.assertEqual(gate.released_tags(self.repo), [])


STANDIN_ENV = {"NS_STANDINS": "standins", "NS_CATALOG_POLARIS": "catalog-polaris",
               "S3_ENDPOINT": "http://seaweedfs.standins.svc.cluster.local:8333",
               "S3_REGION": "us-east-1", "POLARIS_WAREHOUSE_BUCKET": "polaris-warehouse",
               "POLARIS_OPA_URL": "http://opa.standins.svc.cluster.local:8181/v1/data/polaris/authz"}
CURL = "curlimages/curl:8.22.0@sha256:" + "c" * 64


class Standins(unittest.TestCase):
    def test_applies_the_list_server_side_then_waits_for_both_deployments(self):
        apply, wait = gate.standins_commands(STANDIN_ENV)
        self.assertEqual(apply[:3], ["kubectl", "--context", gate.CONTEXT])
        self.assertIn("--server-side", apply)
        self.assertEqual(apply[-2:], ["-f", "-"])
        self.assertEqual(wait[:5], ["kubectl", "--context", gate.CONTEXT, "-n", "standins"])
        self.assertIn("deployment/seaweedfs", wait)
        self.assertIn("deployment/opa", wait)


class CheckStandins(unittest.TestCase):
    CASES = [{"name": "root", "allow": True, "input": {"actor": {"principal": "root"}}},
             {"name": "stranger", "allow": False, "input": {"actor": {"principal": "x"}}}]

    def command(self):
        return gate.check_standins_command(STANDIN_ENV, self.CASES, CURL, name="check-test")

    def test_a_short_lived_curl_pod_runs_in_polaris_namespace(self):
        argv = self.command()
        self.assertEqual(argv[:7], ["kubectl", "--context", gate.CONTEXT, "-n",
                                    "catalog-polaris", "run", "check-test"])
        for flag in ("--rm", "-i", "--restart=Never", f"--image={CURL}"):
            self.assertIn(flag, argv)

    def test_the_s3_credentials_come_from_polaris_secret_never_the_command_line(self):
        argv = self.command()
        overrides = json.loads(argv[argv.index("--overrides") + 1])
        (container,) = overrides["spec"]["containers"]
        self.assertEqual(container["name"], "check-test")
        refs = {e["name"]: e["valueFrom"]["secretKeyRef"] for e in container["env"]}
        self.assertEqual(refs["AWS_ACCESS_KEY_ID"], {"name": "polaris-s3", "key": "accessKeyId"})
        self.assertEqual(refs["AWS_SECRET_ACCESS_KEY"],
                         {"name": "polaris-s3", "key": "secretAccessKey"})

    def test_the_override_merges_into_the_generated_container_instead_of_replacing_it(self):
        # A merge patch (kubectl's default) replaces the containers list whole, so the pod
        # would lose its `sh -s` command and run curl's entrypoint (smoke 38121510918).
        self.assertIn("--override-type=strategic", self.command())

    def test_the_script_reads_stdin_with_the_endpoints_then_each_case(self):
        argv = self.command()
        tail = argv[argv.index("sh"):]
        self.assertEqual(tail[:7], ["sh", "-s", "--", STANDIN_ENV["S3_ENDPOINT"],
                                    "polaris-warehouse", "us-east-1",
                                    STANDIN_ENV["POLARIS_OPA_URL"]])
        self.assertEqual(tail[7:9], ["root", "true"])
        self.assertEqual(json.loads(tail[9]), self.CASES[0]["input"])
        self.assertEqual(tail[10:12], ["stranger", "false"])

    def test_the_committed_cases_load(self):
        cases = gate.standins.load_cases(os.path.join(HERE, "opa", "cases.json"))
        self.assertGreater(len(cases), 1)


if __name__ == "__main__":
    unittest.main()
