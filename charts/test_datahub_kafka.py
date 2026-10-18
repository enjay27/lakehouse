"""Tests for charts/datahub-kafka (ADR-068, ADR-069, ADR-025, ADR-016): the rendered
`StatefulSet` and headless `Service` datahub-kafka, the broker's settings passed as the image's
`KAFKA_*` variables, the values schema, and kubeconform against the StatefulSet and Service
schemas of the gate's Kubernetes version, pinned in gates/pins.json.

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
import gate  # noqa: E402

CHART = os.path.join(HERE, "datahub-kafka")
NAME = "datahub-kafka"
NAMESPACE = "catalog-datahub"
IMAGE_RE = re.compile(r"^apache/kafka:4\.3\.1@sha256:[0-9a-f]{64}$")
# Datahub v1.7.0 sends messages up to 5 MiB (upstream chart, global.kafka.maxMessageBytes).
DATAHUB_MAX_MESSAGE = "5242880"
# The image's user, `appuser`, made by Alpine's adduser as the first user.
APPUSER = 1000
DATA = "/var/lib/kafka/data"

SCHEMAS = None


def setUpModule():
    global SCHEMAS
    missing = [t for t in ("helm", "kubeconform") if not shutil.which(t)]
    if missing:
        raise RuntimeError(f"{', '.join(missing)} not on PATH: run "
                           "`python3 gates/gate.py tools BIN_DIR helm kubeconform` first")
    SCHEMAS = tempfile.TemporaryDirectory()
    pins = gate.load_pins()["kubernetes_schemas"]
    for name in ("statefulset", "service"):
        # Each file under its upstream name, statefulset-apps-v1.json and service-v1.json.
        path = os.path.join(SCHEMAS.name, pins[name]["url"].rsplit("/", 1)[1])
        with open(path, "wb") as f:
            f.write(gate.fetch(pins[name]["url"], pins[name]["sha256"]))


def tearDownModule():
    SCHEMAS.cleanup()


def helm_template(*sets):
    argv = ["helm", "template", NAME, CHART, "--namespace", NAMESPACE]
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


def container(docs):
    (c,) = by_kind(docs)["StatefulSet"]["spec"]["template"]["spec"]["containers"]
    return c


def env_of(docs):
    return {e["name"]: e for e in container(docs)["env"]}


def values_of(docs):
    return {name: e.get("value") for name, e in env_of(docs).items()}


def kubeconform(manifests_text):
    # kubeconform's own naming, which also covers core kinds: -apps-v1 or -v1.
    location = os.path.join(SCHEMAS.name, "{{ .ResourceKind }}{{ .KindSuffix }}.json")
    return subprocess.run(["kubeconform", "-strict", "-summary", "-schema-location", location,
                           "-"], input=manifests_text, capture_output=True, text=True,
                          check=False)


class Rendered(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.docs = render()
        cls.kinds = by_kind(cls.docs)
        cls.sts = cls.kinds["StatefulSet"]
        cls.pod = cls.sts["spec"]["template"]["spec"]
        cls.env = values_of(cls.docs)

    def test_a_statefulset_and_a_headless_service_named_as_the_install_script_waits_for(self):
        self.assertEqual(sorted(self.kinds), ["Service", "StatefulSet"])
        self.assertEqual(len(self.docs), 2)
        self.assertEqual(self.sts["metadata"]["name"], NAME)
        self.assertEqual(self.kinds["Service"]["metadata"]["name"], NAME)
        self.assertEqual(self.sts["spec"]["serviceName"], NAME)

    def test_one_broker(self):
        # ADR-025, ADR-068 §1: one replica, and no value to change it.
        self.assertEqual(self.sts["spec"]["replicas"], 1)

    def test_the_service_is_headless_on_9092_and_publishes_a_starting_broker(self):
        spec = self.kinds["Service"]["spec"]
        self.assertEqual(spec["clusterIP"], "None")
        self.assertEqual([(p["name"], p["port"], p["targetPort"]) for p in spec["ports"]],
                         [("kafka", 9092, "kafka")])
        # The broker's own name resolves before it is ready.
        self.assertTrue(spec["publishNotReadyAddresses"])
        self.assertEqual(spec["selector"], self.sts["spec"]["selector"]["matchLabels"])

    def test_the_pod_carries_the_label_the_network_policy_selects(self):
        labels = self.sts["spec"]["template"]["metadata"]["labels"]
        self.assertEqual(labels["app.kubernetes.io/name"], NAME)
        for key, value in self.sts["spec"]["selector"]["matchLabels"].items():
            self.assertEqual(labels[key], value)

    def test_apache_kafka_4_3_1_pinned_by_tag_and_digest(self):
        self.assertRegex(container(self.docs)["image"], IMAGE_RE)

    def test_one_process_is_broker_and_controller_named_as_a_static_voter(self):
        # ADR-068 §1 as amended by ADR-069: the image formats only with static voters.
        self.assertEqual(self.env["KAFKA_PROCESS_ROLES"], "broker,controller")
        self.assertEqual(self.env["KAFKA_NODE_ID"], "1")
        self.assertEqual(self.env["KAFKA_CONTROLLER_QUORUM_VOTERS"], "1@localhost:9093")
        self.assertNotIn("KAFKA_CONTROLLER_QUORUM_BOOTSTRAP_SERVERS", self.env)
        self.assertEqual(self.env["KAFKA_CONTROLLER_LISTENER_NAMES"], "CONTROLLER")

    def test_the_controller_listens_on_localhost_only_and_clients_on_9092(self):
        listeners = self.env["KAFKA_LISTENERS"].split(",")
        self.assertEqual(sorted(listeners), ["CONTROLLER://localhost:9093", "PLAINTEXT://:9092"])
        self.assertEqual(self.env["KAFKA_LISTENER_SECURITY_PROTOCOL_MAP"],
                         "CONTROLLER:PLAINTEXT,PLAINTEXT:PLAINTEXT")
        self.assertEqual(self.env["KAFKA_INTER_BROKER_LISTENER_NAME"], "PLAINTEXT")
        ports = container(self.docs)["ports"]
        self.assertEqual([(p["name"], p["containerPort"]) for p in ports], [("kafka", 9092)])

    def test_clients_are_sent_to_the_pods_stable_name_in_the_headless_service(self):
        env = env_of(self.docs)
        self.assertEqual(env["POD_NAME"]["valueFrom"]["fieldRef"]["fieldPath"], "metadata.name")
        self.assertEqual(env["POD_NAMESPACE"]["valueFrom"]["fieldRef"]["fieldPath"],
                         "metadata.namespace")
        self.assertEqual(self.env["KAFKA_ADVERTISED_LISTENERS"],
                         f"PLAINTEXT://$(POD_NAME).{NAME}.$(POD_NAMESPACE).svc.cluster.local:9092")
        # Kubernetes expands $(VAR) only from variables defined before it.
        names = [e["name"] for e in container(self.docs)["env"]]
        self.assertLess(names.index("POD_NAME"), names.index("KAFKA_ADVERTISED_LISTENERS"))
        self.assertLess(names.index("POD_NAMESPACE"), names.index("KAFKA_ADVERTISED_LISTENERS"))

    def test_messages_up_to_datahubs_5_mib_are_accepted(self):
        # ADR-068 §2: the broker's default is about 1 MiB.
        self.assertEqual(self.env["KAFKA_MESSAGE_MAX_BYTES"], DATAHUB_MAX_MESSAGE)
        self.assertEqual(self.env["KAFKA_REPLICA_FETCH_MAX_BYTES"], DATAHUB_MAX_MESSAGE)

    def test_every_replication_setting_fits_one_broker(self):
        for var in ("KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR",
                    "KAFKA_TRANSACTION_STATE_LOG_REPLICATION_FACTOR",
                    "KAFKA_DEFAULT_REPLICATION_FACTOR",
                    "KAFKA_TRANSACTION_STATE_LOG_MIN_ISR", "KAFKA_MIN_INSYNC_REPLICAS"):
            with self.subTest(var=var):
                self.assertEqual(self.env[var], "1")

    def test_topics_are_never_created_on_first_use(self):
        # ADR-068 §2: Datahub's system-update creates its topics.
        self.assertEqual(self.env["KAFKA_AUTO_CREATE_TOPICS_ENABLE"], "false")

    def test_retention_and_heap_come_from_the_values(self):
        self.assertEqual(self.env["KAFKA_LOG_RETENTION_HOURS"], "168")
        self.assertEqual(self.env["KAFKA_HEAP_OPTS"], "-Xms1g -Xmx1g")
        env = values_of(render("retentionHours=24", "heap=512m"))
        self.assertEqual(env["KAFKA_LOG_RETENTION_HOURS"], "24")
        self.assertEqual(env["KAFKA_HEAP_OPTS"], "-Xms512m -Xmx512m")

    def test_the_cluster_id_is_the_values_fixed_one(self):
        self.assertRegex(self.env["CLUSTER_ID"], r"^[A-Za-z0-9_-]{22}$")
        # The image's own default, used when none is set, is never ours.
        self.assertNotEqual(self.env["CLUSTER_ID"], "5L6g3nShT-eMCtK--X86sw")

    def test_every_variable_is_set_as_a_string(self):
        for e in container(self.docs)["env"]:
            with self.subTest(name=e["name"]):
                self.assertTrue("valueFrom" in e or isinstance(e["value"], str))

    def test_no_service_variables_reach_the_image_as_broker_settings(self):
        # The image turns every KAFKA_* variable into a setting; a Service named kafka in the
        # namespace would inject KAFKA_PORT.
        self.assertFalse(self.pod["enableServiceLinks"])

    def test_the_log_directory_is_a_folder_on_the_volume(self):
        # A folder below the mount, so the volume's lost+found is never read as a partition.
        (mount,) = container(self.docs)["volumeMounts"]
        self.assertEqual((mount["name"], mount["mountPath"]), ("data", DATA))
        self.assertEqual(self.env["KAFKA_LOG_DIRS"], f"{DATA}/logs")

    def test_the_volume_uses_the_clusters_default_class_unless_named(self):
        (claim,) = self.sts["spec"]["volumeClaimTemplates"]
        self.assertEqual(claim["metadata"]["name"], "data")
        self.assertEqual(claim["spec"]["accessModes"], ["ReadWriteOnce"])
        self.assertEqual(claim["spec"]["resources"]["requests"]["storage"], "10Gi")
        self.assertNotIn("storageClassName", claim["spec"])
        (claim,) = by_kind(render("storage.storageClass=fast"))["StatefulSet"]["spec"][
            "volumeClaimTemplates"]
        self.assertEqual(claim["spec"]["storageClassName"], "fast")

    def test_the_pod_runs_as_the_images_non_root_user_and_can_write_the_volume(self):
        sc = self.pod["securityContext"]
        self.assertEqual((sc["runAsUser"], sc["runAsGroup"], sc["fsGroup"]),
                         (APPUSER, APPUSER, APPUSER))
        self.assertTrue(sc["runAsNonRoot"])
        csc = container(self.docs)["securityContext"]
        self.assertFalse(csc["allowPrivilegeEscalation"])
        self.assertEqual(csc["capabilities"]["drop"], ["ALL"])

    def test_ready_when_9092_answers_and_no_liveness_probe(self):
        # ADR-068 §1: a busy broker must not become a restart (#179).
        c = container(self.docs)
        self.assertEqual(c["readinessProbe"]["tcpSocket"]["port"], "kafka")
        self.assertNotIn("livenessProbe", c)

    def test_requests_and_limits_come_from_the_values(self):
        resources = container(self.docs)["resources"]
        self.assertEqual(sorted(resources), ["limits", "requests"])
        self.assertIn("memory", resources["limits"])
        resources = container(render("resources.limits.memory=3Gi"))["resources"]
        self.assertEqual(resources["limits"]["memory"], "3Gi")

    def test_kubeconform_accepts_the_rendered_chart(self):
        result = kubeconform(helm_template().stdout)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Valid: 2", result.stdout)


class ValuesSchema(unittest.TestCase):
    def assertRefused(self, *sets):
        result = helm_template(*sets)
        self.assertNotEqual(result.returncode, 0, f"accepted: {sets}")
        self.assertIn("schema", result.stderr.lower())

    def test_an_unknown_value_is_refused(self):
        self.assertRefused("retention=24")

    def test_an_unknown_nested_value_is_refused(self):
        self.assertRefused("storage.class=fast")

    def test_the_replica_count_is_not_a_value(self):
        # ADR-025: one broker; more would be a decision of its own.
        self.assertRefused("replicas=3")

    def test_an_image_without_a_digest_is_refused(self):
        self.assertRefused("image=apache/kafka:4.3.1")

    def test_a_cluster_id_kafka_would_refuse_is_refused(self):
        self.assertRefused("clusterId=not-a-cluster-id")
        self.assertRefused("clusterId=")

    def test_a_heap_the_jvm_would_refuse_is_refused(self):
        self.assertRefused("heap=1 GiB")
        self.assertRefused("heap=0g")

    def test_a_retention_below_one_hour_is_refused(self):
        self.assertRefused("retentionHours=0")
        self.assertRefused("retentionHours=a week")

    def test_an_empty_volume_size_is_refused(self):
        self.assertRefused("storage.size=")


if __name__ == "__main__":
    unittest.main()
