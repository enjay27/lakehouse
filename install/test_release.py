"""Tests for release.py. Run: python3 -m unittest discover -s install"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import release  # noqa: E402

ENV = {"KUBE_CONTEXT": "kind-lakehouse-gate", "NS_CNPG_SYSTEM": "cnpg-system"}


def deployments(*images):
    return json.dumps({"items": [
        {"spec": {"template": {"spec": {"containers": [{"name": "manager", "image": i}]}}}}
        for i in images]})


class FakeKubectl:
    """Answers kubectl calls by their arguments; records every call."""

    def __init__(self, crd=True, deploys=deployments("ghcr.io/cloudnative-pg/cloudnative-pg:1.30.1")):
        self.crd, self.deploys, self.calls = crd, deploys, []

    def __call__(self, argv):
        self.calls.append(argv)
        if "crd" in argv:
            return (0, "customresourcedefinition.apiextensions.k8s.io/" + release.CNPG_CRD) \
                if self.crd else (1, "NotFound")
        if "deployments" in argv:
            return 0, self.deploys
        raise AssertionError(f"unexpected call {argv}")


class OperatorVersions(unittest.TestCase):
    def test_tag_is_read(self):
        self.assertEqual(release.operator_versions(
            deployments("ghcr.io/cloudnative-pg/cloudnative-pg:1.30.1")), ["1.30.1"])

    def test_tag_with_a_digest(self):
        self.assertEqual(release.operator_versions(deployments(
            "ghcr.io/cloudnative-pg/cloudnative-pg:1.30.1@sha256:" + "a" * 64)), ["1.30.1"])

    def test_a_mirror_registry_counts(self):
        self.assertEqual(release.operator_versions(
            deployments("registry.example.invalid/mirror/cloudnative-pg:1.29.3")), ["1.29.3"])

    def test_digest_only_or_other_images_give_nothing(self):
        self.assertEqual(release.operator_versions(deployments(
            "ghcr.io/cloudnative-pg/cloudnative-pg@sha256:" + "a" * 64,
            "ghcr.io/cloudnative-pg/postgresql:16")), [])


class Preconditions(unittest.TestCase):
    def check(self, kubectl, env=ENV, supported=("1.30",)):
        return release.check_preconditions(env, kubectl, list(supported))

    def test_present_and_supported_passes(self):
        problems, notes = self.check(FakeKubectl())
        self.assertEqual(problems, [])
        self.assertIn("1.30.1", "\n".join(notes))

    def test_every_call_names_the_context(self):
        kubectl = FakeKubectl()
        self.check(kubectl)
        for argv in kubectl.calls:
            self.assertEqual(argv[:3], ["kubectl", "--context", "kind-lakehouse-gate"])

    def test_the_deployments_are_read_in_the_operator_namespace(self):
        kubectl = FakeKubectl()
        self.check(kubectl)
        call = next(a for a in kubectl.calls if "deployments" in a)
        self.assertIn("-n", call)
        self.assertEqual(call[call.index("-n") + 1], "cnpg-system")

    def test_an_empty_context_stops_before_any_call(self):
        kubectl = FakeKubectl()
        problems, _ = self.check(kubectl, env={**ENV, "KUBE_CONTEXT": ""})
        self.assertTrue(problems)
        self.assertEqual(kubectl.calls, [])

    def test_no_crd_stops(self):
        problems, _ = self.check(FakeKubectl(crd=False))
        self.assertIn(release.CNPG_CRD, "\n".join(problems))

    def test_no_operator_stops(self):
        problems, _ = self.check(FakeKubectl(deploys=deployments()))
        self.assertTrue(problems)

    def test_an_unsupported_version_stops(self):
        problems, _ = self.check(FakeKubectl(
            deploys=deployments("ghcr.io/cloudnative-pg/cloudnative-pg:1.31.0")))
        self.assertIn("1.31.0", "\n".join(problems))

    def test_a_minor_prefix_does_not_match_a_longer_minor(self):
        problems, _ = self.check(FakeKubectl(
            deploys=deployments("ghcr.io/cloudnative-pg/cloudnative-pg:1.300.0")))
        self.assertTrue(problems)


class LoadEnv(unittest.TestCase):
    def test_env_file_then_environment_wins(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, ".env"), "w", encoding="utf-8") as f:
                f.write("KUBE_CONTEXT=from-file\nNS_CNPG_SYSTEM=cnpg-system\n")
            env = release.load_env(d, {"KUBE_CONTEXT": "from-environ"})
        self.assertEqual(env["KUBE_CONTEXT"], "from-environ")
        self.assertEqual(env["NS_CNPG_SYSTEM"], "cnpg-system")

    def test_no_env_file_uses_the_environment(self):
        with tempfile.TemporaryDirectory() as d:
            env = release.load_env(d, dict(ENV))
        self.assertEqual(env, ENV)


class Main(unittest.TestCase):
    def test_stops_with_exit_1_when_kube_context_is_empty(self):
        with tempfile.TemporaryDirectory() as d:
            result = subprocess.run(
                [sys.executable, release.__file__], cwd=d, capture_output=True, text=True,
                env={"PATH": os.environ.get("PATH", ""), "KUBE_CONTEXT": ""}, check=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn("KUBE_CONTEXT", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
