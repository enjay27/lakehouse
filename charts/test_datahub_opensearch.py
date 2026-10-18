"""Tests for charts/datahub-opensearch (ADR-070, ADR-025, ADR-016): upstream's opensearch chart
2.38.0, unchanged, rendered with values-lakehouse.yaml; every key of those values found in
upstream's values.yaml, since upstream has no values schema to refuse a misspelt one; every
value ADR-070 §1 to §3 sets checked on the rendered output; and kubeconform against the
StatefulSet, Service and ConfigMap schemas of the gate's Kubernetes version, pinned in
gates/pins.json.

Run: python3 -m unittest discover -s charts
Needs helm and kubeconform on PATH (`python3 gates/gate.py tools BIN_DIR helm kubeconform`) and
PyYAML (`pip install --require-hashes -r charts/requirements.txt`). Missing tools fail the run.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "gates"))
sys.path.insert(0, os.path.join(ROOT, "install"))
import gate  # noqa: E402
import release  # noqa: E402

NAMESPACE = "catalog-datahub"
IMAGE_RE = re.compile(r"^opensearchproject/opensearch:2\.19\.6@sha256:[0-9a-f]{64}$")
# The image's user, `opensearch`, and the pod's fsGroup in upstream's chart.
OPENSEARCH_USER = 1000
# Values whose contents are a Kubernetes object the chart passes through as it is: their keys are
# Kubernetes' own, checked by kubeconform's strict schema, not upstream's values.yaml.
PASS_THROUGH = {"resources"}

SCHEMAS = None


def setUpModule():
    global SCHEMAS
    missing = [t for t in ("helm", "kubeconform") if not shutil.which(t)]
    if missing:
        raise RuntimeError(f"{', '.join(missing)} not on PATH: run "
                           "`python3 gates/gate.py tools BIN_DIR helm kubeconform` first")
    SCHEMAS = tempfile.TemporaryDirectory()
    pins = gate.load_pins()["kubernetes_schemas"]
    for name in ("statefulset", "service", "configmap"):
        # Each file under its upstream name, as kubeconform looks it up.
        path = os.path.join(SCHEMAS.name, pins[name]["url"].rsplit("/", 1)[1])
        with open(path, "wb") as f:
            f.write(gate.fetch(pins[name]["url"], pins[name]["sha256"]))


def tearDownModule():
    SCHEMAS.cleanup()


def load(name):
    with open(os.path.join(release.DATAHUB_OPENSEARCH_CHART, name), encoding="utf-8") as f:
        return yaml.safe_load(f)


def unknown_keys(ours, upstream, prefix=""):
    """The dotted keys of `ours` that upstream's values do not have; PASS_THROUGH's contents and
    lists are not walked."""
    found = []
    for key, value in ours.items():
        path = prefix + key
        if not isinstance(upstream, dict) or key not in upstream:
            found.append(path)
        elif isinstance(value, dict) and path not in PASS_THROUGH:
            found += unknown_keys(value, upstream[key], path + ".")
    return found


def helm_template(*sets):
    argv = ["helm", "template", release.DATAHUB_OPENSEARCH_RELEASE,
            release.DATAHUB_OPENSEARCH_CHART, "--namespace", NAMESPACE,
            "-f", release.DATAHUB_OPENSEARCH_VALUES]
    for s in sets:
        argv += ["--set", s]
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def render(*sets):
    result = helm_template(*sets)
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return [d for d in yaml.safe_load_all(result.stdout) if d]


def kubeconform(manifests_text):
    # kubeconform's own naming, which also covers core kinds: -apps-v1 or -v1.
    location = os.path.join(SCHEMAS.name, "{{ .ResourceKind }}{{ .KindSuffix }}.json")
    return subprocess.run(["kubeconform", "-strict", "-summary", "-schema-location", location,
                           "-"], input=manifests_text, capture_output=True, text=True,
                          check=False)


class Upstream(unittest.TestCase):
    def test_the_chart_is_upstreams_2_38_0_with_opensearch_2_19_6(self):
        chart = load("Chart.yaml")
        self.assertEqual((chart["name"], chart["version"], chart["appVersion"]),
                         ("opensearch", "2.38.0", "2.19.6"))

    def test_upstream_has_no_values_schema(self):
        # Why the key check below exists (ADR-070 §1).
        self.assertFalse(os.path.exists(os.path.join(release.DATAHUB_OPENSEARCH_CHART,
                                                     "values.schema.json")))

    def test_every_key_of_our_values_is_one_of_upstreams(self):
        self.assertEqual(unknown_keys(load("values-lakehouse.yaml"), load("values.yaml")), [])

    def test_a_misspelt_key_is_found(self):
        upstream = load("values.yaml")
        self.assertEqual(unknown_keys({"singelNode": True}, upstream), ["singelNode"])
        self.assertEqual(unknown_keys({"persistence": {"enableInitChwon": False}}, upstream),
                         ["persistence.enableInitChwon"])
        self.assertEqual(unknown_keys({"resources": {"limits": {"memory": "2Gi"}}}, upstream),
                         [])


class Rendered(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.docs = render()
        cls.named = {(d["kind"], d["metadata"]["name"]): d for d in cls.docs}
        cls.sts = cls.named[("StatefulSet", release.DATAHUB_OPENSEARCH)]
        cls.pod = cls.sts["spec"]["template"]["spec"]
        (cls.container,) = cls.pod["containers"]
        cls.env = {e["name"]: e for e in cls.container["env"]}

    def value(self, name):
        return self.env[name].get("value")

    def test_the_kinds_and_names_the_install_waits_for(self):
        # ADR-070 §1: fullnameOverride and masterService name the StatefulSet and the Service.
        self.assertEqual(sorted(self.named), [
            ("ConfigMap", "datahub-opensearch-config"), ("Service", "datahub-opensearch"),
            ("Service", "datahub-opensearch-headless"), ("StatefulSet", "datahub-opensearch")])
        self.assertEqual(release.DATAHUB_OPENSEARCH, "datahub-opensearch")
        self.assertEqual(release.DATAHUB_OPENSEARCH_RELEASE, "datahub-opensearch")

    def test_no_disruption_budget_and_upstreams_default_would_render_one(self):
        # ADR-070 §1: with one replica, a budget allowing no disruption blocks every drain.
        self.assertNotIn("PodDisruptionBudget", [d["kind"] for d in self.docs])
        self.assertIn("PodDisruptionBudget", [d["kind"] for d in render("maxUnavailable=1")])

    def test_one_node_with_every_role(self):
        self.assertEqual(self.sts["spec"]["replicas"], 1)
        self.assertEqual(self.value("discovery.type"), "single-node")
        roles = set(filter(None, self.value("node.roles").split(",")))
        self.assertLessEqual({"master", "ingest", "data"}, roles)

    def test_every_container_runs_opensearch_2_19_6_by_tag_and_digest(self):
        for c in self.pod["containers"] + self.pod.get("initContainers", []):
            with self.subTest(container=c["name"]):
                self.assertRegex(c["image"], IMAGE_RE)
        # Upstream cannot read it from a tag with a digest, so the values set it.
        self.assertEqual(self.sts["metadata"]["annotations"]["majorVersion"], "2")

    def test_no_root_container_and_no_kernel_setting(self):
        # ADR-070 §1, §3: no fsgroup-volume or sysctl init container, no sysctls.
        self.assertEqual([c["name"] for c in self.pod.get("initContainers", [])],
                         ["configfile"])
        sc = self.pod["securityContext"]
        self.assertEqual((sc["runAsUser"], sc["fsGroup"]), (OPENSEARCH_USER, OPENSEARCH_USER))
        self.assertNotIn("sysctls", sc)
        for c in self.pod["containers"] + self.pod["initContainers"]:
            with self.subTest(container=c["name"]):
                csc = c["securityContext"]
                self.assertTrue(csc["runAsNonRoot"])
                self.assertEqual(csc["runAsUser"], OPENSEARCH_USER)
                self.assertFalse(csc.get("privileged", False))
                self.assertEqual(csc["capabilities"]["drop"], ["ALL"])

    def test_the_security_plugin_is_off_and_nothing_secret_is_mounted(self):
        # ADR-070 §2: plain HTTP on 9200 with no login, no credential, no Secret.
        self.assertEqual(self.value("DISABLE_SECURITY_PLUGIN"), "true")
        self.assertNotIn("Secret", [d["kind"] for d in self.docs])
        self.assertNotIn("OPENSEARCH_INITIAL_ADMIN_PASSWORD", self.env)
        for e in self.container["env"]:
            self.assertNotIn("secretKeyRef", e.get("valueFrom", {}), e["name"])
        self.assertEqual(sorted(v["name"] for v in self.pod["volumes"]),
                         ["config", "config-emptydir"])

    def test_the_performance_analyzers_agent_is_off(self):
        self.assertEqual(self.value("DISABLE_PERFORMANCE_ANALYZER_AGENT_CLI"), "true")

    def test_the_heap_requests_and_limit_are_the_tuning_values(self):
        # ADR-070 §3: tuning values, not measured.
        self.assertEqual(self.value("OPENSEARCH_JAVA_OPTS"), "-Xms1g -Xmx1g")
        self.assertEqual(self.container["resources"], {
            "requests": {"cpu": "250m", "memory": "1536Mi"}, "limits": {"memory": "2Gi"}})

    def test_one_10_gib_volume_on_the_default_storage_class(self):
        (claim,) = self.sts["spec"]["volumeClaimTemplates"]
        self.assertEqual(claim["spec"]["accessModes"], ["ReadWriteOnce"])
        self.assertEqual(claim["spec"]["resources"]["requests"]["storage"], "10Gi")
        self.assertNotIn("storageClassName", claim["spec"])
        mounts = {m["name"]: m["mountPath"] for m in self.container["volumeMounts"]}
        self.assertEqual(mounts[claim["metadata"]["name"]], "/usr/share/opensearch/data")

    def test_startup_and_readiness_on_9200_and_no_liveness_probe(self):
        # ADR-070 §1: upstream's probes, as ADR-068 §1 argues for one broker.
        self.assertEqual(self.container["startupProbe"]["tcpSocket"]["port"], 9200)
        self.assertEqual(self.container["readinessProbe"]["tcpSocket"]["port"], 9200)
        self.assertNotIn("livenessProbe", self.container)

    def test_the_service_serves_http_on_9200_to_the_statefulsets_pod(self):
        spec = self.named[("Service", "datahub-opensearch")]["spec"]
        self.assertIn(("http", 9200), [(p["name"], p["port"]) for p in spec["ports"]])
        labels = self.sts["spec"]["template"]["metadata"]["labels"]
        for key, value in spec["selector"].items():
            self.assertEqual(labels[key], value)

    def test_kubeconform_accepts_the_rendered_chart(self):
        result = kubeconform(helm_template().stdout)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Valid: 4", result.stdout)


if __name__ == "__main__":
    unittest.main()
