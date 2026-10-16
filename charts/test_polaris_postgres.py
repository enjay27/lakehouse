"""Tests for charts/polaris-postgres (ADR-008, ADR-010, ADR-016, ADR-043): the rendered
`Cluster`, its `ObjectStore` and `ScheduledBackup`, the values schema, and kubeconform against
CNPG's CRDs from the gate's pinned chart and the plugin's from its pinned manifest.

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
CNPG_DEFAULTS = {"instances": 1, "podAntiAffinityType": "preferred", "smartShutdownTimeout": 180,
                 "failoverQuorum": False,
                 "readiness": {"periodSeconds": 10, "timeoutSeconds": 5, "failureThreshold": 3}}
# What install/release.py passes from .env (ADR-043 §2), as helm lint reads it in static.yml;
# the chart has no default for them.
LINT_VALUES = os.path.join(CHART, "ci", "lint-values.yaml")
PLUGIN = "barman-cloud.cloudnative-pg.io"
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
    plugin = gate.load_pins()["manifests"]["barman_cloud_plugin"]
    kubeconform_schemas.write(yaml.safe_load_all(gate.fetch(plugin["url"], plugin["sha256"])),
                              SCHEMAS.name)


def tearDownModule():
    SCHEMAS.cleanup()


def helm_template(*sets, backup=True):
    argv = ["helm", "template", release.POLARIS_PG_RELEASE, CHART, "--namespace", NAMESPACE]
    if backup:
        argv += ["-f", LINT_VALUES]
    for s in sets:
        argv += ["--set", s]
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def render(*sets):
    result = helm_template(*sets)
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return [d for d in yaml.safe_load_all(result.stdout) if d]


def by_kind(docs):
    return {d["kind"]: d for d in docs}


def kubeconform(manifests_text):
    return subprocess.run(
        ["kubeconform", "-strict", "-summary",
         "-schema-location", kubeconform_schemas.location(SCHEMAS.name), "-"],
        input=manifests_text, capture_output=True, text=True, check=False)


class Rendered(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.docs = render()
        cls.spec = by_kind(cls.docs)["Cluster"]["spec"]

    def test_a_cluster_named_as_the_install_script_waits_for_and_its_backups(self):
        self.assertEqual(sorted((d["apiVersion"], d["kind"]) for d in self.docs),
                         [("barmancloud.cnpg.io/v1", "ObjectStore"),
                          ("postgresql.cnpg.io/v1", "Cluster"),
                          ("postgresql.cnpg.io/v1", "ScheduledBackup")])
        self.assertEqual(by_kind(self.docs)["Cluster"]["metadata"]["name"], release.POLARIS_PG)

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
        spec = by_kind(render("storage.storageClass=fast"))["Cluster"]["spec"]
        self.assertEqual(spec["storage"], {"size": "10Gi", "storageClass": "fast"})

    def test_a_deleted_primary_shuts_down_fast_instead_of_waiting_180_s(self):
        # ADR-047 §8: C0's S1 outage was ~185 s, CNPG's 180 s smart shutdown plus promotion.
        self.assertEqual(self.spec["smartShutdownTimeout"], 0)
        self.assertNotEqual(self.spec["smartShutdownTimeout"],
                            CNPG_DEFAULTS["smartShutdownTimeout"])
        spec = by_kind(render("smartShutdownTimeout=5"))["Cluster"]["spec"]
        self.assertEqual(spec["smartShutdownTimeout"], 5)

    def test_commits_wait_for_any_one_standby_and_block_without_one(self):
        # ADR-056 §1, §2: CNPG's default is asynchronous replication.
        self.assertNotIn("postgresql", CNPG_DEFAULTS)
        self.assertEqual(self.spec["postgresql"]["synchronous"],
                         {"method": "any", "number": 1, "dataDurability": "required",
                          "failoverQuorum": True})

    def test_promotion_waits_for_a_standby_holding_every_confirmed_commit(self):
        # ADR-058 §3: in S5, C1 lost 512 confirmed writes and C1q none (#173).
        sync = self.spec["postgresql"]["synchronous"]
        self.assertIs(sync["failoverQuorum"], True)
        self.assertNotEqual(sync["failoverQuorum"], CNPG_DEFAULTS["failoverQuorum"])

    def test_an_instance_is_unready_within_3_s_of_postgresql_not_answering(self):
        # ADR-047 §8, ADR-057 §3: CNPG's default takes up to 30 s, and a primary back after a
        # node loss waits up to 10 s for its next probe (#178's C0-S4-1).
        readiness = self.spec["probes"]["readiness"]
        self.assertEqual(readiness, {"periodSeconds": 1, "timeoutSeconds": 1,
                                     "failureThreshold": 3})
        self.assertLessEqual(readiness["periodSeconds"] * readiness["failureThreshold"], 3)
        self.assertNotEqual(readiness, CNPG_DEFAULTS["readiness"])
        self.assertEqual(set(self.spec["probes"]), {"readiness"})  # liveness stays CNPG's

    def test_the_readiness_probe_is_a_value(self):
        spec = by_kind(render("probes.readiness.timeoutSeconds=3",
                              "probes.readiness.failureThreshold=5"))["Cluster"]["spec"]
        self.assertEqual(spec["probes"]["readiness"], {"periodSeconds": 1, "timeoutSeconds": 3,
                                                       "failureThreshold": 5})

    def test_fewer_than_three_instances_stay_asynchronous(self):
        # ADR-056 §3: one standby restarting would stop every write.
        for n in (1, 2):
            spec = by_kind(render(f"instances={n}"))["Cluster"]["spec"]
            self.assertNotIn("postgresql", spec, f"instances={n}")

    def test_the_synchronous_values_are_values(self):
        spec = by_kind(render("synchronous.number=2", "synchronous.dataDurability=preferred",
                              "synchronous.failoverQuorum=false"))["Cluster"]["spec"]
        self.assertEqual(spec["postgresql"]["synchronous"],
                         {"method": "any", "number": 2, "dataDurability": "preferred",
                          "failoverQuorum": False})

    def test_the_instance_count_is_a_value(self):
        self.assertEqual(by_kind(render("instances=1"))["Cluster"]["spec"]["instances"], 1)


class Backups(unittest.TestCase):
    """ADR-043 §1 and §2, ADR-060."""

    @classmethod
    def setUpClass(cls):
        cls.kinds = by_kind(render())

    def test_the_cluster_archives_its_wal_through_the_plugin_to_the_object_store(self):
        self.assertEqual(self.kinds["Cluster"]["spec"]["plugins"], [
            {"name": PLUGIN, "isWALArchiver": True,
             "parameters": {"barmanObjectName": release.POLARIS_BACKUP_STORE}}])

    def test_the_object_store_is_the_backup_bucket_on_the_endpoint_from_env(self):
        store = self.kinds["ObjectStore"]
        self.assertEqual(store["metadata"]["name"], release.POLARIS_BACKUP_STORE)
        conf = store["spec"]["configuration"]
        self.assertEqual(conf["destinationPath"], "s3://polaris-backup/")
        self.assertEqual(conf["endpointURL"], "http://s3.example.invalid:8333")

    def test_the_object_store_reads_the_backup_key_from_the_secret_init_env_creates(self):
        secret = next(s for s in init_env.SECRETS if s.name == release.POLARIS_BACKUP_SECRET)
        creds = self.kinds["ObjectStore"]["spec"]["configuration"]["s3Credentials"]
        self.assertEqual(creds, {
            "accessKeyId": {"name": secret.name, "key": "accessKeyId"},
            "secretAccessKey": {"name": secret.name, "key": "secretAccessKey"},
            "region": {"name": secret.name, "key": "region"}})
        self.assertEqual(sorted(secret.from_env), ["accessKeyId", "region", "secretAccessKey"])

    def test_the_backups_sign_for_the_region_from_env(self):
        """ADR-060: the plugin reads the region only from a Secret key; init_env fills it."""
        secret = next(s for s in init_env.SECRETS if s.name == release.POLARIS_BACKUP_SECRET)
        self.assertEqual(secret.from_env["region"], "S3_REGION")

    def test_retention_is_a_value_30_days_by_default(self):
        self.assertEqual(self.kinds["ObjectStore"]["spec"]["retentionPolicy"], "30d")
        store = by_kind(render("backup.retention=8w"))["ObjectStore"]
        self.assertEqual(store["spec"]["retentionPolicy"], "8w")

    def test_a_daily_base_backup_through_the_plugin_at_two_by_default(self):
        scheduled = self.kinds["ScheduledBackup"]
        self.assertEqual(scheduled["metadata"]["name"], "polaris-pg-daily")
        self.assertEqual(scheduled["spec"], {
            "schedule": "0 0 2 * * *", "cluster": {"name": release.POLARIS_PG},
            "method": "plugin", "pluginConfiguration": {"name": PLUGIN},
            "backupOwnerReference": "self"})
        scheduled = by_kind(render("backup.schedule=0 30 3 * * *"))["ScheduledBackup"]
        self.assertEqual(scheduled["spec"]["schedule"], "0 30 3 * * *")


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

    def test_a_negative_or_textual_shutdown_timeout_is_refused(self):
        self.assertRefused("smartShutdownTimeout=-1")
        self.assertRefused("smartShutdownTimeout=soon")

    def test_a_synchronous_count_below_one_is_refused(self):
        self.assertRefused("synchronous.number=0")

    def test_an_unknown_data_durability_is_refused(self):
        self.assertRefused("synchronous.dataDurability=sometimes")

    def test_a_readiness_probe_below_one_or_unknown_is_refused(self):
        for field in ("periodSeconds", "timeoutSeconds", "failureThreshold"):
            self.assertRefused(f"probes.readiness.{field}=0")
        self.assertRefused("probes.readiness.successThreshold=2")
        self.assertRefused("probes.liveness.periodSeconds=1")

    def test_a_textual_failover_quorum_is_refused(self):
        self.assertRefused("synchronous.failoverQuorum=sometimes")

    def test_an_image_without_a_digest_is_refused(self):
        self.assertRefused("image=ghcr.io/cloudnative-pg/postgresql:16")

    def test_the_lint_values_are_only_what_the_install_script_passes(self):
        with open(LINT_VALUES, encoding="utf-8") as f:
            lint = yaml.safe_load(f)
        env = {"S3_ENDPOINT": "http://s3.example.invalid:8333",
               "POLARIS_BACKUP_BUCKET": "polaris-backup"}
        self.assertEqual(lint, release.postgres_values(env))

    def test_without_the_endpoint_and_bucket_from_env_the_chart_is_refused(self):
        result = helm_template(backup=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("schema", result.stderr.lower())

    def test_an_endpoint_that_is_not_a_url_is_refused(self):
        self.assertRefused("backup.endpointURL=s3.example.invalid")

    def test_a_bucket_name_s3_would_refuse_is_refused(self):
        self.assertRefused("backup.bucket=Polaris_Backup")

    def test_a_retention_the_plugin_would_refuse_is_refused(self):
        self.assertRefused("backup.retention=30 days")

    def test_an_unknown_backup_value_is_refused(self):
        self.assertRefused("backup.retension=30d")


class Kubeconform(unittest.TestCase):
    def test_the_rendered_chart_is_valid_against_cnpgs_and_the_plugins_crds(self):
        result = kubeconform(helm_template().stdout)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Valid: 3", result.stdout)

    def test_a_field_the_object_store_crd_does_not_know_is_refused(self):
        doc = by_kind(render())["ObjectStore"]
        doc["spec"]["configuration"]["bucket"] = "polaris-backup"
        result = kubeconform(yaml.safe_dump(doc))
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("bucket", result.stdout)

    def test_a_field_the_crd_does_not_know_is_refused(self):
        doc = by_kind(render())["Cluster"]
        doc["spec"]["instancesPerNode"] = 1
        result = kubeconform(yaml.safe_dump(doc))
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("instancesPerNode", result.stdout)

    def test_a_kind_without_a_schema_is_refused(self):
        result = kubeconform("apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: x\n")
        self.assertNotEqual(result.returncode, 0, result.stdout)


if __name__ == "__main__":
    unittest.main()
