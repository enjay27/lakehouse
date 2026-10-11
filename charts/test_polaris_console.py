"""Tests for charts/polaris-console (ADR-028, ADR-016): upstream's chart at the pinned
polaris-tools commit, rendered with values-lakehouse.yaml and the values release.py takes from
`.env`, each setting that matters compared with the chart's own default.

The rendered kinds are core Kubernetes kinds, for which kubeconform has no schema here; the
dry-run gate validates them server-side (ADR-016).

Run: python3 -m unittest discover -s charts
Needs helm on PATH (`python3 gates/gate.py tools BIN_DIR helm kubeconform`) and PyYAML
(`pip install --require-hashes -r charts/requirements.txt`).
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "install"))
import release  # noqa: E402

NAMESPACE = "catalog-polaris"
ENV = {"POLARIS_CONSOLE_ENABLED": "true",
       "POLARIS_CONSOLE_ORIGIN": "http://polaris-console.catalog-polaris.svc.cluster.local:8080",
       "POLARIS_CONSOLE_API_URL": "http://polaris.catalog-polaris.svc.cluster.local:8181"}


def setUpModule():
    if not shutil.which("helm"):
        raise RuntimeError("helm not on PATH: run "
                           "`python3 gates/gate.py tools BIN_DIR helm kubeconform` first")


def chart_defaults():
    with open(os.path.join(release.CONSOLE_CHART, "values.yaml"), encoding="utf-8") as f:
        return yaml.safe_load(f)


def image_source():
    with open(os.path.join(ROOT, "images", "polaris-console", "source.json"),
              encoding="utf-8") as f:
        return json.load(f)


def render(env=ENV, *sets):
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        json.dump(release.console_values(env), f)
    try:
        argv = ["helm", "template", release.CONSOLE, release.CONSOLE_CHART,
                "--namespace", NAMESPACE, "-f", release.CONSOLE_VALUES, "-f", f.name]
        for s in sets:
            argv += ["--set", s]
        result = subprocess.run(argv, capture_output=True, text=True, check=False)
    finally:
        os.unlink(f.name)
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return [d for d in yaml.safe_load_all(result.stdout) if d]


def one(docs, kind, name=release.CONSOLE):
    (doc,) = [d for d in docs if d["kind"] == kind and d["metadata"]["name"] == name]
    return doc


class Rendered(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.docs = render()
        cls.deployment = one(cls.docs, "Deployment")
        (cls.container,) = cls.deployment["spec"]["template"]["spec"]["containers"]
        cls.config = one(cls.docs, "ConfigMap")["data"]
        cls.defaults = chart_defaults()

    def test_the_kinds_and_names_the_install_waits_for(self):
        self.assertEqual(sorted((d["kind"], d["metadata"]["name"]) for d in self.docs), [
            ("ConfigMap", "polaris-console"), ("Deployment", "polaris-console"),
            ("Service", "polaris-console"), ("ServiceAccount", "polaris-console")])

    def test_the_image_is_the_one_built_from_the_pinned_commit_never_pulled_latest(self):
        commit = image_source()["commit"]
        self.assertEqual(self.container["image"], f"lakehouse/polaris-console:{commit[:12]}")
        self.assertEqual(self.container["imagePullPolicy"], "IfNotPresent")
        self.assertEqual(self.defaults["image"]["tag"], "latest")

    def test_the_browser_reaches_polaris_at_the_api_url_from_env(self):
        api = ENV["POLARIS_CONSOLE_API_URL"]
        self.assertEqual(self.config["VITE_POLARIS_API_URL"], api)
        self.assertEqual(self.config["VITE_OAUTH_TOKEN_URL"],
                         f"{api}/api/catalog/v1/oauth/tokens")
        self.assertNotEqual(self.defaults["config"]["api"]["polarisApiUrl"], api)

    def test_the_realm_is_the_one_the_bootstrap_creates(self):
        self.assertEqual(self.config["VITE_POLARIS_REALM"], release.POLARIS_REALM)
        self.assertEqual(self.config["VITE_POLARIS_REALM_HEADER_NAME"], "Polaris-Realm")

    def test_no_sso_sign_in_maintainers_use_their_own_principals(self):
        for key in ("VITE_OIDC_ISSUER_URL", "VITE_OIDC_CLIENT_ID"):
            self.assertNotIn(key, self.config)

    def test_served_on_8080_as_the_gate_names_its_origin(self):
        (port,) = one(self.docs, "Service")["spec"]["ports"]
        self.assertEqual(port["port"], 8080)
        self.assertTrue(ENV["POLARIS_CONSOLE_ORIGIN"].endswith(":8080"))

    def test_runs_as_upstream_hardens_it(self):
        security = self.container["securityContext"]
        self.assertIs(security["runAsNonRoot"], True)
        self.assertIs(security["allowPrivilegeEscalation"], False)
        self.assertEqual(security["capabilities"]["drop"], ["ALL"])

    def test_upstreams_schema_refuses_an_unknown_value(self):
        with self.assertRaises(AssertionError):
            render(ENV, "service.port=not-a-port")


if __name__ == "__main__":
    unittest.main()
