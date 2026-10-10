"""Tests for charts/polaris-postgres (ADR-008, ADR-010, ADR-016): the rendered `Cluster`, the
values schema, and kubeconform against CNPG's `Cluster` CRD from the gate's pinned chart.

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
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "gates"))
sys.path.insert(0, os.path.join(ROOT, "install"))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import gate  # noqa: E402
import init_env  # noqa: E402
import kubeconform_schemas  # noqa: E402
import release  # noqa: E402

CHART = os.path.join(HERE, "polaris-postgres")
NAMESPACE = "catalog-polaris"
# CNPG's own defaults for the fields this chart sets (CNPG 1.30 API reference).
CNPG_DEFAULTS = {"instances": 1, "podAntiAffinityType": "preferred"}
IMAGE_RE = re.compile(
    r"^ghcr\.io/cloudnative-pg/postgresql:16\.\d+-\d{12}-minimal-trixie@sha256:[0-9a-f]{64}$")

SCHEMAS = None


def setUpModule():
    global SCHEMAS
    missing = [t for t in ("helm", "kubeconform") if not shutil.which(t)]
    if missing:
        raise RuntimeError(f"{', '.join(missing)} not on PATH: run "
                           "`python3 gates/gate.py tools BIN_DIR helm kubeconform` first")
    SCHEMAS = tempfile.TemporaryDirectory()
    pin = gate.load_pins()["cnpg_chart"]
    archive = os.path.join(SCHEMAS.name, "cnpg.tgz")
    with open(archive, "wb") as f:
        f.write(gate.fetch(pin["url"], pin["sha256"]))
    crds = subprocess.run(["helm", "template", "cnpg", archive, "--show-only",
                           "templates/crds/crds.yaml"], capture_output=True, text=True, check=True)
    kubeconform_schemas.write(yaml.safe_load_all(crds.stdout), SCHEMAS.name)


def tearDownModule():
    SCHEMAS.cleanup()


def helm_template(*sets):
    argv = ["helm", "template", release.POLARIS_PG_RELEASE, CHART, "--namespace", NAMESPACE]
    for s in sets:
        argv += ["--set", s]
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def render(*sets):
    result = helm_template(*sets)
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return [d for d in yaml.safe_load_all(result.stdout) if d]


def kubeconform(manifests_text):
    return subprocess.run(
        ["kubeconform", "-strict", "-summary",
         "-schema-location", kubeconform_schemas.location(SCHEMAS.name), "-"],
        input=manifests_text, capture_output=True, text=True, check=False)


class Rendered(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.docs = render()
        cls.spec = cls.docs[0]["spec"]

    def test_one_cluster_named_as_the_install_script_waits_for(self):
        self.assertEqual([(d["apiVersion"], d["kind"]) for d in self.docs],
                         [("postgresql.cnpg.io/v1", "Cluster")])
        self.assertEqual(self.docs[0]["metadata"]["name"], release.POLARIS_PG)

    def test_three_instances_where_cnpg_defaults_to_one(self):
        self.assertEqual(self.spec["instances"], 3)
        self.assertNotEqual(self.spec["instances"], CNPG_DEFAULTS["instances"])

    def test_postgresql_16_pinned_by_tag_and_digest(self):
        self.assertRegex(self.spec["imageName"], IMAGE_RE)

    def test_instances_never_share_a_node(self):
        affinity = self.spec["affinity"]
        self.assertIs(affinity["enablePodAntiAffinity"], True)
        self.assertEqual(affinity["podAntiAffinityType"], "required")
        self.assertNotEqual(affinity["podAntiAffinityType"], CNPG_DEFAULTS["podAntiAffinityType"])
        self.assertEqual(affinity["topologyKey"], "kubernetes.io/hostname")

    def test_credentials_come_from_the_secret_init_env_creates(self):
        secret = next(s for s in init_env.SECRETS if s.name == release.POLARIS_DB_SECRET)
        initdb = self.spec["bootstrap"]["initdb"]
        self.assertEqual(initdb["secret"], {"name": secret.name})
        self.assertEqual(initdb["owner"], secret.fixed["username"])
        self.assertEqual(initdb["database"], "polaris")

    def test_storage_uses_the_clusters_default_class_unless_named(self):
        self.assertEqual(self.spec["storage"], {"size": "10Gi"})
        spec = render("storage.storageClass=fast")[0]["spec"]
        self.assertEqual(spec["storage"], {"size": "10Gi", "storageClass": "fast"})

    def test_the_instance_count_is_a_value(self):
        self.assertEqual(render("instances=1")[0]["spec"]["instances"], 1)


class ValuesSchema(unittest.TestCase):
    def assertRefused(self, *sets):
        result = helm_template(*sets)
        self.assertNotEqual(result.returncode, 0, f"accepted: {sets}")
        self.assertIn("schema", result.stderr.lower())

    def test_an_unknown_value_is_refused(self):
        self.assertRefused("instance=3")

    def test_an_unknown_nested_value_is_refused(self):
        self.assertRefused("storage.class=fast")

    def test_zero_instances_are_refused(self):
        self.assertRefused("instances=0")

    def test_an_unknown_anti_affinity_type_is_refused(self):
        self.assertRefused("podAntiAffinityType=sometimes")

    def test_an_image_without_a_digest_is_refused(self):
        self.assertRefused("image=ghcr.io/cloudnative-pg/postgresql:16")


class Kubeconform(unittest.TestCase):
    def test_the_rendered_chart_is_a_valid_cluster(self):
        result = kubeconform(helm_template().stdout)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Valid: 1", result.stdout)

    def test_a_field_the_crd_does_not_know_is_refused(self):
        doc = render()[0]
        doc["spec"]["instancesPerNode"] = 1
        result = kubeconform(yaml.safe_dump(doc))
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("instancesPerNode", result.stdout)

    def test_a_kind_without_a_schema_is_refused(self):
        result = kubeconform("apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: x\n")
        self.assertNotEqual(result.returncode, 0, result.stdout)


if __name__ == "__main__":
    unittest.main()
