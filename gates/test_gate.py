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


def helm_tarball(binary=b"helm-binary"):
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        info = tarfile.TarInfo("linux-amd64/helm")
        info.size = len(binary)
        tar.addfile(info, io.BytesIO(binary))
    return out.getvalue()


class Pins(unittest.TestCase):
    def test_the_committed_pins_are_complete(self):
        pins = gate.load_pins(os.path.join(HERE, "pins.json"))
        for tool in ("kind", "kubectl", "helm", "cnpg_chart"):
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
        tarball = helm_tarball()
        blobs = {"https://k/kind": b"kind-binary", "https://k/kubectl": b"kubectl-binary",
                 "https://k/helm.tgz": tarball}
        pins = {"kind": {"url": "https://k/kind", "sha256": sha(b"kind-binary")},
                "kubectl": {"url": "https://k/kubectl", "sha256": sha(b"kubectl-binary")},
                "helm": {"url": "https://k/helm.tgz", "sha256": sha(tarball)}}
        with tempfile.TemporaryDirectory() as d:
            gate.tools(pins, d, get=blobs.__getitem__)
            for name, content in (("kind", b"kind-binary"), ("kubectl", b"kubectl-binary"),
                                  ("helm", b"helm-binary")):
                with self.subTest(tool=name):
                    path = os.path.join(d, name)
                    with open(path, "rb") as f:
                        self.assertEqual(f.read(), content)
                    self.assertTrue(os.access(path, os.X_OK))

    def test_a_bad_hash_writes_nothing(self):
        pins = {"kind": {"url": "https://k/kind", "sha256": "0" * 64},
                "kubectl": {"url": "https://k/kubectl", "sha256": "0" * 64},
                "helm": {"url": "https://k/helm.tgz", "sha256": "0" * 64}}
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                gate.tools(pins, d, get=lambda u: b"x")
            self.assertEqual(os.listdir(d), [])


class Commands(unittest.TestCase):
    PINS = {"node_image": {"image": "kindest/node:v1.36.4@sha256:" + "a" * 64}}

    def test_up_creates_the_three_node_cluster_then_installs_cnpg(self):
        create, helm = gate.up_commands(self.PINS, "/tmp/chart.tgz", "cnpg-system")
        self.assertEqual(create[:4], ["kind", "create", "cluster", "--name"])
        self.assertEqual(create[create.index("--name") + 1], gate.CLUSTER)
        self.assertEqual(create[create.index("--image") + 1], self.PINS["node_image"]["image"])
        self.assertTrue(create[create.index("--config") + 1].endswith("gates/kind.yaml"))
        self.assertEqual(helm[:3], ["helm", "--kube-context", gate.CONTEXT])
        self.assertIn("/tmp/chart.tgz", helm)
        self.assertEqual(helm[helm.index("--namespace") + 1], "cnpg-system")
        self.assertIn("--wait", helm)

    def test_smoke_creates_waits_for_and_removes_a_cluster(self):
        steps = gate.smoke_commands()
        for argv, _ in steps:
            self.assertEqual(argv[:3], ["kubectl", "--context", gate.CONTEXT])
            self.assertIn("-n", argv)
        verbs = [argv[argv.index("-n") + 2] for argv, _ in steps]
        self.assertEqual(verbs, ["create", "apply", "wait", "delete"])
        manifest = steps[1][1]
        self.assertIn("kind: Cluster", manifest)
        self.assertIn("instances: 1", manifest)

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

    def test_an_unknown_install_option_exits_2(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(gate.main(["gate.py", "install", "--upgrade"]), 2)


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


if __name__ == "__main__":
    unittest.main()
