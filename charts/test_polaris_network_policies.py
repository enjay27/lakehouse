"""Tests for charts/polaris-network-policies (ADR-044): default deny in Polaris's namespace and
one allow policy per rule of ADR-044 §3, the values schema, and kubeconform against the
NetworkPolicy schema of the gate's Kubernetes version, pinned in gates/pins.json.

Run: python3 -m unittest discover -s charts
Needs helm and kubeconform on PATH (`python3 gates/gate.py tools BIN_DIR helm kubeconform`) and
PyYAML (`pip install --require-hashes -r charts/requirements.txt`). Missing tools fail the run.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "gates"))
sys.path.insert(0, os.path.join(ROOT, "install"))
import gate  # noqa: E402
import kubeconform_schemas  # noqa: E402
import release  # noqa: E402

CHART = os.path.join(HERE, "polaris-network-policies")
NAMESPACE = "catalog-polaris"
# What install/release.py passes from .env, with example labels (ADR-002): the chart has no
# default for the operator's and Datahub's namespaces.
LINT_VALUES = os.path.join(CHART, "ci", "lint-values.yaml")
NAME = "kubernetes.io/metadata.name"
INSTANCES = {"cnpg.io/podRole": "instance"}

SCHEMAS = None


def setUpModule():
    global SCHEMAS
    missing = [t for t in ("helm", "kubeconform") if not shutil.which(t)]
    if missing:
        raise RuntimeError(f"{', '.join(missing)} not on PATH: run "
                           "`python3 gates/gate.py tools BIN_DIR helm kubeconform` first")
    SCHEMAS = tempfile.TemporaryDirectory()
    pin = gate.load_pins()["kubernetes_schemas"]["networkpolicy"]
    path = os.path.join(SCHEMAS.name, "networking.k8s.io", "networkpolicy_v1.json")
    os.makedirs(os.path.dirname(path))
    with open(path, "wb") as f:
        f.write(gate.fetch(pin["url"], pin["sha256"]))


def tearDownModule():
    SCHEMAS.cleanup()


def helm_template(*sets, lint=True):
    argv = ["helm", "template", release.POLARIS_NETPOL_RELEASE, CHART, "--namespace", NAMESPACE]
    if lint:
        argv += ["-f", LINT_VALUES]
    for s in sets:
        argv += ["--set", s]
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def render(*sets, lint=True):
    result = helm_template(*sets, lint=lint)
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return [d for d in yaml.safe_load_all(result.stdout) if d]


def by_name(docs):
    return {d["metadata"]["name"]: d for d in docs}


def kubeconform(manifests_text):
    return subprocess.run(
        ["kubeconform", "-strict", "-summary",
         "-schema-location", kubeconform_schemas.location(SCHEMAS.name), "-"],
        input=manifests_text, capture_output=True, text=True, check=False)


def namespace(key, value):
    return {"namespaceSelector": {"matchLabels": {key: value}}}


class Rendered(unittest.TestCase):
    """ADR-044 §3, with every caller class labelled and the console enabled."""

    @classmethod
    def setUpClass(cls):
        cls.docs = render("console.enabled=true")
        cls.policies = by_name(cls.docs)

    def test_only_network_policies(self):
        self.assertEqual({(d["apiVersion"], d["kind"]) for d in self.docs},
                         {("networking.k8s.io/v1", "NetworkPolicy")})

    def test_every_pod_is_denied_ingress_by_default(self):
        deny = self.policies["default-deny-ingress"]["spec"]
        self.assertEqual(deny, {"podSelector": {}, "policyTypes": ["Ingress"]})

    def test_every_policy_restricts_ingress_only(self):
        for name, policy in self.policies.items():
            self.assertEqual(policy["spec"]["policyTypes"], ["Ingress"], name)

    def test_no_rule_allows_everyone(self):
        # An ingress rule without `from` allows every source.
        for name, policy in self.policies.items():
            for rule in policy["spec"].get("ingress", []):
                self.assertTrue(rule.get("from"), name)
                self.assertTrue(rule.get("ports"), name)

    def test_polaris_api_from_gateway_query_datahub_and_ingress(self):
        spec = self.policies["polaris-api"]["spec"]
        self.assertEqual(spec["podSelector"], {"matchLabels": {
            "app.kubernetes.io/name": "polaris", "app.kubernetes.io/instance": release.POLARIS}})
        self.assertEqual(spec["ingress"], [{
            "ports": [{"protocol": "TCP", "port": 8181}],
            "from": [namespace("example.org/caller", "gateway"),
                     namespace("example.org/caller", "query"),
                     namespace(NAME, "catalog-datahub"),
                     namespace("example.org/caller", "ingress")]}])

    def test_polaris_management_from_the_metrics_scraper_only(self):
        spec = self.policies["polaris-management"]["spec"]
        self.assertEqual(spec["podSelector"],
                         self.policies["polaris-api"]["spec"]["podSelector"])
        self.assertEqual(spec["ingress"], [{
            "ports": [{"protocol": "TCP", "port": 8182}],
            "from": [namespace("example.org/caller", "metrics")]}])

    def test_the_console_from_the_ingress_controller_only(self):
        spec = self.policies["polaris-console"]["spec"]
        self.assertEqual(spec["podSelector"], {"matchLabels": {
            "app.kubernetes.io/name": "polaris-console",
            "app.kubernetes.io/instance": release.CONSOLE}})
        self.assertEqual(spec["ingress"], [{
            "ports": [{"protocol": "TCP", "port": 8080}],
            "from": [namespace("example.org/caller", "ingress")]}])

    def test_postgres_from_its_clients_the_other_instances_and_cnpgs_join_pods(self):
        # ADR-048 §3: a join pod clones the primary to make a replica (#103).
        spec = self.policies["polaris-pg-clients"]["spec"]
        self.assertEqual(spec["podSelector"], {"matchLabels": INSTANCES})
        self.assertEqual(spec["ingress"], [{
            "ports": [{"protocol": "TCP", "port": 5432}],
            "from": [{"podSelector": {"matchLabels": {release.PG_CLIENT_LABEL: "true"}}},
                     {"podSelector": {"matchLabels": INSTANCES}},
                     {"podSelector": {"matchLabels": {"cnpg.io/jobRole": "join"}}}]}])

    def test_postgres_and_its_status_port_from_the_operators_namespace(self):
        spec = self.policies["polaris-pg-operator"]["spec"]
        self.assertEqual(spec["podSelector"], {"matchLabels": INSTANCES})
        self.assertEqual(spec["ingress"], [{
            "ports": [{"protocol": "TCP", "port": 5432}, {"protocol": "TCP", "port": 8000}],
            "from": [namespace(NAME, "cnpg-system")]}])

    def test_postgres_metrics_from_the_metrics_scraper_only(self):
        # ADR-046 §4: CNPG's exporter on each instance.
        spec = self.policies["polaris-pg-metrics"]["spec"]
        self.assertEqual(spec["podSelector"], {"matchLabels": INSTANCES})
        self.assertEqual(spec["ingress"], [{
            "ports": [{"protocol": "TCP", "port": 9187}],
            "from": [namespace("example.org/caller", "metrics")]}])

    def test_kubeconform_accepts_every_policy(self):
        result = helm_template("console.enabled=true")
        checked = kubeconform(result.stdout)
        self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
        self.assertIn(f"Valid: {len(self.docs)}", checked.stdout)


class PortsMatchTheCharts(unittest.TestCase):
    """The ports this chart opens are the ones Polaris's and the console's values use."""

    @staticmethod
    def values(chart, name="values.yaml"):
        with open(os.path.join(HERE, chart, name), encoding="utf-8") as f:
            return yaml.safe_load(f)

    def test_polaris_api_and_management_ports(self):
        upstream = self.values("polaris")
        ours = self.values("polaris", "values-lakehouse.yaml")
        self.assertEqual([p["port"] for p in upstream["service"]["ports"]], [8181])
        self.assertEqual([p["port"] for p in ours["managementService"]["ports"]], [8182])

    def test_the_console_port(self):
        self.assertEqual(self.values("polaris-console")["service"]["port"], 8080)
        self.assertNotIn("service", self.values("polaris-console", "values-lakehouse.yaml"))


class EmptyCallers(unittest.TestCase):
    """ADR-044 §2: an empty caller class allows no namespace; its rule is left out, never
    rendered without sources."""

    def test_no_labels_leave_polaris_api_to_datahub_only(self):
        docs = by_name(render(*self.required(), lint=False))
        self.assertEqual(docs["polaris-api"]["spec"]["ingress"][0]["from"],
                         [namespace(NAME, "catalog-datahub")])
        self.assertNotIn("polaris-management", docs)
        self.assertNotIn("polaris-pg-metrics", docs)

    def test_the_console_disabled_has_no_policy_and_no_ingress_on_the_api(self):
        docs = by_name(render())
        self.assertNotIn("polaris-console", docs)
        sources = docs["polaris-api"]["spec"]["ingress"][0]["from"]
        self.assertNotIn(namespace("example.org/caller", "ingress"), sources)

    def test_the_console_enabled_without_an_ingress_label_has_no_policy(self):
        docs = by_name(render("console.enabled=true", *self.required(), lint=False))
        self.assertNotIn("polaris-console", docs)

    def test_postgres_rules_need_no_caller_label(self):
        docs = by_name(render(*self.required(), lint=False))
        self.assertIn("polaris-pg-clients", docs)
        self.assertIn("polaris-pg-operator", docs)
        self.assertIn("default-deny-ingress", docs)

    @staticmethod
    def required():
        return ("namespaces.datahub=catalog-datahub", "namespaces.operator=cnpg-system")


class ValuesSchema(unittest.TestCase):
    def assertRefused(self, *sets, lint=True):
        result = helm_template(*sets, lint=lint)
        self.assertNotEqual(result.returncode, 0, f"accepted: {sets}")
        self.assertIn("schema", result.stderr.lower())

    def test_an_unknown_value_is_refused(self):
        self.assertRefused("console.enable=true")

    def test_an_unknown_caller_class_is_refused(self):
        self.assertRefused("callers.lineage.key=a", "callers.lineage.value=b")

    def test_the_operators_namespace_is_required(self):
        self.assertRefused("namespaces.datahub=catalog-datahub", lint=False)

    def test_a_label_key_with_a_space_is_refused(self):
        self.assertRefused("callers.gateway.key=bad key")

    def test_a_label_value_with_a_slash_is_refused(self):
        self.assertRefused("callers.gateway.value=a/b")

    def test_a_caller_without_a_value_is_refused(self):
        self.assertRefused("namespaces.datahub=catalog-datahub", "namespaces.operator=cnpg-system",
                           "callers.gateway.key=only-key", lint=False)

    def test_a_namespace_name_that_is_not_dns_is_refused(self):
        self.assertRefused("namespaces.operator=CNPG_System")


if __name__ == "__main__":
    unittest.main()
