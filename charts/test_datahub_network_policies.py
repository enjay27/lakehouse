"""Tests for charts/datahub-network-policies (ADR-048, ADR-066 §4, ADR-068 §3, ADR-070 §2): default
deny in Datahub's namespace, datahub-pg's two allow policies, datahub-kafka's and
datahub-opensearch's, the values schema, and kubeconform against the
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

CHART = os.path.join(HERE, "datahub-network-policies")
NAMESPACE = "catalog-datahub"
# What install/release.py passes from .env: the chart has no default for the operator's
# namespace.
LINT_VALUES = os.path.join(CHART, "ci", "lint-values.yaml")
NAME = "kubernetes.io/metadata.name"
INSTANCES = {"cnpg.io/podRole": "instance"}
KAFKA = {"app.kubernetes.io/name": "datahub-kafka"}
# The labels upstream's opensearch chart gives its pod, with release datahub-opensearch.
OPENSEARCH = {"app.kubernetes.io/name": "opensearch",
              "app.kubernetes.io/instance": "datahub-opensearch"}

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
    argv = ["helm", "template", release.DATAHUB_NETPOL_RELEASE, CHART, "--namespace", NAMESPACE]
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


class Rendered(unittest.TestCase):
    """ADR-066 §4: default deny, and datahub-pg's rules as polaris-pg's (ADR-048 §3)."""

    @classmethod
    def setUpClass(cls):
        cls.docs = render()
        cls.policies = by_name(cls.docs)

    def test_only_these_five_network_policies(self):
        self.assertEqual({(d["apiVersion"], d["kind"]) for d in self.docs},
                         {("networking.k8s.io/v1", "NetworkPolicy")})
        self.assertEqual(set(self.policies),
                         {"default-deny-ingress", "datahub-pg-clients", "datahub-pg-operator",
                          "datahub-kafka-clients", "datahub-opensearch-clients"})

    def test_every_pod_is_denied_ingress_by_default(self):
        deny = self.policies["default-deny-ingress"]["spec"]
        self.assertEqual(deny, {"podSelector": {}, "policyTypes": ["Ingress"]})

    def test_no_rule_allows_everyone(self):
        for name, policy in self.policies.items():
            self.assertEqual(policy["spec"]["policyTypes"], ["Ingress"], name)
            for rule in policy["spec"].get("ingress", []):
                self.assertTrue(rule.get("from"), name)
                self.assertTrue(rule.get("ports"), name)

    def test_postgres_from_its_clients_the_other_instances_and_cnpgs_join_pods(self):
        spec = self.policies["datahub-pg-clients"]["spec"]
        self.assertEqual(spec["podSelector"], {"matchLabels": INSTANCES})
        self.assertEqual(spec["ingress"], [{
            "ports": [{"protocol": "TCP", "port": 5432}],
            "from": [{"podSelector": {"matchLabels": {release.DATAHUB_PG_CLIENT_LABEL: "true"}}},
                     {"podSelector": {"matchLabels": INSTANCES}},
                     {"podSelector": {"matchLabels": {"cnpg.io/jobRole": "join"}}}]}])

    def test_the_client_label_is_datahubs_own(self):
        self.assertEqual(release.DATAHUB_PG_CLIENT_LABEL, "lakehouse/datahub-pg-client")
        self.assertNotEqual(release.DATAHUB_PG_CLIENT_LABEL, release.PG_CLIENT_LABEL)

    def test_postgres_and_its_status_port_from_the_operators_namespace(self):
        spec = self.policies["datahub-pg-operator"]["spec"]
        self.assertEqual(spec["podSelector"], {"matchLabels": INSTANCES})
        self.assertEqual(spec["ingress"], [{
            "ports": [{"protocol": "TCP", "port": 5432}, {"protocol": "TCP", "port": 8000}],
            "from": [{"namespaceSelector": {"matchLabels": {NAME: "cnpg-system"}}}]}])

    def test_kafka_on_9092_only_from_its_clients_in_this_namespace(self):
        # ADR-068 §3: plaintext behind this rule; the controller listens on localhost only.
        spec = self.policies["datahub-kafka-clients"]["spec"]
        self.assertEqual(spec["podSelector"], {"matchLabels": KAFKA})
        self.assertEqual(spec["ingress"], [{
            "ports": [{"protocol": "TCP", "port": 9092}],
            "from": [{"podSelector": {"matchLabels": {
                release.DATAHUB_KAFKA_CLIENT_LABEL: "true"}}}]}])

    def test_the_kafka_client_label_is_its_own(self):
        self.assertEqual(release.DATAHUB_KAFKA_CLIENT_LABEL, "lakehouse/datahub-kafka-client")
        self.assertNotIn(release.DATAHUB_KAFKA_CLIENT_LABEL,
                         (release.DATAHUB_PG_CLIENT_LABEL, release.PG_CLIENT_LABEL))

    def test_the_kafka_rule_selects_the_brokers_pod(self):
        out = subprocess.run(["helm", "template", release.DATAHUB_KAFKA_RELEASE,
                              release.DATAHUB_KAFKA_CHART, "--namespace", NAMESPACE],
                             capture_output=True, text=True, check=True).stdout
        (sts,) = [d for d in yaml.safe_load_all(out) if d and d["kind"] == "StatefulSet"]
        labels = sts["spec"]["template"]["metadata"]["labels"]
        for key, value in KAFKA.items():
            self.assertEqual(labels.get(key), value)
        self.assertEqual(sts["metadata"]["name"], release.DATAHUB_KAFKA)

    def test_opensearch_on_9200_only_from_its_clients_in_this_namespace(self):
        # ADR-070 §2: plain HTTP behind this rule; the transport port 9300 gets none.
        spec = self.policies["datahub-opensearch-clients"]["spec"]
        self.assertEqual(spec["podSelector"], {"matchLabels": OPENSEARCH})
        self.assertEqual(spec["ingress"], [{
            "ports": [{"protocol": "TCP", "port": 9200}],
            "from": [{"podSelector": {"matchLabels": {
                release.DATAHUB_OPENSEARCH_CLIENT_LABEL: "true"}}}]}])

    def test_the_opensearch_client_label_is_its_own(self):
        self.assertEqual(release.DATAHUB_OPENSEARCH_CLIENT_LABEL,
                         "lakehouse/datahub-opensearch-client")
        self.assertNotIn(release.DATAHUB_OPENSEARCH_CLIENT_LABEL,
                         (release.DATAHUB_KAFKA_CLIENT_LABEL, release.DATAHUB_PG_CLIENT_LABEL,
                          release.PG_CLIENT_LABEL))

    def test_the_opensearch_rule_selects_the_nodes_pod_only(self):
        out = subprocess.run(["helm", "template", release.DATAHUB_OPENSEARCH_RELEASE,
                              release.DATAHUB_OPENSEARCH_CHART, "--namespace", NAMESPACE,
                              "-f", release.DATAHUB_OPENSEARCH_VALUES],
                             capture_output=True, text=True, check=True).stdout
        (sts,) = [d for d in yaml.safe_load_all(out) if d and d["kind"] == "StatefulSet"]
        labels = sts["spec"]["template"]["metadata"]["labels"]
        for key, value in OPENSEARCH.items():
            self.assertEqual(labels.get(key), value)
        self.assertEqual(sts["metadata"]["name"], release.DATAHUB_OPENSEARCH)
        # app.kubernetes.io/name alone, opensearch, could match another release's pods.
        self.assertIn("app.kubernetes.io/instance", OPENSEARCH)

    def test_kubeconform_accepts_every_policy(self):
        result = helm_template()
        checked = subprocess.run(
            ["kubeconform", "-strict", "-summary",
             "-schema-location", kubeconform_schemas.location(SCHEMAS.name), "-"],
            input=result.stdout, capture_output=True, text=True, check=False)
        self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
        self.assertIn(f"Valid: {len(self.docs)}", checked.stdout)


class ValuesSchema(unittest.TestCase):
    def assertRefused(self, *sets, lint=True):
        result = helm_template(*sets, lint=lint)
        self.assertNotEqual(result.returncode, 0, f"accepted: {sets}")
        self.assertIn("schema", result.stderr.lower())

    def test_an_unknown_value_is_refused(self):
        self.assertRefused("namespaces.polaris=catalog-polaris")

    def test_the_operators_namespace_is_required(self):
        self.assertRefused(lint=False)

    def test_a_namespace_name_that_is_not_dns_is_refused(self):
        self.assertRefused("namespaces.operator=CNPG_System")


if __name__ == "__main__":
    unittest.main()
