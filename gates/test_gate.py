"""Tests for gate.py. Run: python3 -m unittest discover -s gates"""

import contextlib
import hashlib
import io
import json
import re
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

    def test_cert_manager_and_the_plugin_are_pinned_release_manifests(self):
        # ADR-043 §3: the gate installs both, each pinned with its digest.
        manifests = gate.load_pins(os.path.join(HERE, "pins.json"))["manifests"]
        self.assertEqual(sorted(manifests), ["barman_cloud_plugin", "cert_manager"])
        for name, pin in manifests.items():
            with self.subTest(manifest=name):
                self.assertRegex(pin["sha256"], r"^[0-9a-f]{64}$")
                self.assertRegex(pin["version"], r"^v\d+\.\d+\.\d+$")
                self.assertTrue(pin["url"].startswith("https://github.com/"))
                self.assertIn(f"/download/{pin['version']}/", pin["url"])
                self.assertTrue(pin["url"].endswith(".yaml"))
                self.assertTrue(pin["source"])

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
    MANIFESTS = {"cert_manager": "cert-manager manifest",
                 "barman_cloud_plugin": "plugin manifest"}

    def commands(self):
        return gate.up_commands(self.PINS, "/tmp/chart.tgz", "cnpg-system", self.MANIFESTS)

    def helm_index(self, steps):
        return next(i for i, (argv, _) in enumerate(steps) if argv[0] == "helm")

    def test_up_creates_the_three_node_cluster_first(self):
        (create, stdin) = self.commands()[0]
        self.assertIsNone(stdin)
        self.assertEqual(create[:4], ["kind", "create", "cluster", "--name"])
        self.assertEqual(create[create.index("--name") + 1], gate.CLUSTER)
        self.assertEqual(create[create.index("--image") + 1], self.PINS["node_image"]["image"])
        self.assertTrue(create[create.index("--config") + 1].endswith("gates/kind.yaml"))

    def test_up_installs_cnpg_from_the_chart_archive_and_waits(self):
        steps = self.commands()
        helm, stdin = steps[self.helm_index(steps)]
        self.assertIsNone(stdin)
        self.assertEqual(helm[:3], ["helm", "--kube-context", gate.CONTEXT])
        self.assertIn("/tmp/chart.tgz", helm)
        self.assertEqual(helm[helm.index("--namespace") + 1], "cnpg-system")
        self.assertIn("--wait", helm)

    def test_after_cnpg_cert_manager_then_the_plugin_are_applied_and_waited_for(self):
        # ADR-043 §3: cert-manager in its own namespace, then the plugin's manifest, which
        # needs cert-manager's webhook for its Issuer and Certificates, in the operator's.
        steps = self.commands()
        after = steps[self.helm_index(steps) + 1:]
        self.assertEqual(len(after), 4)
        (cm_apply, cm_text), (cm_wait, w1), (pl_apply, pl_text), (pl_wait, w2) = after
        for apply in (cm_apply, pl_apply):
            self.assertEqual(apply, ["kubectl", "--context", gate.CONTEXT, "apply",
                                     "--server-side", "--field-manager=gate", "-f", "-"])
        self.assertEqual((cm_text, pl_text), ("cert-manager manifest", "plugin manifest"))
        self.assertEqual((w1, w2), (None, None))
        self.assertEqual(cm_wait, ["kubectl", "--context", gate.CONTEXT, "-n",
                                   gate.CERT_MANAGER_NAMESPACE, "rollout", "status",
                                   "deployment/cert-manager",
                                   "deployment/cert-manager-cainjector",
                                   "deployment/cert-manager-webhook", "--timeout=5m"])
        self.assertEqual(pl_wait, ["kubectl", "--context", gate.CONTEXT, "-n", "cnpg-system",
                                   "rollout", "status", f"deployment/{gate.PLUGIN_DEPLOYMENT}",
                                   "--timeout=5m"])

    def test_up_lets_the_control_plane_take_pods_before_cnpg(self):
        taint, _ = self.commands()[1]
        self.assertEqual(taint, ["kubectl", "--context", gate.CONTEXT, "taint", "nodes",
                                 f"{gate.CLUSTER}-control-plane",
                                 "node-role.kubernetes.io/control-plane:NoSchedule-"])

    def test_up_gives_kinds_provisioner_a_shared_class_and_keeps_standard(self):
        # ADR-036: ReadWriteMany on kind, from local-path-provisioner's sharedFileSystemPath.
        (patch, _), = [c for c in self.commands() if "patch" in c[0]]
        self.assertEqual(patch[patch.index("-n") + 1], "local-path-storage")
        self.assertEqual(patch[patch.index("patch"):patch.index("patch") + 3],
                         ["patch", "configmap", "local-path-config"])
        data = json.loads(patch[patch.index("-p") + 1])["data"]
        self.assertEqual(list(data), ["config.json"])
        config = json.loads(data["config.json"])
        self.assertEqual(config["nodePathMap"], [])
        self.assertEqual(config["storageClassConfigs"], {
            "standard": {"nodePathMap": [{"node": "DEFAULT_PATH_FOR_NON_LISTED_NODES",
                                          "paths": ["/var/local-path-provisioner"]}]},
            gate.SHARED_CLASS: {"sharedFileSystemPath": gate.SHARED_NODE_DIR}})

    def test_the_shared_class_is_applied_and_the_provisioner_restarted_before_cnpg(self):
        steps = self.commands()
        helm = self.helm_index(steps)
        (apply, stdin), = [c for c in steps[:helm] if "apply" in c[0]]
        storage_class = json.loads(stdin)
        self.assertEqual((storage_class["kind"], storage_class["metadata"]["name"]),
                         ("StorageClass", gate.SHARED_CLASS))
        self.assertEqual(storage_class["provisioner"], "rancher.io/local-path")
        self.assertEqual(storage_class["reclaimPolicy"], "Delete")
        names = [" ".join(argv) for argv, _ in steps]
        restart = next(i for i, n in enumerate(names) if "rollout restart" in n)
        status = next(i for i, n in enumerate(names) if "rollout status" in n)
        self.assertIn("deployment/local-path-provisioner", names[restart])
        self.assertLess(restart, status)
        self.assertLess(status, helm)
        for argv, _ in steps[1:]:
            if argv[0] == "kubectl":
                self.assertEqual(argv[1:3], ["--context", gate.CONTEXT])

    def test_the_plugin_manifest_must_name_only_the_operators_namespace(self):
        # The release manifest names its namespace itself; applied elsewhere, the plugin would
        # not run next to the operator (ADR-043 §3).
        text = "kind: Deployment\nmetadata:\n  name: barman-cloud\n  namespace: cnpg-system\n"
        self.assertEqual(gate.plugin_manifest(text, "cnpg-system"), text)
        with self.assertRaisesRegex(ValueError, "cnpg-system.*operators"):
            gate.plugin_manifest(text, "operators")
        with self.assertRaisesRegex(ValueError, "no namespace"):
            gate.plugin_manifest("kind: Deployment\n", "cnpg-system")

    def test_the_kind_config_has_one_control_plane_and_two_workers(self):
        with open(os.path.join(HERE, "kind.yaml"), encoding="utf-8") as f:
            roles = [line.split(":", 1)[1].strip() for line in f if "role:" in line]
        self.assertEqual(roles, ["control-plane", "worker", "worker"])

    def test_every_node_mounts_the_same_host_directory_for_the_shared_class(self):
        with open(os.path.join(HERE, "kind.yaml"), encoding="utf-8") as f:
            text = f.read()
        self.assertEqual(text.count(f"hostPath: {gate.SHARED_HOST_DIR}"), 3)
        self.assertEqual(text.count(f"containerPath: {gate.SHARED_NODE_DIR}"), 3)


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
POLARIS_DB_HOST=
POLARIS_CONSOLE_ENABLED=false
POLARIS_CONSOLE_ORIGIN=
POLARIS_CONSOLE_API_URL=
POLARIS_LOGS_STORAGE_CLASS=
POLARIS_LOGS_SIZE=
POLARIS_BACKUP_BUCKET=
POLARIS_S3_ACCESS_KEY_ID=
POLARIS_S3_SECRET_ACCESS_KEY=
SMOKE_S3_ACCESS_KEY_ID=
SMOKE_S3_SECRET_ACCESS_KEY=
POLARIS_BACKUP_S3_ACCESS_KEY_ID=
POLARIS_BACKUP_S3_SECRET_ACCESS_KEY=
POLARIS_DB_PASSWORD=
"""


class BatchImage(unittest.TestCase):
    """ADR-042: the gate builds the batch's image and loads it into kind under the name
    release.py installs, from IMAGE_REGISTRY or GHCR."""

    def test_builds_and_loads_the_image_under_the_name_release_installs(self):
        mirror = "mirror.example.invalid/platform"
        build, load = gate.batch_image_commands(registry=mirror)
        image = gate.release.batch_image(registry=mirror)
        self.assertEqual(build[:2], ["docker", "build"])
        self.assertEqual(build[build.index("--tag") + 1], image)
        self.assertEqual(load, ["kind", "load", "docker-image", image, "--name", gate.CLUSTER])

    def test_the_default_is_the_github_container_registry(self):
        build, _ = gate.batch_image_commands()
        self.assertTrue(build[build.index("--tag") + 1].startswith(
            "ghcr.io/enjay27/lakehouse/polaris-log-batch:"))

    def test_the_gates_env_from_the_template_leaves_the_registry_empty(self):
        with open(os.path.join(gate.ROOT, ".env.template"), encoding="utf-8") as f:
            env = gate.init_env.parse(gate.gate_env(f.read(), gen=lambda name: "x"))
        self.assertEqual(env["IMAGE_REGISTRY"], "")
        self.assertEqual(gate.release.image_registry(env), gate.release.DEFAULT_IMAGE_REGISTRY)


class GateEnv(unittest.TestCase):
    def test_the_gate_context_and_the_stand_in_credentials_are_set(self):
        env = gate.init_env.parse(gate.gate_env(TEMPLATE, gen=lambda name: f"gen-{name}"))
        self.assertEqual(env["KUBE_CONTEXT"], gate.CONTEXT)
        self.assertEqual(env["POLARIS_S3_ACCESS_KEY_ID"], "gen-POLARIS_S3_ACCESS_KEY_ID")
        self.assertEqual(env["POLARIS_S3_SECRET_ACCESS_KEY"],
                         "gen-POLARIS_S3_SECRET_ACCESS_KEY")
        self.assertEqual(env["SMOKE_S3_ACCESS_KEY_ID"], "gen-SMOKE_S3_ACCESS_KEY_ID")
        self.assertEqual(env["SMOKE_S3_SECRET_ACCESS_KEY"], "gen-SMOKE_S3_SECRET_ACCESS_KEY")
        self.assertEqual(env["POLARIS_BACKUP_S3_ACCESS_KEY_ID"],
                         "gen-POLARIS_BACKUP_S3_ACCESS_KEY_ID")
        self.assertEqual(env["POLARIS_BACKUP_S3_SECRET_ACCESS_KEY"],
                         "gen-POLARIS_BACKUP_S3_SECRET_ACCESS_KEY")

    def test_the_endpoints_name_the_recorder_in_the_stand_ins_namespace(self):
        env = gate.init_env.parse(gate.gate_env(TEMPLATE.replace("NS_STANDINS=standins",
                                                                 "NS_STANDINS=si")))
        self.assertEqual(env["S3_ENDPOINT"], "http://recorder.si.svc.cluster.local:8333")
        self.assertEqual(env["S3_REGION"], "us-east-1")
        self.assertEqual(env["POLARIS_WAREHOUSE_BUCKET"], "polaris-warehouse")
        self.assertEqual(env["POLARIS_BACKUP_BUCKET"], "polaris-backup")
        self.assertEqual(env["POLARIS_OPA_URL"],
                         "http://recorder.si.svc.cluster.local:8181/v1/data/polaris/authz")
        self.assertEqual(env["POLARIS_DB_HOST"], "recorder.si.svc.cluster.local")

    def test_the_console_is_enabled_at_its_service_and_reaches_polaris_at_its_service(self):
        env = gate.init_env.parse(gate.gate_env(TEMPLATE, gen=lambda name: "x"))
        self.assertEqual(env["POLARIS_CONSOLE_ENABLED"], "true")
        self.assertEqual(env["POLARIS_CONSOLE_ORIGIN"],
                         "http://polaris-console.catalog-polaris.svc.cluster.local:8080")
        self.assertEqual(env["POLARIS_CONSOLE_API_URL"],
                         "http://polaris.catalog-polaris.svc.cluster.local:8181")

    def test_the_log_claim_uses_the_shared_class(self):
        env = gate.init_env.parse(gate.gate_env(TEMPLATE, gen=lambda name: "x"))
        self.assertEqual(env["POLARIS_LOGS_STORAGE_CLASS"], gate.SHARED_CLASS)
        self.assertEqual(env["POLARIS_LOGS_SIZE"], "1Gi")

    def test_everything_else_is_left_to_init_env(self):
        env = gate.init_env.parse(gate.gate_env(TEMPLATE, gen=lambda name: "x"))
        self.assertEqual(env["POLARIS_DB_PASSWORD"], "")
        self.assertEqual(env["NS_CATALOG_POLARIS"], "catalog-polaris")

    def test_the_committed_template_has_every_variable_the_gate_sets(self):
        with open(os.path.join(HERE, "..", ".env.template"), encoding="utf-8") as f:
            env = gate.init_env.parse(gate.gate_env(f.read()))
        for name in gate.GATE_ENV_GENERATED + ("KUBE_CONTEXT", "S3_ENDPOINT", "S3_REGION",
                                               "POLARIS_WAREHOUSE_BUCKET", "POLARIS_OPA_URL",
                                               "POLARIS_BACKUP_BUCKET", "POLARIS_DB_HOST", "POLARIS_CONSOLE_ORIGIN",
                                               "POLARIS_CONSOLE_API_URL",
                                               "POLARIS_LOGS_STORAGE_CLASS", "POLARIS_LOGS_SIZE"):
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


POLARIS_ENV = {"NS_CATALOG_POLARIS": "catalog-polaris", "POLARIS_DB_HOST": "recorder.standins.svc",
               "POLARIS_OPA_URL": "http://recorder.standins.svc:8181/v1/data/polaris/authz"}
POLARIS_IMAGE = "apache/polaris:1.6.0@sha256:" + "d" * 64
POLARIS_VALUES = {
    "replicaCount": 2,
    "image": {"repository": "apache/polaris", "tag": POLARIS_IMAGE.split(":", 1)[1]},
    "persistence": {"type": "relational-jdbc"},
    "authentication": {"tokenBroker": {"type": "symmetric-key"}},
    "advancedConfig": {
        "polaris.authorization.type": "opa",
        "polaris.authorization.opa.policy-uri": POLARIS_ENV["POLARIS_OPA_URL"],
        "quarkus.datasource.jdbc.url":
            "jdbc:postgresql://recorder.standins.svc:5432/polaris?sslmode=require"},
}


def polaris_properties(**changes):
    props = {"polaris.persistence.type": "relational-jdbc",
             "polaris.authorization.type": "opa",
             "polaris.authorization.opa.policy-uri": POLARIS_ENV["POLARIS_OPA_URL"],
             "quarkus.datasource.jdbc.url":
                 "jdbc:postgresql://recorder.standins.svc:5432/polaris?sslmode=require",
             "polaris.authentication.token-broker.type": "symmetric-key", **changes}
    text = "\n".join(f"{k}={v}" for k, v in props.items())
    return {"data": {"application.properties": text}}


POLARIS_S3_ENV = [{"name": var, "valueFrom": {"secretKeyRef": {"name": "polaris-s3", "key": key}}}
                  for var, key in (("AWS_ACCESS_KEY_ID", "accessKeyId"),
                                   ("AWS_SECRET_ACCESS_KEY", "secretAccessKey"))]


def polaris_deployment(replicas=2, ready=2, image=POLARIS_IMAGE, env=POLARIS_S3_ENV):
    container = {"name": "polaris", "image": image, "env": env}
    return {"spec": {"replicas": replicas,
                     "template": {"spec": {"containers": [container]}}},
            "status": {"readyReplicas": ready}}


def polaris_pods(*nodes):
    return {"items": [{"metadata": {"name": f"polaris-{i}"},
                       "spec": {"nodeName": n},
                       "status": {"phase": "Running", "podIP": f"10.0.0.{i + 1}"}}
                      for i, n in enumerate(nodes)]}


class Polaris(unittest.TestCase):
    """The running Polaris against the values it was installed with (ADR-016)."""

    def problems(self, values=POLARIS_VALUES, deployment=None, pods=None, configmap=None,
                 env=POLARIS_ENV):
        return gate.polaris_problems(values, deployment or polaris_deployment(),
                                     pods or polaris_pods("a", "b"),
                                     configmap or polaris_properties(), env)

    def test_as_installed_passes_and_names_the_nodes_and_settings(self):
        problems, notes = self.problems()
        self.assertEqual(problems, [])
        text = "\n".join(notes)
        for part in ("2 replicas ready on 2 nodes (a, b)", POLARIS_IMAGE, "relational-jdbc",
                     "symmetric-key", "opa", POLARIS_ENV["POLARIS_OPA_URL"],
                     "S3 key from Secret polaris-s3"):
            self.assertIn(part, text)

    def test_a_different_replica_count_is_found(self):
        problems, _ = self.problems(deployment=polaris_deployment(replicas=1, ready=1),
                                    pods=polaris_pods("a"))
        self.assertIn("replicas", "\n".join(problems))

    def test_replicas_not_ready_are_found(self):
        problems, _ = self.problems(deployment=polaris_deployment(ready=1))
        self.assertIn("ready", "\n".join(problems))

    def test_a_different_image_is_found(self):
        problems, _ = self.problems(deployment=polaris_deployment(image="apache/polaris:1.6.0"))
        self.assertIn("image", "\n".join(problems))

    def test_two_replicas_on_one_node_are_found(self):
        problems, _ = self.problems(pods=polaris_pods("a", "a"))
        self.assertIn("nodes", "\n".join(problems))

    def test_each_running_setting_is_compared_with_the_values(self):
        for key, other in (("polaris.persistence.type", "in-memory"),
                           ("polaris.authorization.type", "internal"),
                           ("polaris.authorization.opa.policy-uri", "http://elsewhere/v1"),
                           ("quarkus.datasource.jdbc.url", "jdbc:postgresql://x:5432/polaris"),
                           ("polaris.authentication.token-broker.type", "rsa-key-pair")):
            with self.subTest(key=key):
                problems, _ = self.problems(configmap=polaris_properties(**{key: other}))
                self.assertIn(key, "\n".join(problems))

    def test_polaris_without_its_s3_key_for_the_aws_sdk_is_found(self):
        for env in ([], POLARIS_S3_ENV[:1],
                    [{"name": "AWS_ACCESS_KEY_ID", "valueFrom": {"secretKeyRef": {
                        "name": "smoke-s3", "key": "accessKeyId"}}}, POLARIS_S3_ENV[1]]):
            with self.subTest(env=env):
                problems, _ = self.problems(deployment=polaris_deployment(env=env))
                self.assertIn("AWS_", "\n".join(problems))

    def test_values_that_do_not_name_the_endpoints_in_env_are_found(self):
        env = {**POLARIS_ENV, "POLARIS_DB_HOST": "polaris-pg-rw",
               "POLARIS_OPA_URL": "http://opa.standins.svc:8181/v1/data/polaris/authz"}
        problems, _ = self.problems(env=env)
        text = "\n".join(problems)
        self.assertIn("quarkus.datasource.jdbc.url", text)
        self.assertIn("polaris.authorization.opa.policy-uri", text)

    def test_each_running_pod_is_asked_at_its_own_address(self):
        pods = polaris_pods("a", "b")
        pods["items"].append({"metadata": {"name": "polaris-old"}, "spec": {"nodeName": "c"},
                              "status": {"phase": "Succeeded", "podIP": "10.0.0.9"}})
        self.assertEqual(gate.polaris_pod_urls(pods),
                         ["http://10.0.0.1:8181", "http://10.0.0.2:8181"])

    def test_the_check_reads_the_release_and_its_objects_in_its_namespace(self):
        commands = gate.polaris_commands("catalog-polaris")
        self.assertEqual([c[0] for c in commands], ["helm", "kubectl", "kubectl", "kubectl"])
        for argv in commands:
            self.assertEqual(argv[argv.index("-n") + 1], "catalog-polaris")
            self.assertIn(gate.CONTEXT, argv)
        self.assertEqual(commands[0][commands[0].index("values") + 1], "polaris")
        self.assertIn("--all", commands[0])
        self.assertIn("deployment/polaris", commands[1])
        self.assertIn("app.kubernetes.io/name=polaris,app.kubernetes.io/instance=polaris",
                      commands[2])
        self.assertIn("configmap/polaris", commands[3])

    def test_the_check_pod_gets_roots_credentials_from_their_secret(self):
        argv = gate.check_polaris_command(POLARIS_ENV, "curl:1@sha256:" + "e" * 64, "check-p",
                                          ["http://10.0.0.1:8181", "http://10.0.0.2:8181"])
        self.assertEqual(argv[:7], ["kubectl", "--context", gate.CONTEXT, "-n", "catalog-polaris",
                                    "run", "check-p"])
        overrides = json.loads(argv[argv.index("--overrides") + 1])
        (container,) = overrides["spec"]["containers"]
        env = {e["name"]: e["valueFrom"]["secretKeyRef"] for e in container["env"]}
        self.assertEqual(env, {"ROOT_CLIENT_ID": {"name": "polaris-root", "key": "clientId"},
                               "ROOT_CLIENT_SECRET": {"name": "polaris-root",
                                                      "key": "clientSecret"}})
        tail = argv[argv.index("--") + 1:]
        self.assertEqual(tail, ["sh", "-s", "--", "http://polaris:8181", "http://polaris-mgmt:8182",
                                "POLARIS", "http://10.0.0.1:8181", "http://10.0.0.2:8181"])


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
               "POLARIS_BACKUP_BUCKET": "polaris-backup",
               "POLARIS_OPA_URL": "http://opa.standins.svc.cluster.local:8181/v1/data/polaris/authz",
               "POLARIS_DB_HOST": "recorder.standins.svc.cluster.local"}
CURL = "curlimages/curl:8.22.0@sha256:" + "c" * 64


PYTHON = "python:3.12.14-slim@sha256:" + "f" * 64


class CheckIceberg(unittest.TestCase):
    def commands(self):
        return gate.check_iceberg_commands(STANDIN_ENV, PYTHON, name="iceberg-test")

    def test_the_configmap_holds_the_script_and_its_requirements_in_polaris_namespace(self):
        manifest = gate.iceberg_configmap(STANDIN_ENV, "# smoke.py\n", "pyiceberg==0.12.0\n")
        self.assertEqual(manifest["kind"], "ConfigMap")
        self.assertEqual(manifest["metadata"]["name"], "iceberg-smoke")
        self.assertEqual(manifest["metadata"]["namespace"], "catalog-polaris")
        self.assertEqual(manifest["data"], {"smoke.py": "# smoke.py\n",
                                            "requirements.txt": "pyiceberg==0.12.0\n"})
        apply, _ = self.commands()
        self.assertEqual(apply, ["kubectl", "--context", gate.CONTEXT, "-n", "catalog-polaris",
                                 "apply", "--server-side", "--field-manager=gate", "-f", "-"])

    def test_a_short_lived_pod_on_the_pinned_python_runs_in_polaris_namespace(self):
        _, argv = self.commands()
        self.assertEqual(argv[:7], ["kubectl", "--context", gate.CONTEXT, "-n",
                                    "catalog-polaris", "run", "iceberg-test"])
        for flag in ("--rm", "--attach", "--restart=Never", f"--image={PYTHON}",
                     "--override-type=strategic"):
            self.assertIn(flag, argv)

    def test_credentials_come_from_their_secrets_never_the_command_line(self):
        _, argv = self.commands()
        overrides = json.loads(argv[argv.index("--overrides") + 1])
        (container,) = overrides["spec"]["containers"]
        self.assertEqual(container["name"], "iceberg-test")
        refs = {e["name"]: e["valueFrom"]["secretKeyRef"] for e in container["env"]
                if "valueFrom" in e}
        self.assertEqual(refs, {
            "ROOT_CLIENT_ID": {"name": "polaris-root", "key": "clientId"},
            "ROOT_CLIENT_SECRET": {"name": "polaris-root", "key": "clientSecret"},
            "S3_ACCESS_KEY_ID": {"name": "smoke-s3", "key": "accessKeyId"},
            "S3_SECRET_ACCESS_KEY": {"name": "smoke-s3", "key": "secretAccessKey"}})
        self.assertIn({"name": "code", "mountPath": "/smoke", "readOnly": True},
                      container["volumeMounts"])
        self.assertEqual(overrides["spec"]["volumes"],
                         [{"name": "code", "configMap": {"name": "iceberg-smoke"}}])

    def test_pip_installs_only_hashed_wheels_then_the_script_runs_against_the_gate(self):
        _, argv = self.commands()
        tail = argv[argv.index("--command") + 2:]
        self.assertEqual(tail[:2], ["sh", "-c"])
        for part in ("pip install", "--require-hashes", "--only-binary :all:",
                     "-r /smoke/requirements.txt", "exec python3 /smoke/smoke.py"):
            self.assertIn(part, tail[2])
        self.assertEqual(tail[3:], ["smoke", "http://polaris:8181", "POLARIS", "gate_catalog",
                                    STANDIN_ENV["S3_ENDPOINT"], "us-east-1",
                                    "polaris-warehouse"])

    def test_the_committed_requirements_pin_every_package_by_hash(self):
        with open(os.path.join(HERE, "iceberg", "requirements.txt"), encoding="utf-8") as f:
            text = f.read()
        packages = re.findall(r"^([a-z0-9-]+)==([^ ]+) \\$", text, re.MULTILINE)
        self.assertIn(("pyiceberg", "0.12.0"), packages)
        self.assertIn(("pyarrow", "25.0.1"), packages)
        blocks = re.split(r"^(?=[a-z0-9-]+==)", text, flags=re.MULTILINE)[1:]
        self.assertEqual(len(blocks), len(packages))
        for block in blocks:
            self.assertRegex(block, r"--hash=sha256:[0-9a-f]{64}")

    def test_the_python_image_is_the_pinned_one_the_recorder_runs_on(self):
        self.assertEqual(gate.smoke_python_image(gate.load_pins()),
                         gate.load_pins()["standin_images"]["recorder"]["image"])


CONSOLE_ENV = {**STANDIN_ENV, "POLARIS_CONSOLE_ENABLED": "true",
               "POLARIS_CONSOLE_ORIGIN": "http://polaris-console.catalog-polaris.svc.cluster.local:8080",
               "POLARIS_CONSOLE_API_URL": "http://polaris.catalog-polaris.svc.cluster.local:8181"}


class CheckConsole(unittest.TestCase):
    def command(self):
        return gate.check_console_command(CONSOLE_ENV, PYTHON, name="console-test")

    def test_a_short_lived_pod_on_the_pinned_python_reads_the_check_on_stdin(self):
        argv = self.command()
        self.assertEqual(argv[:7], ["kubectl", "--context", gate.CONTEXT, "-n",
                                    "catalog-polaris", "run", "console-test"])
        for flag in ("--rm", "-i", "--restart=Never", f"--image={PYTHON}",
                     "--override-type=strategic"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--command") + 1:], [
            "--", "python3", "-", CONSOLE_ENV["POLARIS_CONSOLE_ORIGIN"],
            CONSOLE_ENV["POLARIS_CONSOLE_API_URL"], "POLARIS"])

    def test_roots_credentials_come_from_their_secret_never_the_command_line(self):
        argv = self.command()
        overrides = json.loads(argv[argv.index("--overrides") + 1])
        (container,) = overrides["spec"]["containers"]
        self.assertEqual(container["name"], "console-test")
        self.assertIs(container["stdin"], True)
        refs = {e["name"]: e["valueFrom"]["secretKeyRef"] for e in container["env"]
                if "valueFrom" in e}
        self.assertEqual(refs, {
            "ROOT_CLIENT_ID": {"name": "polaris-root", "key": "clientId"},
            "ROOT_CLIENT_SECRET": {"name": "polaris-root", "key": "clientSecret"}})

    def test_a_console_that_is_not_enabled_is_not_checked(self):
        with self.assertRaises(SystemExit):
            gate.check_console_command({**CONSOLE_ENV, "POLARIS_CONSOLE_ENABLED": "false"},
                                       PYTHON, name="console-test")


class Standins(unittest.TestCase):
    def test_applies_the_list_server_side_then_waits_for_both_deployments(self):
        (apply, stdin), (wait, _) = gate.standins_commands(
            STANDIN_ENV, "the stand-ins list")[:2]
        self.assertEqual(stdin, "the stand-ins list")
        self.assertEqual(apply[:3], ["kubectl", "--context", gate.CONTEXT])
        self.assertIn("--server-side", apply)
        self.assertEqual(apply[-2:], ["-f", "-"])
        self.assertEqual(wait[:5], ["kubectl", "--context", gate.CONTEXT, "-n", "standins"])
        for name in ("seaweedfs", "opa", "recorder"):
            self.assertIn(f"deployment/{name}", wait)

    def test_then_the_backup_bucket_is_created_in_seaweedfs_and_checked(self):
        # ADR-043 §2: `weed mini -bucket` makes one bucket; the backups get their own.
        (create, stdin), (check, none) = gate.standins_commands(
            STANDIN_ENV, "the stand-ins list")[2:]
        exec_ = ["kubectl", "--context", gate.CONTEXT, "-n", "standins", "exec"]
        self.assertEqual(create, exec_ + ["-i", "deployment/seaweedfs", "--", "weed", "shell"])
        self.assertEqual(stdin, "s3.bucket.create -name polaris-backup\n")
        self.assertEqual(check, exec_ + ["deployment/seaweedfs", "--", "curl", "-sf",
                                         "-o", "/dev/null",
                                         "http://127.0.0.1:8888/buckets/polaris-backup/"])
        self.assertIsNone(none)

    def test_the_recorder_certificate_is_made_here_for_its_service_name(self):
        with tempfile.TemporaryDirectory() as d:
            cert, key = gate.recorder_certificate(STANDIN_ENV, d)
        self.assertIn("BEGIN CERTIFICATE", cert)
        self.assertIn("PRIVATE KEY", key)


class CheckTraffic(unittest.TestCase):
    def test_reads_every_line_the_recorder_wrote(self):
        self.assertEqual(gate.recorder_logs_command(STANDIN_ENV),
                         ["kubectl", "--context", gate.CONTEXT, "-n", "standins", "logs",
                          "deployment/recorder", "--tail=-1"])

    def test_every_expectation_file_is_checked_and_any_failure_fails(self):
        log = "\n".join(json.dumps(r) for r in (
            {"proto": "http", "listener": "opa", "status": 200},
            {"proto": "pg", "event": "connect", "result": "ok"}))
        good = [{"name": "opa answered", "must": "exist", "match": {"listener": "opa"}}]
        bad = [{"name": "no pg", "must": "not_exist", "match": {"proto": "pg"}}]
        problems, lines = gate.check_traffic(log, [good])
        self.assertEqual(problems, [])
        self.assertIn("2 records: 1 HTTP, 1 PostgreSQL", lines[0])
        problems, _ = gate.check_traffic(log, [good, bad])
        self.assertEqual(len(problems), 1)


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

    def test_the_database_check_runs_psql_through_the_recorder_as_polaris(self):
        argv = gate.check_db_command(STANDIN_ENV, "pg-image@sha256:" + "d" * 64, "polaris",
                                     name="db-test")
        self.assertEqual(argv[:7], ["kubectl", "--context", gate.CONTEXT, "-n",
                                    "catalog-polaris", "run", "db-test"])
        self.assertIn("--override-type=strategic", argv)
        overrides = json.loads(argv[argv.index("--overrides") + 1])
        refs = {e["name"]: e["valueFrom"]["secretKeyRef"]
                for e in overrides["spec"]["containers"][0]["env"]}
        self.assertEqual(refs, {"PGUSER": {"name": "polaris-db", "key": "username"},
                                "PGPASSWORD": {"name": "polaris-db", "key": "password"}})
        self.assertEqual(argv[-5:], ["sh", "-s", "--", STANDIN_ENV["POLARIS_DB_HOST"],
                                     "polaris"])

    def test_the_database_image_and_name_are_the_charts(self):
        values = gate.polaris_pg_values()
        self.assertRegex(values["image"], r"@sha256:[0-9a-f]{64}$")
        self.assertEqual(values["database"], "polaris")

    def test_the_committed_cases_load(self):
        cases = gate.standins.load_cases(os.path.join(HERE, "opa", "cases.json"))
        self.assertGreater(len(cases), 1)


if __name__ == "__main__":
    unittest.main()
