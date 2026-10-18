"""Tests for charts/datahub-network-policies (ADR-048, ADR-066 §4, ADR-068 §3, ADR-070 §2,
ADR-071 §5): default deny in Datahub's namespace, datahub-pg's two allow policies,
datahub-kafka's, datahub-opensearch's, GMS's and the frontend's, the values schema, and
kubeconform against the NetworkPolicy schema of the gate's Kubernetes version, pinned in
gates/pins.json.

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
# The labels upstream's datahub chart gives GMS's and the frontend's pods, with release datahub.
GMS = {"app.kubernetes.io/name": "datahub-gms", "app.kubernetes.io/instance": "datahub"}
FRONTEND = {"app.kubernetes.io/name": "datahub-frontend", "app.kubernetes.io/instance": "datahub"}
DATAHUB_CHART = os.path.join(HERE, "datahub")
DATAHUB_VALUES = os.path.join(DATAHUB_CHART, "values-lakehouse.yaml")
# The caller classes of ci/lint-values.yaml.
INGRESS = {"key": "lakehouse.example/caller", "value": "ingress"}
EMITTER = {"key": "lakehouse.example/caller", "value": "lineage-emitter"}

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

    def test_only_these_seven_network_policies(self):
        self.assertEqual({(d["apiVersion"], d["kind"]) for d in self.docs},
                         {("networking.k8s.io/v1", "NetworkPolicy")})
        self.assertEqual(set(self.policies),
                         {"default-deny-ingress", "datahub-pg-clients", "datahub-pg-operator",
                          "datahub-kafka-clients", "datahub-opensearch-clients",
                          "datahub-gms", "datahub-frontend"})

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

    def test_gms_on_8080_from_its_clients_and_the_emitters_namespaces(self):
        # ADR-071 §5: Datahub's own pods carry the client label; the emitters are a caller
        # class (ADR-048 §2). Metrics ports get no rule until Datahub's scrape contract.
        spec = self.policies["datahub-gms"]["spec"]
        self.assertEqual(spec["podSelector"], {"matchLabels": GMS})
        self.assertEqual(spec["ingress"], [{
            "ports": [{"protocol": "TCP", "port": 8080}],
            "from": [{"podSelector": {"matchLabels": {
                         release.DATAHUB_GMS_CLIENT_LABEL: "true"}}},
                     {"namespaceSelector": {"matchLabels": {EMITTER["key"]:
                                                            EMITTER["value"]}}}]}])

    def test_without_the_emitters_label_gms_admits_its_clients_only(self):
        policies = by_name(render("namespaces.operator=cnpg-system",
                                  f"callers.ingress.key={INGRESS['key']}",
                                  f"callers.ingress.value={INGRESS['value']}", lint=False))
        self.assertEqual(policies["datahub-gms"]["spec"]["ingress"], [{
            "ports": [{"protocol": "TCP", "port": 8080}],
            "from": [{"podSelector": {"matchLabels": {
                release.DATAHUB_GMS_CLIENT_LABEL: "true"}}}]}])

    def test_the_frontend_on_9002_from_the_ingress_namespace_only(self):
        spec = self.policies["datahub-frontend"]["spec"]
        self.assertEqual(spec["podSelector"], {"matchLabels": FRONTEND})
        self.assertEqual(spec["ingress"], [{
            "ports": [{"protocol": "TCP", "port": 9002}],
            "from": [{"namespaceSelector": {"matchLabels": {INGRESS["key"]:
                                                            INGRESS["value"]}}}]}])

    def test_without_the_ingress_label_the_frontend_has_no_rule(self):
        policies = by_name(render("namespaces.operator=cnpg-system",
                                  f"callers.emitter.key={EMITTER['key']}",
                                  f"callers.emitter.value={EMITTER['value']}", lint=False))
        self.assertNotIn("datahub-frontend", policies)
        self.assertIn("datahub-gms", policies)

    def test_the_gms_client_label_is_its_own(self):
        self.assertEqual(release.DATAHUB_GMS_CLIENT_LABEL, "lakehouse/datahub-gms-client")
        self.assertNotIn(release.DATAHUB_GMS_CLIENT_LABEL,
                         (release.DATAHUB_OPENSEARCH_CLIENT_LABEL,
                          release.DATAHUB_KAFKA_CLIENT_LABEL, release.DATAHUB_PG_CLIENT_LABEL,
                          release.PG_CLIENT_LABEL))

    def test_the_gms_and_frontend_rules_select_datahubs_pods(self):
        out = subprocess.run(["helm", "template", "datahub", DATAHUB_CHART, "--namespace",
                              NAMESPACE, "-f", DATAHUB_VALUES],
                             capture_output=True, text=True, check=True).stdout
        deployments = {d["metadata"]["name"]: d for d in yaml.safe_load_all(out)
                       if d and d["kind"] == "Deployment"}
        for name, selector in (("datahub-datahub-gms", GMS),
                               ("datahub-datahub-frontend", FRONTEND)):
            with self.subTest(deployment=name):
                labels = deployments[name]["spec"]["template"]["metadata"]["labels"]
                for key, value in selector.items():
                    self.assertEqual(labels.get(key), value)
                self.assertEqual(labels[release.DATAHUB_GMS_CLIENT_LABEL], "true")

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

    def test_an_unknown_caller_class_is_refused(self):
        self.assertRefused("callers.gateway.key=a", "callers.gateway.value=b")

    def test_a_caller_label_that_is_not_a_label_is_refused(self):
        self.assertRefused("callers.emitter.key=not a label")
        self.assertRefused("callers.emitter.value=a=b")


if __name__ == "__main__":
    unittest.main()
