"""Tests for gate.py. Run: python3 -m unittest discover -s gates"""

import hashlib
import io
import json
import os
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


if __name__ == "__main__":
    unittest.main()
