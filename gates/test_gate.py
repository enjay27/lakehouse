"""Tests for gate.py. Run: python3 -m unittest discover -s gates"""

import contextlib
import copy
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
from unittest import mock

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

    def test_the_network_policy_schema_is_the_gates_kubernetes_version_at_a_commit(self):
        # ADR-048 §4: kubeconform validates the policies against it in the static gate.
        pins = gate.load_pins(os.path.join(HERE, "pins.json"))
        pin = pins["kubernetes_schemas"]["networkpolicy"]
        version = pins["node_image"]["image"].split(":")[1].split("@")[0]
        self.assertRegex(pin["sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(pin["url"], r"^https://raw\.githubusercontent\.com/yannh/"
                         r"kubernetes-json-schema/[0-9a-f]{40}/")
        self.assertTrue(pin["url"].endswith(
            f"/{version}-standalone-strict/networkpolicy-networking-v1.json"))
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

    def test_up_takes_another_kind_config_for_the_failover_measurement(self):
        steps = gate.up_commands(self.PINS, "/tmp/chart.tgz", "cnpg-system", self.MANIFESTS,
                                 kind_config="/tmp/failover-kind.yaml")
        create = steps[0][0]
        self.assertEqual(create[create.index("--config") + 1], "/tmp/failover-kind.yaml")
        self.assertEqual(steps[1:], self.commands()[1:])

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
    # ADR-048 §5: the gate-only allow policies, before the first release that installs for
    # real: the last release has network policies of its own since platform-2026.10.14.
    GATE_POLICIES = ["python3", os.path.join(HERE, "gate.py"), "gate-policies"]
    # ADR-051 §2: the seed, between the last release's install and this commit's.
    SEED = ["python3", os.path.join(HERE, "gate.py"), "seed"]

    def test_with_a_release_installs_it_from_its_tag_seeds_it_then_this_commit(self):
        commands, summary = gate.install_plan("platform-2026.10.10", "/tmp/prev", fresh=False)
        self.assertEqual(commands, [
            self.GATE_POLICIES,
            ["git", "-C", self.ROOT, "worktree", "add", "--detach", "/tmp/prev",
             "platform-2026.10.10"],
            ["cp", "-p", os.path.join(self.ROOT, ".env"), "/tmp/prev/.env"],
            ["python3", "/tmp/prev/install/release.py"],
            self.SEED,
            self.THIS,
        ])
        self.assertIn("platform-2026.10.10", summary)
        self.assertIn("ADR-051", summary)

    def test_the_release_reads_the_gates_env_in_its_worktree(self):
        # release.py reads .env from its own checkout; a tag's worktree has none, so a
        # release that reads more than step 0 would find every endpoint empty.
        commands, _ = gate.install_plan("platform-2026.10.12", "/tmp/prev", fresh=False)
        worktree, copy, previous = commands[1], commands[2], commands[3]
        self.assertEqual(worktree[3:5], ["worktree", "add"])
        self.assertEqual(copy, ["cp", "-p", os.path.join(self.ROOT, ".env"), "/tmp/prev/.env"])
        self.assertEqual(previous, ["python3", "/tmp/prev/install/release.py"])

    def test_without_a_release_installs_this_commit_and_says_so(self):
        commands, summary = gate.install_plan(None, "/tmp/prev", fresh=False)
        self.assertEqual(commands, [self.GATE_POLICIES, self.THIS])
        self.assertEqual(summary, "No release to upgrade from: fresh install (ADR-051).")

    def test_fresh_ignores_the_release(self):
        commands, summary = gate.install_plan("platform-2026.10.10", "/tmp/prev", fresh=True)
        self.assertEqual(commands, [self.GATE_POLICIES, self.THIS])
        self.assertIn("Fresh install", summary)

    def test_only_an_upgrade_that_installs_for_real_seeds(self):
        # ADR-051 §4: the dry-run seeds nothing; a fresh install has nothing to upgrade.
        for previous, fresh, dry_run in ((None, False, False), ("platform-2026.10.10", True,
                                                                False),
                                         ("platform-2026.10.10", False, True),
                                         (None, False, True)):
            with self.subTest(previous=previous, fresh=fresh, dry_run=dry_run):
                commands, _ = gate.install_plan(previous, "/tmp/prev", fresh, dry_run)
                self.assertNotIn(self.SEED, commands)

    def test_dry_run_installs_the_release_then_dry_runs_this_commit_over_it(self):
        commands, summary = gate.install_plan("platform-2026.10.10", "/tmp/prev", fresh=False,
                                              dry_run=True)
        # The last release installs for real, so the gate's policies come first; dry-run 38493145873
        # failed without them: the recorder could not reach polaris-pg for the realm bootstrap.
        self.assertEqual(commands[0], self.GATE_POLICIES)
        self.assertEqual(commands[3], ["python3", "/tmp/prev/install/release.py"])
        # The dry-run itself changes nothing.
        self.assertEqual(commands[4:], [self.THIS + ["--dry-run"]])
        self.assertIn("server-side dry-run", summary)

    def test_dry_run_without_a_release_dry_runs_this_commit(self):
        commands, summary = gate.install_plan(None, "/tmp/prev", fresh=False, dry_run=True)
        self.assertEqual(commands, [self.THIS + ["--dry-run"]])
        self.assertIn("No release to upgrade from", summary)

    def test_an_unknown_install_option_exits_2(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(gate.main(["gate.py", "install", "--upgrade"]), 2)


def pulled(message, node="lakehouse-gate-worker", at="2026-10-14T01:10:00Z", reason="Pulled"):
    return {"reason": reason, "message": message, "source": {"host": node},
            "firstTimestamp": at, "lastTimestamp": at}


class ImagePulls(unittest.TestCase):
    """#125: how long the install spent pulling images, from the kubelet's Pulled events."""
    SINCE = "2026-10-14T01:05:00+00:00"

    def test_go_durations_in_seconds(self):
        for text, seconds in (("3.162s", 3.162), ("1m2.5s", 62.5), ("512ms", 0.512),
                              ("1h0m1s", 3601.0), ("850µs", 0.00085), ("0s", 0.0)):
            self.assertAlmostEqual(gate.go_seconds(text), seconds, msg=text)
        with self.assertRaises(ValueError):
            gate.go_seconds("soon")

    def test_the_events_are_read_in_every_namespace(self):
        self.assertEqual(gate.pull_events_command(), [
            "kubectl", "--context", gate.CONTEXT, "get", "events", "--all-namespaces",
            "--field-selector", "reason=Pulled", "-o", "json"])

    def test_pulls_and_images_already_present_since_the_install_started(self):
        events = {"items": [
            pulled('Successfully pulled image "apache/polaris:1.6.0" in 1m2.5s '
                   '(1m10s including waiting). Image size: 250000000 bytes.'),
            pulled('Successfully pulled image "apache/polaris:1.6.0" in 40s '
                   '(40s including waiting). Image size: 250000000 bytes.',
                   node="lakehouse-gate-worker2"),
            # Kubelets before 1.30 print no size.
            pulled('Successfully pulled image "curlimages/curl:8" in 2.1s '
                   '(2.1s including waiting)'),
            pulled('Container image "ghcr.io/cloudnative-pg/postgresql:17" already present '
                   'on machine', node="lakehouse-gate-control-plane"),
            # Before the install (the stand-ins' own), and another reason: not counted.
            pulled('Successfully pulled image "openpolicyagent/opa:1" in 9s '
                   '(9s including waiting)', at="2026-10-14T01:04:59Z"),
            pulled('Pulling image "apache/polaris:1.6.0"', reason="Pulling"),
            pulled("something new"),
        ]}
        pulls, present, unknown = gate.image_pulls(events, self.SINCE)
        self.assertEqual(pulls, [
            {"image": "apache/polaris:1.6.0", "node": "lakehouse-gate-worker",
             "seconds": 62.5, "waiting": 70.0, "bytes": 250000000},
            {"image": "apache/polaris:1.6.0", "node": "lakehouse-gate-worker2",
             "seconds": 40.0, "waiting": 40.0, "bytes": 250000000},
            {"image": "curlimages/curl:8", "node": "lakehouse-gate-worker",
             "seconds": 2.1, "waiting": 2.1, "bytes": None},
        ])
        self.assertEqual(present, {("ghcr.io/cloudnative-pg/postgresql:17",
                                    "lakehouse-gate-control-plane")})
        self.assertEqual(unknown, 1)

    def test_the_node_comes_from_the_reporting_instance_without_a_source(self):
        event = pulled('Successfully pulled image "a:1" in 1s (1s including waiting)')
        event["source"] = {}
        event["reportingInstance"] = "lakehouse-gate-worker2"
        pulls, _, _ = gate.image_pulls({"items": [event]}, self.SINCE)
        self.assertEqual(pulls[0]["node"], "lakehouse-gate-worker2")

    def test_lines_put_the_slowest_image_first_and_the_busiest_node_against_the_install(self):
        pulls = [
            {"image": "curlimages/curl:8", "node": "w1", "seconds": 2.1, "waiting": 2.1,
             "bytes": None},
            {"image": "apache/polaris:1.6.0", "node": "w1", "seconds": 62.5, "waiting": 70.0,
             "bytes": 250000000},
            {"image": "apache/polaris:1.6.0", "node": "w2", "seconds": 40.0, "waiting": 40.0,
             "bytes": 250000000},
        ]
        present = {("postgresql:17", "cp"), ("postgresql:17", "w1"), ("curlimages/curl:8", "w2")}
        lines = gate.pull_lines(pulls, present, 0, install_seconds=300)
        self.assertEqual(lines, [
            "Image pulls during the install: 3 pulls of 2 images; the busiest node, w1, "
            "spent 64.6 s pulling, of the install's 300 s.",
            "- apache/polaris:1.6.0: pulled on 2 nodes, longest 62.5 s (70.0 s with waiting), "
            "250.0 MB",
            "- curlimages/curl:8: pulled on 1 node, longest 2.1 s (2.1 s with waiting); "
            "already present on 1 node",
            "- postgresql:17: already present on 2 nodes",
            "- per node: w1 64.6 s, w2 40.0 s",
        ])

    def test_lines_say_when_nothing_was_pulled_and_count_what_was_not_understood(self):
        self.assertEqual(gate.pull_lines([], set(), 2, install_seconds=12), [
            "Image pulls during the install: none; every image was already present.",
            "- 2 Pulled events not understood",
        ])

    def test_install_measures_pulls_after_its_commands(self):
        events = json.dumps({"items": [pulled(
            'Successfully pulled image "a:1" in 1s (1s including waiting)',
            at="2999-01-01T00:00:00Z")]})
        done = subprocess.CompletedProcess([], 0, stdout=events, stderr="")
        with mock.patch.object(gate, "released_tags", return_value=[]), \
                mock.patch.object(gate, "run") as run, \
                mock.patch.object(gate.subprocess, "run", return_value=done) as kubectl, \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(gate.main(["gate.py", "install", "--dry-run"]), 0)
        run.assert_called_once()
        kubectl.assert_called_once_with(gate.pull_events_command(), capture_output=True,
                                        text=True, check=False)
        self.assertIn("Image pulls during the install: 1 pull of 1 image;", out.getvalue())

    def test_a_measurement_that_fails_never_fails_the_install(self):
        refused = subprocess.CompletedProcess([], 1, stdout="", stderr="connection refused")
        for done in (refused, subprocess.CompletedProcess([], 0, stdout="{", stderr="")):
            with mock.patch.object(gate, "released_tags", return_value=[]), \
                    mock.patch.object(gate, "run"), \
                    mock.patch.object(gate.subprocess, "run", return_value=done), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(gate.main(["gate.py", "install", "--dry-run"]), 0)
            self.assertIn("Image pulls: not measured (", out.getvalue())


class InstallTimeline(unittest.TestCase):
    """#125: where the install's time goes, read back from the cluster after it."""
    SINCE = "2026-10-14T06:44:00+00:00"
    HISTORIES = {("catalog-polaris", "polaris-postgres"): [
        # Before the install: not on the timeline.
        {"revision": 1, "updated": "2026-10-13T03:00:00.5+00:00", "status": "superseded"},
        {"revision": 2, "updated": "2026-10-14T06:44:05.811736291Z", "status": "deployed"}],
        ("catalog-polaris", "polaris"): [
        {"revision": 1, "updated": "2026-10-14T06:45:30.1Z", "status": "deployed"}]}
    JOBS = {"items": [{"metadata": {"namespace": "catalog-polaris", "name": "polaris-bootstrap"},
                       "status": {"startTime": "2026-10-14T06:45:00Z",
                                  "completionTime": "2026-10-14T06:45:20Z"}}]}
    DEPLOYMENTS = {"items": [
        {"metadata": {"namespace": "catalog-polaris", "name": "polaris"},
         "status": {"conditions": [
             {"type": "Available", "status": "True",
              "lastTransitionTime": "2026-10-14T06:46:00Z"},
             {"type": "Progressing", "status": "True", "reason": "NewReplicaSetAvailable",
              "lastUpdateTime": "2026-10-14T06:46:10Z"}]}},
        # Still rolling out: no end to show.
        {"metadata": {"namespace": "catalog-polaris", "name": "polaris-console"},
         "status": {"conditions": [
             {"type": "Progressing", "status": "True", "reason": "ReplicaSetUpdated",
              "lastUpdateTime": "2026-10-14T06:46:20Z"}]}}]}
    CLUSTERS = {"items": [{"metadata": {"namespace": "catalog-polaris", "name": "polaris-pg"},
                           "status": {"conditions": [
                               {"type": "Ready", "status": "True",
                                "lastTransitionTime": "2026-10-14T06:44:50Z"}]}}]}
    COMMANDS = [("2026-10-14T06:44:01+00:00", "2026-10-14T06:46:12+00:00",
                 "release.py (the last release)")]

    def entries(self):
        return gate.timeline_entries(self.SINCE, self.HISTORIES, self.JOBS, self.DEPLOYMENTS,
                                     self.CLUSTERS, self.COMMANDS)

    def test_what_happened_since_the_start_in_order(self):
        self.assertEqual([text for _, text in self.entries()], [
            "release.py (the last release) started",
            "helm catalog-polaris/polaris-postgres revision 2 deployed",
            "cluster catalog-polaris/polaris-pg Ready",
            "job catalog-polaris/polaris-bootstrap started",
            "job catalog-polaris/polaris-bootstrap complete",
            "helm catalog-polaris/polaris revision 1 deployed",
            "deployment catalog-polaris/polaris rolled out",
            "release.py (the last release) ended",
        ])

    def test_helms_nanosecond_times_are_read(self):
        at = dict((text, when) for when, text in self.entries())
        self.assertEqual(at["helm catalog-polaris/polaris-postgres revision 2 deployed"],
                         gate.dt.datetime(2026, 10, 14, 6, 44, 5, 811736,
                                          tzinfo=gate.dt.timezone.utc))

    EVENTS = {"items": [
        {"involvedObject": {"kind": "Job", "namespace": "catalog-polaris",
                            "name": "polaris-pg-1-initdb"},
         "reason": "SuccessfulCreate", "message": "Created pod: polaris-pg-1-initdb-x",
         "lastTimestamp": "2026-10-14T06:44:10Z"},
        {"involvedObject": {"kind": "Job", "namespace": "catalog-polaris",
                            "name": "polaris-pg-1-initdb"},
         "reason": "Completed", "message": "Job completed", "eventTime": None,
         "lastTimestamp": "2026-10-14T06:44:30Z"},
        {"involvedObject": {"kind": "Cluster", "namespace": "catalog-polaris",
                            "name": "polaris-pg"},
         "reason": "CreatingInstance", "message": "Primary instance (initdb)",
         "eventTime": "2026-10-14T06:44:08.123456Z", "lastTimestamp": None},
        # Not a Job or a Cluster, and one from before the install: left out.
        {"involvedObject": {"kind": "Pod", "namespace": "catalog-polaris", "name": "p"},
         "reason": "Pulled", "message": "x", "lastTimestamp": "2026-10-14T06:44:20Z"},
        {"involvedObject": {"kind": "Job", "namespace": "catalog-polaris", "name": "old"},
         "reason": "Completed", "message": "Job completed",
         "lastTimestamp": "2026-10-13T06:44:20Z"}]}

    def test_job_and_cluster_events_split_what_cnpg_does(self):
        # CNPG deletes its initdb and join Jobs once they end; their events stay.
        entries = gate.timeline_entries(self.SINCE, {}, {}, {}, {}, [], events=self.EVENTS)
        self.assertEqual([text for _, text in entries], [
            "cluster catalog-polaris/polaris-pg: CreatingInstance (Primary instance (initdb))",
            "job catalog-polaris/polaris-pg-1-initdb: SuccessfulCreate "
            "(Created pod: polaris-pg-1-initdb-x)",
            "job catalog-polaris/polaris-pg-1-initdb: Completed (Job completed)",
        ])

    def test_the_endpoint_bootstrap_and_claim_events_split_the_step_before_polaris(self):
        # release.py runs the endpoint check and the bootstrap in pods it deletes (--rm);
        # their events stay, as do the log claim's. Other pods and claims are left out.
        def event(kind, name, reason, at, message="m"):
            return {"involvedObject": {"kind": kind, "namespace": "catalog-polaris",
                                       "name": name},
                    "reason": reason, "message": message, "lastTimestamp": at}

        entries = gate.timeline_entries(self.SINCE, {}, {}, {}, {}, [], events={"items": [
            event("Pod", "endpoint-check-1a2b3c4d", "Started", "2026-10-14T06:44:21Z"),
            event("Pod", "polaris-bootstrap-5e6f7a8b", "Pulled", "2026-10-14T06:44:25Z",
                  "Container image already present on machine"),
            event("PersistentVolumeClaim", "polaris-logs", "ProvisioningSucceeded",
                  "2026-10-14T06:44:40Z"),
            event("Pod", "polaris-pg-1", "Started", "2026-10-14T06:44:22Z"),
            event("PersistentVolumeClaim", "polaris-pg-1", "ProvisioningSucceeded",
                  "2026-10-14T06:44:23Z"),
            event("Pod", "polaris-bootstrap", "Started", "2026-10-14T06:44:24Z")]})
        self.assertEqual([text for _, text in entries], [
            "pod catalog-polaris/endpoint-check-1a2b3c4d: Started (m)",
            "pod catalog-polaris/polaris-bootstrap-5e6f7a8b: Pulled "
            "(Container image already present on machine)",
            "persistentvolumeclaim catalog-polaris/polaris-logs: ProvisioningSucceeded (m)",
        ])

    def test_events_that_cannot_be_read_leave_the_rest_of_the_timeline(self):
        def subprocess_run(argv, **kwargs):
            if "events" in argv:
                return subprocess.CompletedProcess(argv, 1, stdout="", stderr="forbidden")
            out = "[]" if "list" in argv else json.dumps({"items": []})
            return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

        with mock.patch.object(gate.subprocess, "run", side_effect=subprocess_run):
            lines = gate.measure_timeline(self.SINCE, self.COMMANDS)
        self.assertIn("release.py (the last release) started", "\n".join(lines))
        self.assertEqual(lines[-1], "- events not read (forbidden)")

    def test_a_revision_without_a_time_is_left_out(self):
        # Helm 4 omits a zero `updated` from history's JSON.
        entries = gate.timeline_entries(self.SINCE, {("catalog-polaris", "polaris"): [
            {"revision": 1, "status": "pending-install"}]}, {}, {}, {}, [])
        self.assertEqual(entries, [])

    def test_lines_give_seconds_from_the_start_and_since_the_line_before(self):
        lines = gate.timeline_lines(self.entries()[:3], self.SINCE)
        self.assertEqual(lines, [
            "Install timeline, seconds after the install started (and since the line before):",
            "-   1 s (+ 1)  release.py (the last release) started",
            "-   5 s (+ 4)  helm catalog-polaris/polaris-postgres revision 2 deployed",
            "-  50 s (+45)  cluster catalog-polaris/polaris-pg Ready",
        ])

    def test_nothing_after_the_start_is_said(self):
        self.assertEqual(gate.timeline_lines([], self.SINCE),
                         ["Install timeline: nothing recorded after the install started."])

    def test_each_command_is_named_for_what_it_installs(self):
        previous = os.path.join(tempfile.gettempdir(), "previous", "install", "release.py")
        this = os.path.join(gate.ROOT, "install", "release.py")
        for command, label in (
                (["python3", previous], "release.py (the last release)"),
                (["python3", this], "release.py (this commit)"),
                (["python3", this, "--dry-run"], "release.py (this commit) --dry-run"),
                (["python3", os.path.join(gate.HERE, "gate.py"), "gate-policies"],
                 "gate.py gate-policies"),
                (["python3", os.path.join(gate.HERE, "gate.py"), "seed"], "gate.py seed"),
                (["git", "-C", gate.ROOT, "worktree", "add"], "git worktree add")):
            with self.subTest(label=label):
                self.assertEqual(gate.command_label(command), label)

    def test_a_measurement_that_fails_never_fails_the_install(self):
        refused = subprocess.CompletedProcess([], 1, stdout="", stderr="connection refused")
        with mock.patch.object(gate.subprocess, "run", return_value=refused):
            lines = gate.measure_timeline(self.SINCE, [])
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("Install timeline: not measured ("), lines[0])

    def test_reads_every_helm_release_then_jobs_deployments_and_clusters(self):
        answers = {
            "list": json.dumps([{"name": "polaris", "namespace": "catalog-polaris"}]),
            "history": json.dumps(self.HISTORIES[("catalog-polaris", "polaris")]),
            "jobs": json.dumps(self.JOBS), "deployments": json.dumps(self.DEPLOYMENTS),
            "clusters.postgresql.cnpg.io": json.dumps(self.CLUSTERS),
            "events": json.dumps(self.EVENTS)}
        calls = []

        def subprocess_run(argv, **kwargs):
            calls.append(argv)
            key = next(k for k in answers if k in argv)
            return subprocess.CompletedProcess(argv, 0, stdout=answers[key], stderr="")

        with mock.patch.object(gate.subprocess, "run", side_effect=subprocess_run):
            lines = gate.measure_timeline(self.SINCE, self.COMMANDS)
        # Helm 4, as pinned, lists every release without --all, and has no such flag.
        self.assertEqual(calls[0], ["helm", "--kube-context", gate.CONTEXT, "list",
                                    "--all-namespaces", "-o", "json"])
        self.assertIn(["helm", "--kube-context", gate.CONTEXT, "-n", "catalog-polaris",
                       "history", "polaris", "-o", "json"], calls)
        self.assertIn("helm catalog-polaris/polaris revision 1 deployed", "\n".join(lines))
        self.assertIn("deployment catalog-polaris/polaris rolled out", "\n".join(lines))
        self.assertIn(["kubectl", "--context", gate.CONTEXT, "get", "events",
                       "--all-namespaces", "-o", "json"], calls)
        self.assertIn("job catalog-polaris/polaris-pg-1-initdb: Completed", "\n".join(lines))

    def test_a_dry_run_that_installs_nothing_measures_nothing(self):
        with mock.patch.object(gate, "released_tags", return_value=[]), \
                mock.patch.object(gate, "run"), \
                mock.patch.object(gate, "measure_pulls", return_value=[]), \
                mock.patch.object(gate, "measure_timeline") as timeline, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(gate.main(["gate.py", "install", "--dry-run"]), 0)
        timeline.assert_not_called()

    def test_an_install_prints_its_timeline_with_each_command_timed(self):
        with mock.patch.object(gate, "released_tags", return_value=[]), \
                mock.patch.object(gate, "run"), \
                mock.patch.object(gate, "measure_pulls", return_value=[]), \
                mock.patch.object(gate, "measure_timeline",
                                  return_value=["Install timeline: x"]) as timeline, \
                mock.patch.object(gate, "INSTALL_RECORD",
                                  os.path.join(tempfile.mkdtemp(), "install.json")), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(gate.main(["gate.py", "install"]), 0)
        since, commands = timeline.call_args.args
        self.assertEqual([label for _, _, label in commands],
                         ["gate.py gate-policies", "release.py (this commit)"])
        self.assertIn("Install timeline: x", out.getvalue())


TEMPLATE = """KUBE_CONTEXT=
NS_CATALOG_POLARIS=catalog-polaris
NS_STANDINS=standins
S3_ENDPOINT=
S3_REGION=
POLARIS_WAREHOUSE_BUCKET=
POLARIS_OPA_URL=
POLARIS_DB_HOST=
POLARIS_GATEWAY_NAMESPACE_LABEL=
POLARIS_QUERY_NAMESPACE_LABEL=
METRICS_NAMESPACE_LABEL=
INGRESS_NAMESPACE_LABEL=
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

    def test_direct_endpoints_name_the_stand_ins_and_polaris_pg_never_the_recorder(self):
        # ADR-053 §4: a stress install measures Polaris, not the recorder.
        template = TEMPLATE.replace("NS_STANDINS=standins", "NS_STANDINS=si")
        env = gate.init_env.parse(gate.gate_env(template, gen=lambda name: "x", direct=True))
        self.assertEqual(env["S3_ENDPOINT"], "http://seaweedfs.si.svc.cluster.local:8333")
        self.assertEqual(env["POLARIS_OPA_URL"],
                         "http://opa.si.svc.cluster.local:8181/v1/data/polaris/authz")
        self.assertEqual(env["POLARIS_DB_HOST"], "polaris-pg-rw")
        self.assertNotIn("recorder", "".join(env.values()))

    def test_direct_changes_only_the_three_endpoints(self):
        recorded = gate.init_env.parse(gate.gate_env(TEMPLATE, gen=lambda name: "x"))
        direct = gate.init_env.parse(gate.gate_env(TEMPLATE, gen=lambda name: "x", direct=True))
        self.assertEqual(sorted(k for k in recorded if recorded[k] != direct[k]),
                         ["POLARIS_DB_HOST", "POLARIS_OPA_URL", "S3_ENDPOINT"])

    def test_each_caller_class_gets_the_gates_namespace_label(self):
        # ADR-048 §6: check-network gives these labels to its source namespaces.
        env = gate.init_env.parse(gate.gate_env(TEMPLATE, gen=lambda name: "x"))
        self.assertEqual({var: env[var] for var in gate.release.CALLER_VARS.values()}, {
            "POLARIS_GATEWAY_NAMESPACE_LABEL": "lakehouse-gate/caller=gateway",
            "POLARIS_QUERY_NAMESPACE_LABEL": "lakehouse-gate/caller=query",
            "METRICS_NAMESPACE_LABEL": "lakehouse-gate/caller=metrics",
            "INGRESS_NAMESPACE_LABEL": "lakehouse-gate/caller=ingress"})

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

    def test_a_direct_env_file_names_the_stand_ins(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, ".env")
            gate.write_gate_env(path, TEMPLATE, direct=True)
            with open(path, encoding="utf-8") as f:
                env = gate.init_env.parse(f.read())
        self.assertEqual(env["S3_ENDPOINT"], "http://seaweedfs.standins.svc.cluster.local:8333")

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


def rebuilding(primary="polaris-pg-1", ready=3, condition="True"):
    return {"spec": {"instances": 3},
            "status": {"currentPrimary": primary, "readyInstances": ready,
                       "conditions": [{"type": "Ready", "status": condition}]}}


def jobs(*names, uid="1"):
    return {"items": [{"metadata": {"name": n, "uid": f"{n}-{uid}"}} for n in names]}


class RebuildReplica(unittest.TestCase):
    """ADR-048 §7: a replica's PVC and pod deleted; CNPG rebuilds it with a join pod."""

    def test_it_picks_a_replica_never_the_primary(self):
        self.assertEqual(gate.rebuild_target(rebuilding("polaris-pg-1"), pods("a", "b", "c")),
                         "polaris-pg-2")
        self.assertEqual(gate.rebuild_target(rebuilding("polaris-pg-2"), pods("a", "b", "c")),
                         "polaris-pg-1")

    def test_with_no_replica_there_is_nothing_to_rebuild(self):
        self.assertIsNone(gate.rebuild_target(rebuilding("polaris-pg-1"), pods("a")))

    def test_it_deletes_the_pvc_without_waiting_then_the_pod_in_polaris_namespace(self):
        pvc, pod = gate.rebuild_commands("catalog-polaris", "polaris-pg-2")
        for argv in (pvc, pod):
            self.assertEqual(argv[:5], ["kubectl", "--context", gate.CONTEXT, "-n",
                                        "catalog-polaris"])
        self.assertEqual(pvc[5:], ["delete", "pvc", "polaris-pg-2", "--wait=false"])
        self.assertEqual(pod[5:], ["delete", "pod", "polaris-pg-2"])

    def test_it_reads_polaris_pgs_join_jobs(self):
        argv = gate.join_jobs_command("catalog-polaris")
        self.assertEqual(argv[argv.index("-n") + 1], "catalog-polaris")
        self.assertIn("cnpg.io/cluster=polaris-pg,cnpg.io/jobRole=join", argv)

    def test_rebuilt_needs_a_new_join_and_the_cluster_ready_with_every_instance(self):
        self.assertTrue(gate.rebuilt(rebuilding(), {"polaris-pg-4-join"}))
        self.assertFalse(gate.rebuilt(rebuilding(), set()))
        self.assertFalse(gate.rebuilt(rebuilding(ready=2), {"polaris-pg-4-join"}))
        self.assertFalse(gate.rebuilt(rebuilding(condition="False"), {"polaris-pg-4-join"}))

    def answers(self, after):
        """gate.output's answers: the Cluster, its instances and the join Jobs before the
        deletion, then (join Jobs, Cluster) for each poll in `after`."""
        before = [json.dumps(rebuilding()), json.dumps(pods("a", "b", "c")),
                  json.dumps(jobs("polaris-pg-2-join", "polaris-pg-3-join"))]
        return before + [json.dumps(x) for pair in after for x in pair]

    def run_rebuild(self, after, timeout=900):
        ran, other = [], []
        clock = iter(range(0, 100000, 3))
        with mock.patch.object(gate, "output", side_effect=self.answers(after)), \
                mock.patch.object(gate, "run", side_effect=ran.append), \
                mock.patch.object(gate.subprocess, "run",
                                  side_effect=lambda argv, **kw: other.append(argv)), \
                mock.patch.object(gate.time, "monotonic", side_effect=lambda: next(clock)), \
                mock.patch.object(gate.time, "sleep"), \
                mock.patch.object(gate, "REBUILD_TIMEOUT", timeout):
            problems, notes = gate.rebuild_replica({"NS_CATALOG_POLARIS": "catalog-polaris"})
        return problems, notes, ran, other

    def test_it_passes_once_a_new_join_ran_and_the_cluster_is_ready_again(self):
        old = jobs("polaris-pg-2-join", "polaris-pg-3-join")
        new = jobs("polaris-pg-2-join", "polaris-pg-3-join", "polaris-pg-4-join")
        problems, notes, ran, other = self.run_rebuild([
            (old, rebuilding()),            # the operator has not acted yet
            (new, rebuilding(ready=2)),     # the join runs
            (old, rebuilding()),            # CNPG removed the finished Job: seen all the same
        ])
        self.assertEqual(problems, [])
        self.assertEqual(ran, gate.rebuild_commands("catalog-polaris", "polaris-pg-2"))
        self.assertIn("polaris-pg-4-join", "\n".join(notes))
        self.assertEqual(other, [])

    def test_a_ready_cluster_without_a_new_join_fails_and_prints_the_join_pods_log(self):
        old = jobs("polaris-pg-2-join", "polaris-pg-3-join")
        problems, _, _, other = self.run_rebuild([(old, rebuilding())] * 3, timeout=6)
        self.assertEqual(len(problems), 1)
        self.assertIn("polaris-pg", problems[0])
        (logs,) = other
        self.assertIn("logs", logs)
        self.assertIn("cnpg.io/cluster=polaris-pg,cnpg.io/jobRole=join", logs)

    def test_a_join_job_made_again_under_an_old_name_counts_as_new(self):
        problems, notes, _, _ = self.run_rebuild([
            (jobs("polaris-pg-2-join", "polaris-pg-3-join", uid="2"), rebuilding())])
        self.assertEqual(problems, [])

    def test_a_join_that_never_gets_the_cluster_ready_fails(self):
        new = jobs("polaris-pg-2-join", "polaris-pg-3-join", "polaris-pg-4-join")
        problems, _, _, _ = self.run_rebuild([(new, rebuilding(ready=2))] * 3, timeout=6)
        self.assertEqual(len(problems), 1)

    def test_with_no_replica_it_deletes_nothing(self):
        with mock.patch.object(gate, "output", side_effect=[
                    json.dumps(rebuilding()), json.dumps(pods("a"))]), \
                mock.patch.object(gate, "run") as ran:
            problems, _ = gate.rebuild_replica({"NS_CATALOG_POLARIS": "catalog-polaris"})
        self.assertEqual(len(problems), 1)
        ran.assert_not_called()

    def test_main_runs_it_and_the_usage_names_it(self):
        self.assertIn("| rebuild-replica |", gate.__doc__.strip().splitlines()[-1])
        with mock.patch.object(gate, "rebuild_replica", return_value=([], ["ok"])) as rebuild, \
                mock.patch.object(gate.release, "load_env", return_value={}), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(gate.main(["gate.py", "rebuild-replica"]), 0)
        rebuild.assert_called_once()


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


class Migration(unittest.TestCase):
    """ADR-051 §2, §3: the seed before the upgrade and check-migration after it."""

    def pod(self, mode):
        return gate.seed_pod_command(STANDIN_ENV, PYTHON, f"seed-{mode}-1", mode)

    def test_the_configmap_holds_the_seed_smoke_py_and_the_requirements(self):
        files = {"seed.py": "# seed\n", "smoke.py": "# smoke\n", "requirements.txt": "x\n"}
        manifest = gate.seed_configmap(STANDIN_ENV, files)
        self.assertEqual((manifest["kind"], manifest["metadata"]),
                         ("ConfigMap", {"name": "migration-seed", "namespace": "catalog-polaris"}))
        self.assertEqual(manifest["data"], files)

    def test_both_pods_run_seed_py_on_the_pinned_python_with_credentials_from_secrets(self):
        for mode in ("seed", "check"):
            with self.subTest(mode=mode):
                argv = self.pod(mode)
                self.assertEqual(argv[:7], ["kubectl", "--context", gate.CONTEXT, "-n",
                                            "catalog-polaris", "run", f"seed-{mode}-1"])
                for flag in ("--rm", "--restart=Never", f"--image={PYTHON}", gate.CHECK_POD_LABEL):
                    self.assertIn(flag, argv)
                overrides = json.loads(argv[argv.index("--overrides") + 1])
                (container,) = overrides["spec"]["containers"]
                refs = {e["name"]: e["valueFrom"]["secretKeyRef"] for e in container["env"]
                        if "valueFrom" in e}
                self.assertEqual(refs, {
                    "ROOT_CLIENT_ID": {"name": "polaris-root", "key": "clientId"},
                    "ROOT_CLIENT_SECRET": {"name": "polaris-root", "key": "clientSecret"},
                    "S3_ACCESS_KEY_ID": {"name": "smoke-s3", "key": "accessKeyId"},
                    "S3_SECRET_ACCESS_KEY": {"name": "smoke-s3", "key": "secretAccessKey"}})
                self.assertEqual(overrides["spec"]["volumes"],
                                 [{"name": "code", "configMap": {"name": "migration-seed"}}])
                tail = argv[argv.index("--command") + 2:]
                for part in ("--require-hashes", "--only-binary :all:",
                             "-r /seed/requirements.txt", "exec python3 /seed/seed.py"):
                    self.assertIn(part, tail[2])
                self.assertEqual(tail[3:], ["seed", mode, "http://polaris:8181", "POLARIS",
                                            STANDIN_ENV["S3_ENDPOINT"], "us-east-1",
                                            "polaris-warehouse"])

    def test_only_the_check_reads_the_record_on_stdin(self):
        seed_pod, check_pod = self.pod("seed"), self.pod("check")
        self.assertIn("--attach", seed_pod)
        self.assertNotIn("-i", seed_pod)
        self.assertIn("-i", check_pod)
        (container,) = json.loads(check_pod[check_pod.index("--overrides") + 1])["spec"][
            "containers"]
        self.assertEqual((container["stdin"], container["stdinOnce"]), (True, True))

    def test_the_counts_are_read_as_polaris_from_polaris_pg_directly(self):
        command = gate.db_counts_command(STANDIN_ENV, PG_IMAGE, "polaris", "db-counts-1")
        self.assertEqual(command[:7], ["kubectl", "--context", gate.CONTEXT, "-n",
                                       "catalog-polaris", "run", "db-counts-1"])
        self.assertIn(gate.CHECK_POD_LABEL, command)
        # Not through the recorder: the counts are the gate's, not Polaris's traffic.
        self.assertEqual(command[command.index("--") + 1:],
                         ["sh", "-s", "--", "polaris-pg-rw", "polaris"])
        (c,) = json.loads(command[command.index("--overrides") + 1])["spec"]["containers"]
        self.assertEqual([e["valueFrom"]["secretKeyRef"] for e in c["env"]],
                         [{"name": "polaris-db", "key": "username"},
                          {"name": "polaris-db", "key": "password"}])

    def test_the_counts_script_reads_every_table_of_polaris_schema_and_the_version(self):
        with open(os.path.join(HERE, "db_counts.sh"), encoding="utf-8") as f:
            script = f.read()
        self.assertIn("table_schema = 'polaris_schema'", script)
        self.assertIn("polaris_schema.version", script)
        self.assertIn("sslmode=require", script)

    def test_counts_and_the_version_parse_from_the_script_s_lines(self):
        text = "table entities 12\ntable grant_records 30\nversion 4\n"
        self.assertEqual(gate.parse_counts(text),
                         {"counts": {"entities": 12, "grant_records": 30}, "version": 4})
        self.assertEqual(gate.parse_counts("table entities 1\n"),
                         {"counts": {"entities": 1}, "version": None})

    def test_fewer_rows_or_a_table_gone_fails_and_the_version_is_reported(self):
        before = {"counts": {"entities": 12, "grant_records": 30, "events": 2}, "version": 4}
        after = {"counts": {"entities": 13, "grant_records": 29}, "version": 5}
        problems, notes = gate.db_problems(before, after)
        self.assertEqual(problems, [
            "polaris_schema.grant_records: 29 rows, 30 recorded before the upgrade",
            "polaris_schema.events: gone, 2 rows recorded before the upgrade"])
        self.assertIn("Schema version: 4 before the upgrade, 5 after: a change is reported, "
                      "not failed (ADR-051 §3).", notes)

    def test_the_same_or_more_rows_pass(self):
        before = {"counts": {"entities": 12}, "version": 4}
        problems, notes = gate.db_problems(before, {"counts": {"entities": 15, "new": 1},
                                                    "version": 4})
        self.assertEqual(problems, [])
        self.assertIn("Schema version: 4 before the upgrade, 4 after.", notes)

    def test_check_migration_without_a_seed_says_so_and_passes(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(gate.subprocess, "run") as run:
            problems, notes = gate.check_migration(STANDIN_ENV, gate.load_pins(),
                                                   os.path.join(d, "none.json"))
        self.assertEqual(problems, [])
        self.assertEqual(notes, ["Nothing was seeded: a fresh install, nothing to check "
                                 "(ADR-051 §3)."])
        run.assert_not_called()

    def test_the_seed_keeps_its_record_and_the_counts_on_the_runner_and_prints_neither(self):
        record = {"objects": {}, "rows": {}, "principals": {"gate_seed": "id:Secret0123456789"}}
        answers = {"seed": subprocess.CompletedProcess(
            [], 0, stdout=f"ok   a\nSEED-RECORD {json.dumps(record)}\n", stderr="")}
        counts = "table entities 12\nversion 4\n"
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(gate.subprocess, "run",
                                  side_effect=lambda argv, **kw: answers.get(
                                      argv[6].split("-")[0] if argv[5:6] == ["run"] else "",
                                      subprocess.CompletedProcess([], 0, "", ""))), \
                mock.patch.object(gate, "output", return_value=counts), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            path = os.path.join(d, "seed.json")
            problems, _ = gate.seed_gate(STANDIN_ENV, gate.load_pins(), path)
            with open(path, encoding="utf-8") as f:
                kept = json.load(f)
        self.assertEqual(problems, [])
        self.assertEqual(kept, {"api": record, "db": {"counts": {"entities": 12},
                                                      "version": 4}})
        self.assertIn("ok   a", out.getvalue())
        self.assertNotIn("Secret0123456789", out.getvalue())

    def test_a_seed_that_fails_or_writes_no_record_fails(self):
        for answer in (subprocess.CompletedProcess([], 1, stdout="FAIL x\n", stderr=""),
                       subprocess.CompletedProcess([], 0, stdout="ok   x\n", stderr="")):
            with self.subTest(code=answer.returncode), tempfile.TemporaryDirectory() as d, \
                    mock.patch.object(gate.subprocess, "run", return_value=answer), \
                    contextlib.redirect_stdout(io.StringIO()):
                path = os.path.join(d, "seed.json")
                problems, _ = gate.seed_gate(STANDIN_ENV, gate.load_pins(), path)
                self.assertTrue(problems)
                self.assertFalse(os.path.exists(path))

    def test_check_migration_hands_the_record_to_the_pod_and_compares_the_counts(self):
        record = {"objects": {"/x": {}}, "rows": {}, "principals": {}}
        kept = {"api": record, "db": {"counts": {"entities": 12}, "version": 4}}
        calls = []

        def fake_run(argv, **kw):
            calls.append((argv, kw))
            return subprocess.CompletedProcess([], 0, "", "")

        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(gate.subprocess, "run", side_effect=fake_run), \
                mock.patch.object(gate, "output", return_value="table entities 11\nversion 4\n"), \
                contextlib.redirect_stdout(io.StringIO()):
            path = os.path.join(d, "seed.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(kept, f)
            problems, notes = gate.check_migration(STANDIN_ENV, gate.load_pins(), path)
        pod = next(kw for argv, kw in calls if "--overrides" in argv and "-i" in argv)
        self.assertEqual(json.loads(pod["input"]), record)
        self.assertEqual(problems,
                         ["polaris_schema.entities: 11 rows, 12 recorded before the upgrade"])


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


RESTORE_ENV = {"NS_CATALOG_POLARIS": "catalog-polaris"}
PG_IMAGE = "ghcr.io/cloudnative-pg/postgresql:16.15-1-minimal-trixie@sha256:" + "a" * 64


def recovery_pod(suffix, phase, created, containers, init=()):
    ended = created.replace(":00Z", ":20Z")

    def status(name, state, restarts=0):
        if "terminated" in state:
            state = {"terminated": {**state["terminated"], "finishedAt": ended}}
        return {"name": name, "state": state, "restartCount": restarts}
    return {"metadata": {"name": f"polaris-pg-restore-1-full-recovery-{suffix}",
                         "creationTimestamp": created},
            "status": {"phase": phase,
                       "initContainerStatuses": [status(*c) for c in init],
                       "containerStatuses": [status(*c) for c in containers]}}


ARCHIVED_BEFORE = "000000010000000000000007"
SWITCHED = "000000010000000000000008"
DONE = {"terminated": {"reason": "Completed", "exitCode": 0}}
ERROR = {"terminated": {"reason": "Error", "exitCode": 1}}
# Listed out of order: the report sorts them by creation.
RECOVERY_PODS = {"items": [
    recovery_pod("ccccc", "Succeeded", "2026-10-14T00:03:00Z", [("full-recovery", DONE)]),
    recovery_pod("aaaaa", "Failed", "2026-10-14T00:01:00Z", [("full-recovery", ERROR)],
                 init=[("plugin-barman-cloud", DONE)]),
    recovery_pod("bbbbb", "Failed", "2026-10-14T00:02:00Z", [("full-recovery", ERROR, 2)],
                 init=[("plugin-barman-cloud", {"running": {}})]),
]}


class CheckRestore(unittest.TestCase):
    """ADR-043 §4: a backup of polaris-pg, restored into polaris-pg-restore, read, deleted."""

    def test_an_on_demand_backup_of_polaris_pg_through_the_plugin(self):
        backup = gate.backup_manifest(RESTORE_ENV, "polaris-pg-gate-1")
        self.assertEqual((backup["apiVersion"], backup["kind"]),
                         ("postgresql.cnpg.io/v1", "Backup"))
        self.assertEqual(backup["metadata"], {"name": "polaris-pg-gate-1",
                                              "namespace": "catalog-polaris"})
        self.assertEqual(backup["spec"], {
            "cluster": {"name": "polaris-pg"}, "method": "plugin",
            "pluginConfiguration": {"name": "barman-cloud.cloudnative-pg.io"}})

    def test_the_restore_bootstraps_from_the_object_store_to_the_latest_wal(self):
        restore = gate.restore_cluster(RESTORE_ENV, PG_IMAGE)
        self.assertEqual((restore["kind"], restore["metadata"]["name"]),
                         ("Cluster", gate.RESTORE))
        self.assertEqual(gate.RESTORE, "polaris-pg-restore")
        self.assertEqual(restore["metadata"]["namespace"], "catalog-polaris")
        spec = restore["spec"]
        self.assertEqual(spec["instances"], 1)
        self.assertEqual(spec["imageName"], PG_IMAGE)
        # No recovery target: everything archived is replayed.
        self.assertEqual(spec["bootstrap"], {"recovery": {
            "source": "polaris-pg", "database": "polaris", "owner": "polaris",
            "secret": {"name": "polaris-db"}}})
        self.assertEqual(spec["externalClusters"], [{
            "name": "polaris-pg",
            "plugin": {"name": "barman-cloud.cloudnative-pg.io",
                       "parameters": {"barmanObjectName": "polaris-backup",
                                      "serverName": "polaris-pg"}}}])

    def test_the_restore_never_archives_into_polaris_pgs_backups(self):
        self.assertNotIn("plugins", gate.restore_cluster(RESTORE_ENV, PG_IMAGE)["spec"])

    def test_a_backup_is_done_once_completed_or_failed(self):
        self.assertIsNone(gate.backup_outcome({}))
        self.assertIsNone(gate.backup_outcome({"status": {"phase": "running"}}))
        self.assertEqual(gate.backup_outcome({"status": {"phase": "completed"}}),
                         ("completed", ""))
        self.assertEqual(gate.backup_outcome({"status": {"phase": "failed",
                                                         "error": "no bucket"}}),
                         ("failed", "no bucket"))

    def test_the_check_pod_reads_the_restore_as_polaris_with_its_secret(self):
        command = gate.check_restore_command(RESTORE_ENV, PG_IMAGE, "polaris", "check-restore-1")
        self.assertEqual(command[:5], ["kubectl", "--context", gate.CONTEXT, "-n",
                                       "catalog-polaris"])
        self.assertIn("--rm", command)
        self.assertEqual(command[command.index("--") + 1:],
                         ["sh", "-s", "--", "polaris-pg-restore-rw", "polaris", "POLARIS",
                          gate.ICEBERG_CATALOG])
        overrides = json.loads(command[command.index("--overrides") + 1])
        (c,) = overrides["spec"]["containers"]
        self.assertEqual(c["image"], PG_IMAGE)
        self.assertEqual(c["env"], [
            {"name": "PGUSER", "valueFrom": {"secretKeyRef": {"name": "polaris-db",
                                                              "key": "username"}}},
            {"name": "PGPASSWORD", "valueFrom": {"secretKeyRef": {"name": "polaris-db",
                                                                  "key": "password"}}}])

    def test_check_restore_is_a_command(self):
        self.assertIn("check-restore", gate.__doc__.strip().splitlines()[-1])

    def test_it_reads_the_restores_full_recovery_pods(self):
        argv = gate.recovery_pods_command("catalog-polaris")
        self.assertEqual(argv[:5], ["kubectl", "--context", gate.CONTEXT, "-n",
                                    "catalog-polaris"])
        self.assertIn("cnpg.io/cluster=polaris-pg-restore,cnpg.io/jobRole=full-recovery", argv)

    def test_the_report_counts_the_pods_and_names_how_each_ended(self):
        # Oldest first, whatever order they were read in.
        lines, failed = gate.recovery_report(RECOVERY_PODS)
        self.assertEqual(lines[0], "Full recovery of polaris-pg-restore: 3 pods, 2 failed.")
        self.assertEqual(lines[1:], [
            "  polaris-pg-restore-1-full-recovery-aaaaa: Failed, created 00:01:00, ended "
            "00:01:20; plugin-barman-cloud: terminated Completed (exit 0); full-recovery: "
            "terminated Error (exit 1)",
            "  polaris-pg-restore-1-full-recovery-bbbbb: Failed, created 00:02:00, ended "
            "00:02:20; plugin-barman-cloud: running; full-recovery: terminated Error (exit 1), "
            "2 restarts",
            "  polaris-pg-restore-1-full-recovery-ccccc: Succeeded, created 00:03:00, ended "
            "00:03:20; full-recovery: terminated Completed (exit 0)",
        ])
        self.assertEqual(failed, ["polaris-pg-restore-1-full-recovery-aaaaa",
                                  "polaris-pg-restore-1-full-recovery-bbbbb"])

    def test_no_pods_read_is_said_and_names_none(self):
        self.assertEqual(gate.recovery_report({"items": []}),
                         (["Full recovery of polaris-pg-restore: 0 pods, 0 failed."], []))

    def test_the_restore_is_ready_only_on_its_ready_condition(self):
        def cluster(*conditions):
            return {"status": {"conditions": [{"type": t, "status": s} for t, s in conditions]}}
        self.assertTrue(gate.cluster_ready(cluster(("Ready", "True"))))
        self.assertFalse(gate.cluster_ready(cluster(("Ready", "False"))))
        self.assertFalse(gate.cluster_ready(cluster(("ContinuousArchiving", "True"))))
        self.assertFalse(gate.cluster_ready({}))

    def test_pods_are_remembered_after_they_are_gone_and_each_failure_named_once(self):
        # #127's smoke: CNPG deletes the Job's pods, so a read after Ready found one of them.
        aaaaa, bbbbb, ccccc = sorted(RECOVERY_PODS["items"],
                                     key=lambda p: p["metadata"]["name"])
        running = copy.deepcopy(bbbbb)
        running["status"]["phase"] = "Running"
        seen = {}
        self.assertEqual(gate.remember_pods(seen, {"items": [aaaaa, running]}),
                         [aaaaa["metadata"]["name"]])
        self.assertEqual(gate.remember_pods(seen, {"items": [aaaaa, bbbbb]}),
                         [bbbbb["metadata"]["name"]])
        self.assertEqual(gate.remember_pods(seen, {"items": [ccccc]}), [])
        self.assertEqual(sorted(seen), sorted(p["metadata"]["name"] for p in (aaaaa, bbbbb,
                                                                              ccccc)))
        lines, _ = gate.recovery_report({"items": list(seen.values())})
        self.assertEqual(lines[0], "Full recovery of polaris-pg-restore: 3 pods, 2 failed.")

    def check(self, polls=None, settled=None, unread=(), archived=None):
        """Runs check_restore with every command faked. `polls` lists, per read of the wait,
        whether the restore is Ready and the pods read; once they run out the clock passes
        the timeout. `unread` names the reads of the pods (0-based) that fail. `archived`
        lists what pg_stat_archiver answers on each read, the last answer repeated.
        (problems, notes, events)."""
        if archived is None:
            archived = [ARCHIVED_BEFORE, SWITCHED]
        if polls is None:
            aaaaa, bbbbb, ccccc = sorted(RECOVERY_PODS["items"],
                                         key=lambda p: p["metadata"]["name"])
            polls = [(False, [aaaaa]), (False, [bbbbb]), (True, [ccccc])]
        events, reads = [], {"cluster": 0, "pods": 0, "archived": 0}

        def subprocess_run(argv, **kwargs):
            text = " ".join(argv)
            kind = ("switch" if "pg_switch_wal" in text else
                    "archived" if "pg_stat_archiver" in text else
                    "primary" if f"cluster/{gate.release.POLARIS_PG}" in argv else
                    "cluster" if f"cluster/{gate.RESTORE}" in argv else
                    "delete" if "delete" in argv else "pods" if "get" in argv else
                    "logs" if "logs" in argv else "psql")
            events.append((kind, argv))
            if kind == "primary":
                return subprocess.CompletedProcess(argv, 0, json.dumps(
                    {"status": {"currentPrimary": "polaris-pg-2"}}), "")
            if kind == "switch":
                return subprocess.CompletedProcess(argv, 0, SWITCHED + "\n", "")
            if kind == "archived":
                answer = archived[min(reads["archived"], len(archived) - 1)]
                reads["archived"] += 1
                return subprocess.CompletedProcess(argv, 0, answer + "\n", "")
            if kind == "cluster":
                ready = polls[min(reads["cluster"], len(polls) - 1)][0]
                reads["cluster"] += 1
                return subprocess.CompletedProcess(argv, 0, json.dumps({"status": {
                    "conditions": [{"type": "Ready", "status": str(ready)}]}}), "")
            if kind == "pods":
                n = reads["pods"]
                reads["pods"] += 1
                if n in unread:
                    return subprocess.CompletedProcess(argv, 1, "", "connection refused")
                pods = polls[min(n, len(polls) - 1)][1]
                return subprocess.CompletedProcess(argv, 0, json.dumps({"items": pods}), "")
            return subprocess.CompletedProcess(argv, 0, "", "")

        def settle(run, context, namespace, cluster):
            events.append(("settle", (run, context, namespace, cluster)))
            return settled

        def monotonic():
            # Within the timeout for as many reads as there are polls, past it after.
            return 0 if reads["cluster"] < len(polls) else gate.RESTORE_TIMEOUT + 1

        def run(argv, stdin=None):
            events.append(("apply", stdin and json.loads(stdin)["kind"]))

        with mock.patch.object(gate, "run", side_effect=run), \
                mock.patch.object(gate, "wait_for", return_value=("completed", "")), \
                mock.patch.object(gate, "polaris_pg_values",
                                  return_value={"image": PG_IMAGE, "database": "polaris"}), \
                mock.patch.object(gate.subprocess, "run", side_effect=subprocess_run), \
                mock.patch.object(gate.time, "monotonic", side_effect=monotonic), \
                mock.patch.object(gate.time, "sleep") as sleep, \
                mock.patch.object(gate.release, "settle", side_effect=settle), \
                contextlib.redirect_stdout(io.StringIO()):
            problems, notes = gate.check_restore(RESTORE_ENV)
        for call in sleep.call_args_list:
            self.assertEqual(call.args, (gate.RESTORE_POLL,))
        return problems, notes, events

    # Smoke 38245393066: Ready, then psql refused while CNPG restarted the restored instance.
    def test_the_restore_settles_after_ready_and_before_psql(self):
        problems, notes, events = self.check()
        self.assertEqual(problems, [])
        self.assertEqual([kind for kind, _ in events][-3:], ["settle", "psql", "delete"])
        settle = next(args for kind, args in events if kind == "settle")
        self.assertEqual(settle, (gate.release.call, gate.CONTEXT, "catalog-polaris",
                                  "polaris-pg-restore"))
        self.assertIn("healthy", "\n".join(notes))

    def test_each_failed_pods_logs_are_printed_while_the_restore_waits(self):
        # #86: the logs are read as soon as a pod fails, before CNPG deletes it.
        problems, notes, events = self.check()
        self.assertEqual(problems, [])
        wait = [kind for kind, _ in events][events.index(("apply", "Cluster")) + 1:]
        self.assertEqual(wait[:8], ["cluster", "pods", "logs", "cluster", "pods", "logs",
                                    "cluster", "pods"])
        logs = [argv for kind, argv in events if kind == "logs"]
        self.assertEqual([argv[argv.index("logs") + 1] for argv in logs],
                         ["polaris-pg-restore-1-full-recovery-aaaaa",
                          "polaris-pg-restore-1-full-recovery-bbbbb"])
        for argv in logs:
            self.assertIn("--all-containers", argv)
            self.assertIn("--prefix", argv)
        # Every pod seen in the wait is reported, though no single read held them all.
        self.assertIn("Full recovery of polaris-pg-restore: 3 pods, 2 failed.", notes)

    def test_a_restore_that_never_gets_ready_still_reports_its_pods(self):
        aaaaa = sorted(RECOVERY_PODS["items"], key=lambda p: p["metadata"]["name"])[0]
        problems, notes, events = self.check(polls=[(False, [aaaaa]), (False, [aaaaa])])
        self.assertEqual(problems, ["Cluster polaris-pg-restore did not get Ready from "
                                    "polaris-backup"])
        self.assertEqual([kind for kind, _ in events].count("logs"), 1)
        self.assertNotIn("settle", [kind for kind, _ in events])
        self.assertEqual(events[-1][0], "delete")
        self.assertIn("Full recovery of polaris-pg-restore: 1 pods, 1 failed.", notes)

    def test_a_read_of_the_pods_that_fails_is_counted_and_the_wait_goes_on(self):
        problems, notes, _ = self.check(unread=(1,))
        self.assertEqual(problems, [])
        self.assertIn("Full recovery of polaris-pg-restore: 2 pods, 1 failed.", notes)
        self.assertIn("  1 read of the pods failed: connection refused", notes)

    def test_a_restore_that_does_not_settle_is_a_problem_and_still_deleted(self):
        problems, _, events = self.check(settled="Cluster polaris-pg-restore did not settle")
        self.assertEqual(problems, ["Cluster polaris-pg-restore did not settle"])
        self.assertEqual([kind for kind, _ in events][-2:], ["settle", "delete"])

    # #86: a backup from a standby is consistent only past the WAL archived so far; without a
    # switch, the restore failed until archive_timeout (smoke 38515945485: 4 attempts).
    def test_the_primary_switches_wal_and_it_is_archived_before_the_restore(self):
        problems, notes, events = self.check(archived=[ARCHIVED_BEFORE, ARCHIVED_BEFORE,
                                                       SWITCHED])
        self.assertEqual(problems, [])
        kinds = [kind for kind, _ in events]
        self.assertEqual(kinds[:7], ["apply", "primary", "switch", "archived", "archived",
                                     "archived", "apply"])
        self.assertEqual(events[0][1], "Backup")
        self.assertEqual(events[6][1], "Cluster")
        self.assertIn(f"WAL switched on polaris-pg-2: {SWITCHED} archived before the "
                      "restore.", notes)

    def test_the_switch_and_the_archive_are_read_on_the_primary(self):
        self.assertEqual(gate.wal_switch_command("catalog-polaris", "polaris-pg-2"), [
            "kubectl", "--context", gate.CONTEXT, "-n", "catalog-polaris", "exec",
            "polaris-pg-2", "-c", "postgres", "--", "psql", "-XAtq", "-c",
            "SELECT pg_walfile_name(pg_switch_wal())"])
        self.assertEqual(gate.archived_wal_command("catalog-polaris", "polaris-pg-2")[-1],
                         "SELECT coalesce(last_archived_wal, '') FROM pg_stat_archiver")

    def test_a_segment_counts_as_archived_once_the_archiver_reached_it(self):
        self.assertTrue(gate.wal_archived(SWITCHED, SWITCHED))
        self.assertTrue(gate.wal_archived(SWITCHED, "000000010000000000000009"))
        self.assertFalse(gate.wal_archived(SWITCHED, ARCHIVED_BEFORE))
        self.assertFalse(gate.wal_archived(SWITCHED, ""))
        # A later timeline's segment comes after every segment of an earlier one.
        self.assertTrue(gate.wal_archived(SWITCHED, "000000020000000000000001"))

    def test_a_segment_never_archived_is_a_problem_and_no_restore_is_made(self):
        problems, _, events = self.check(archived=[ARCHIVED_BEFORE])
        self.assertEqual(problems, [f"WAL segment {SWITCHED} from polaris-pg-2 was not "
                                    f"archived within {gate.ARCHIVE_TIMEOUT} s"])
        kinds = [kind for kind, _ in events]
        self.assertNotIn("cluster", kinds)
        self.assertEqual([args for kind, args in events if kind == "apply"], ["Backup"])
        self.assertEqual(kinds.count("archived"), gate.ARCHIVE_TIMEOUT // gate.RESTORE_POLL)

class Standins(unittest.TestCase):
    def test_applies_the_list_server_side_then_waits_for_both_deployments(self):
        (apply, stdin), (wait, _) = gate.standins_commands(
            STANDIN_ENV, "the stand-ins list")[:2]
        self.assertEqual(stdin, "the stand-ins list")
        self.assertEqual(apply[:3], ["kubectl", "--context", gate.CONTEXT])
        self.assertIn("--server-side", apply)
        self.assertEqual(apply[-2:], ["-f", "-"])
        self.assertEqual(wait[:5], ["kubectl", "--context", gate.CONTEXT, "-n", "standins"])
        for name in ("seaweedfs", "opa", "recorder", "prometheus"):
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

    def test_direct_stand_ins_take_their_cpu_limits_from_the_stress_config(self):
        # ADR-053 §4: k6, OPA and SeaweedFS each run under a CPU limit; tuning values live in
        # gates/stress/config.json (ADR-000 rule 11).
        limits = gate.stress_standin_cpu()
        self.assertEqual(sorted(limits), ["opa", "seaweedfs"])
        for name, cpu in limits.items():
            with self.subTest(name=name):
                self.assertRegex(cpu, r"^[1-9][0-9]*m?$")

    def test_the_stress_config_says_its_numbers_are_placeholders_until_calibration(self):
        with open(gate.STRESS_CONFIG, encoding="utf-8") as f:
            config = json.load(f)
        self.assertIn("calibration", config["_comment"])

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


def upgrading(*times, name="polaris-pg", reason="UpgradingInstance"):
    return {"items": [{"involvedObject": {"kind": "Cluster", "name": name}, "reason": reason,
                       "firstTimestamp": t} for t in times]}


class RestartWindow(unittest.TestCase):
    END = "2026-10-12T10:37:58+00:00"

    def test_from_the_first_upgrading_instance_event_to_the_end_of_the_install(self):
        events = upgrading("2026-10-12T10:34:40Z", "2026-10-12T10:34:00Z")
        self.assertEqual(gate.restart_window(events, self.END),
                         ("2026-10-12T10:34:00Z", self.END))

    def test_no_window_without_a_restart_or_an_upgrade(self):
        self.assertIsNone(gate.restart_window(upgrading(), self.END))
        self.assertIsNone(gate.restart_window(upgrading("2026-10-12T10:34:00Z"), None))

    def test_other_clusters_reasons_and_later_restarts_do_not_count(self):
        self.assertIsNone(gate.restart_window(
            upgrading("2026-10-12T10:34:00Z", name="polaris-pg-restore"), self.END))
        self.assertIsNone(gate.restart_window(
            upgrading("2026-10-12T10:34:00Z", reason="CreatingInstance"), self.END))
        self.assertIsNone(gate.restart_window(upgrading("2026-10-12T10:40:00Z"), self.END))

    def test_the_events_are_read_for_polaris_pg_in_its_namespace(self):
        self.assertEqual(gate.restart_events_command("catalog-polaris"), [
            "kubectl", "--context", gate.CONTEXT, "-n", "catalog-polaris", "get", "events",
            "--field-selector", "involvedObject.name=polaris-pg,reason=UpgradingInstance",
            "-o", "json"])

    def test_check_traffic_names_the_window_and_passes_it_on(self):
        log = json.dumps({"proto": "pg", "event": "statement", "time": "2026-10-12T10:37:06Z",
                          "error": {"sqlstate": "57P01", "severity": "FATAL"}})
        rule = [{"name": "no failed statement", "must": "not_exist",
                 "match": {"event": "statement"},
                 "allow_during_restart": {"error.sqlstate": "57P01"}}]
        problems, lines = gate.check_traffic(log, [rule],
                                             restart=("2026-10-12T10:34:00Z", self.END))
        self.assertEqual(problems, [])
        self.assertIn("polaris-pg restarted during the upgrade from 2026-10-12T10:34:00Z",
                      lines[1])
        problems, _ = gate.check_traffic(log, [rule])
        self.assertEqual(len(problems), 1)

    def test_only_the_rules_kade_named_carry_an_allowance(self):
        # Two on 2026-10-12; the TLS negotiation on 2026-10-13 (nightly smoke 38385377721).
        with open(os.path.join(HERE, "expectations", "standins.json"), encoding="utf-8") as f:
            rules = {e["name"]: e.get("allow_during_restart")
                     for e in json.load(f)["expectations"]}
        self.assertEqual({n: a for n, a in rules.items() if a}, {
            "No PostgreSQL statement failed, but for a table that did not exist yet":
                {"error.sqlstate": "57P01", "error.severity": "FATAL"},
            "No upstream the recorder could not reach": {"listener": "pg"},
            "No PostgreSQL TLS negotiation failed": {"listener": "pg"}})


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
