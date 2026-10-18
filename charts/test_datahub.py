"""Tests for charts/datahub (ADR-071, ADR-072, ADR-025, ADR-016): upstream's datahub chart 1.1.7,
its release archive with the one line ADR-072 §1 patches, proven against the archive itself,
fetched by the URL and sha256 pinned in gates/pins.json; every key of values-lakehouse.yaml found
in upstream's values or read by its templates, since its values schema refuses no unknown key
(ADR-072 §3); every value ADR-071 §1 to §6, ADR-072 §1 and §2 and ADR-076 §1 and §2 set checked
on the rendered output; and kubeconform against the schemas of the gate's Kubernetes version, pinned in
gates/pins.json.

Run: python3 -m unittest discover -s charts
Needs helm and kubeconform on PATH (`python3 gates/gate.py tools BIN_DIR helm kubeconform`) and
PyYAML (`pip install --require-hashes -r charts/requirements.txt`). Missing tools fail the run.
"""

import difflib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "gates"))
import gate  # noqa: E402

CHART = os.path.join(HERE, "datahub")
VALUES = os.path.join(CHART, "values-lakehouse.yaml")
RELEASE = "datahub"
NAMESPACE = "catalog-datahub"
# Our files beside upstream's.
OURS = {"values-lakehouse.yaml"}
# ADR-072 §1: the label datahub.acryl.io/datahub-version takes the tag up to its first `@`, in
# both copies of datahub-gms's helpers, line 39.
HELPERS = ("charts/datahub-gms/templates/_helpers.tpl",
           "subcharts/datahub-gms/templates/_helpers.tpl")
PATCHED_LINE_NUMBER = 39
UPSTREAM_LINE = "{{- .Values.image.tag | default .Values.global.datahub.version -}}"
PATCHED_LINE = ('{{- .Values.image.tag | default .Values.global.datahub.version '
                '| splitList "@" | first -}}')
# The subcharts helm renders, each reading its values under its own name.
SUBCHARTS = ("datahub-gms", "datahub-frontend", "datahub-mae-consumer", "datahub-mce-consumer",
             "datahub-ingestion-cron", "acryl-datahub-actions")
# Values whose contents are a map of labels or a Kubernetes object the chart passes through as it
# is: their keys are not upstream's, and kubeconform's strict schemas check the objects.
# Upstream's ingestion CronJob reads each entry of `crons` through `range`, never as
# `.Values.…`: CronEntry checks those keys against its templates instead.
PASS_THROUGH = {"global.podLabels", "datahub-ingestion-cron.crons"}
VALUES_RE = re.compile(r"\.Values((?:\.[A-Za-z_][A-Za-z0-9_]*)+)")
# A map passed as `"image" .Values.…` to upstream's `datahub.image` helper, which reads these keys
# of it (templates/_helpers.tpl).
IMAGE_ARG_RE = re.compile(r'"image" \.Values((?:\.[A-Za-z_][A-Za-z0-9_]*)+)')
IMAGE_KEYS = ("registry", "repository", "tag")
VERSION = "v1.7.0.1"
IMAGES = {"gms": "docker.io/acryldata/datahub-gms",
          "ingestion": "docker.io/acryldata/datahub-ingestion",
          "frontend": "docker.io/acryldata/datahub-frontend-react",
          "upgrade": "docker.io/acryldata/datahub-upgrade"}
DIGEST_RE = re.compile(r"@sha256:[0-9a-f]{64}$")
LABEL_VALUE_RE = re.compile(r"^(([A-Za-z0-9][-A-Za-z0-9_.]*)?[A-Za-z0-9])?$")
# ADR-071 §4, §5: every Datahub pod is a client of its three stores and of GMS.
CLIENT_LABELS = {"lakehouse/datahub-pg-client": "true", "lakehouse/datahub-kafka-client": "true",
                 "lakehouse/datahub-opensearch-client": "true",
                 "lakehouse/datahub-gms-client": "true"}
GMS = f"{RELEASE}-datahub-gms"
FRONTEND = f"{RELEASE}-datahub-frontend"
SYSTEM_UPDATE = f"{RELEASE}-system-update"
SYSTEM_UPDATE_NONBLOCKING = f"{RELEASE}-system-update-nonblk"
RESTORE_INDICES = f"{RELEASE}-datahub-restore-indices-job-template"
# ADR-076 §1, §2: one CronJob, polaris, its recipe in a ConfigMap of the chart's.
INGESTION = f"{RELEASE}-datahub-ingestion-cron-polaris"
RECIPE_CONFIGMAP = "datahub-ingestion-polaris-recipe"
RECIPE_FILE = "polaris.yml"
CRON_TEMPLATES = ("charts/datahub-ingestion-cron/templates/cron.yaml",
                  "charts/datahub-ingestion-cron/templates/configmap.yaml")
RECIPE = {
    "source": {"type": "iceberg", "config": {
        "catalog": {"polaris": {
            "type": "rest",
            "uri": "${POLARIS_URI}",
            "oauth2-server-uri": "${POLARIS_URI}/v1/oauth/tokens",
            "credential": "${POLARIS_CLIENT_ID}:${POLARIS_CLIENT_SECRET}",
            "scope": "PRINCIPAL_ROLE:ALL",
            "warehouse": "${POLARIS_CATALOG}",
            "header.Polaris-Realm": "POLARIS",
            "header.X-Iceberg-Access-Delegation": ""}},
        "platform_instance": "${POLARIS_CATALOG}",
        "env": "PROD",
        "profiling": {"enabled": False},
        "stateful_ingestion": {"enabled": True, "remove_stale_metadata": True}}},
    "pipeline_name": "polaris-${POLARIS_CATALOG}",
    "sink": {"type": "datahub-rest", "config": {"server": f"http://{RELEASE}-datahub-gms:8080"}},
}
OPERATOR = "datahub-operator"
# The images' user in upstream's security contexts.
DATAHUB_USER = 1000
KINDS = ("deployment", "service", "serviceaccount", "role", "rolebinding", "job", "cronjob",
         "configmap")

ARCHIVE = None
SCHEMAS = None


def setUpModule():
    global ARCHIVE, SCHEMAS
    missing = [t for t in ("helm", "kubeconform") if not shutil.which(t)]
    if missing:
        raise RuntimeError(f"{', '.join(missing)} not on PATH: run "
                           "`python3 gates/gate.py tools BIN_DIR helm kubeconform` first")
    pins = gate.load_pins()
    SCHEMAS = tempfile.TemporaryDirectory()
    for name in KINDS:
        pin = pins["kubernetes_schemas"][name]
        # Each file under its upstream name, as kubeconform looks it up.
        path = os.path.join(SCHEMAS.name, pin["url"].rsplit("/", 1)[1])
        with open(path, "wb") as f:
            f.write(gate.fetch(pin["url"], pin["sha256"]))
    pin = pins["datahub_chart"]
    data = gate.fetch(pin["url"], pin["sha256"])
    ARCHIVE = {}
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        for member in archive.getmembers():
            if member.isfile():
                top, _, path = member.name.partition("/")
                if top != "datahub":
                    raise AssertionError(f"{member.name}: not under datahub/")
                ARCHIVE[path] = archive.extractfile(member).read()


def tearDownModule():
    SCHEMAS.cleanup()


def chart_files():
    """{path relative to charts/datahub: bytes} for every file but ours."""
    files = {}
    for directory, _, names in os.walk(CHART):
        for name in names:
            path = os.path.relpath(os.path.join(directory, name), CHART).replace(os.sep, "/")
            if path not in OURS:
                with open(os.path.join(directory, name), "rb") as f:
                    files[path] = f.read()
    return files


def changed_lines(old, new):
    """[(line number in old, old line, new line)] for each line replaced one for one; any other
    change (lines added or removed) is returned as (None, old lines, new lines)."""
    a, b = old.decode("utf-8").split("\n"), new.decode("utf-8").split("\n")
    changes = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b).get_opcodes():
        if tag == "equal":
            continue
        if tag == "replace" and i2 - i1 == j2 - j1:
            changes += [(i1 + k + 1, a[i1 + k], b[j1 + k]) for k in range(i2 - i1)]
        else:
            changes.append((None, a[i1:i2], b[j1:j2]))
    return changes


def load(path):
    with open(os.path.join(CHART, path), encoding="utf-8") as f:
        return yaml.safe_load(f)


def merged(base, over):
    """`over` deep-merged onto `base`, as helm merges a parent's values onto a subchart's."""
    out = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = merged(out[key], value)
        else:
            out[key] = value
    return out


def upstream_values():
    """Upstream's values: the parent's, with each subchart's own under its name."""
    values = load("values.yaml")
    for sub in SUBCHARTS:
        values[sub] = merged(load(f"charts/{sub}/values.yaml") or {}, values.get(sub) or {})
    return values


def template_paths():
    """Every dotted path upstream's rendered templates read as `.Values.…`, and each of its
    prefixes: a subchart's under its name, `global` as it is; for a map passed to the
    `datahub.image` helper, the keys the helper reads of it."""
    paths = set()
    for directory, _, names in os.walk(CHART):
        parts = os.path.relpath(directory, CHART).replace(os.sep, "/").split("/")
        if "templates" not in parts or parts[0] == "subcharts":
            continue
        prefix = [parts[1]] if parts[0] == "charts" else []
        for name in names:
            with open(os.path.join(directory, name), encoding="utf-8") as f:
                text = f.read()
            read = [m.group(1).lstrip(".").split(".") for m in VALUES_RE.finditer(text)]
            read += [m.group(1).lstrip(".").split(".") + [key]
                     for m in IMAGE_ARG_RE.finditer(text) for key in IMAGE_KEYS]
            for path in read:
                full = path if path[0] == "global" else prefix + path
                paths.update(".".join(full[:n]) for n in range(1, len(full) + 1))
    return paths


def unknown_keys(ours, values, paths, prefix=""):
    """The dotted keys of `ours` neither in upstream's `values` nor among the `paths` its
    templates read; PASS_THROUGH's contents and lists are not walked."""
    found = []
    for key, value in ours.items():
        path = prefix + key
        in_values = isinstance(values, dict) and key in values
        if not in_values and path not in paths:
            found.append(path)
        elif isinstance(value, dict) and path not in PASS_THROUGH:
            found += unknown_keys(value, values[key] if in_values else None, paths, path + ".")
    return found


def helm_template(*sets):
    argv = ["helm", "template", RELEASE, CHART, "--namespace", NAMESPACE, "-f", VALUES]
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


def pod_template(doc):
    if doc["kind"] not in ("Deployment", "Job", "CronJob"):
        return None
    spec = doc["spec"]
    if doc["kind"] == "CronJob":
        spec = spec["jobTemplate"]["spec"]
    return spec["template"]


def containers(pod):
    return pod["spec"]["containers"] + (pod["spec"].get("initContainers") or [])


def env(container):
    """{name: value, or valueFrom} of the container's variables; a name set twice fails."""
    names = [e["name"] for e in container.get("env", [])]
    twice = sorted({n for n in names if names.count(n) > 1})
    if twice:
        raise AssertionError(f"{container['name']}: set more than once: {twice}")
    return {e["name"]: e.get("value", e.get("valueFrom")) for e in container.get("env", [])}


def secret(name, key):
    return {"secretKeyRef": {"name": name, "key": key}}


def label_maps(doc):
    """The document's own labels, and its pod template's."""
    found = [doc["metadata"].get("labels") or {}]
    pod = pod_template(doc)
    if pod:
        found.append(pod["metadata"].get("labels") or {})
    return found


class Upstream(unittest.TestCase):
    def test_the_chart_is_the_pinned_archives_1_1_7_with_datahub_v1_7_0(self):
        chart = load("Chart.yaml")
        self.assertEqual((chart["name"], chart["version"], chart["appVersion"]),
                         ("datahub", gate.load_pins()["datahub_chart"]["version"], "v1.7.0"))

    def test_the_chart_has_every_file_of_the_archive_and_no_other(self):
        ours = chart_files()
        self.assertEqual(sorted(set(ARCHIVE) - set(ours)), [], "missing from charts/datahub")
        self.assertEqual(sorted(set(ours) - set(ARCHIVE)), [], "not in the archive")

    def test_every_file_but_the_helpers_is_the_archives_byte_for_byte(self):
        ours = chart_files()
        changed = sorted(p for p in ARCHIVE if p in ours and p not in HELPERS
                         and ours[p] != ARCHIVE[p])
        self.assertEqual(changed, [])

    def test_each_helper_differs_from_the_archives_in_the_patched_line_alone(self):
        ours = chart_files()
        for path in HELPERS:
            with self.subTest(file=path):
                self.assertEqual(changed_lines(ARCHIVE[path], ours[path]),
                                 [(PATCHED_LINE_NUMBER, UPSTREAM_LINE, PATCHED_LINE)])

    def test_changed_lines_finds_a_replaced_line_and_an_added_one(self):
        self.assertEqual(changed_lines(b"a\nb\nc\n", b"a\nB\nc\n"), [(2, "b", "B")])
        self.assertEqual(changed_lines(b"a\nc\n", b"a\nb\nc\n"), [(None, [], ["b"])])
        self.assertEqual(changed_lines(b"a\n", b"a\n"), [])

    def test_upstreams_values_schema_refuses_no_unknown_key(self):
        # Why the key check below exists (ADR-071 §1).
        with open(os.path.join(CHART, "values.schema.json"), encoding="utf-8") as f:
            self.assertNotIn("additionalProperties", json.load(f))

    def test_every_key_of_our_values_is_upstreams_or_read_by_its_templates(self):
        # ADR-072 §3.
        self.assertEqual(unknown_keys(load("values-lakehouse.yaml"), upstream_values(),
                                      template_paths()), [])

    def test_a_misspelt_key_is_found(self):
        values, paths = upstream_values(), template_paths()
        self.assertEqual(unknown_keys({"datahub-gms": {"extraEnv": []}}, values, paths),
                         ["datahub-gms.extraEnv"])
        self.assertEqual(unknown_keys({"global": {"podLabel": {}}}, values, paths),
                         ["global.podLabel"])
        self.assertEqual(unknown_keys({"datahubSystemUpdate": {"extraEnvs": []}}, values, paths),
                         [])
        self.assertEqual(unknown_keys({"datahubUpgrade": {"image": {"tag": "x"}}}, values, paths),
                         [])
        self.assertEqual(unknown_keys({"datahubUpgrade": {"image": {"tga": "x"}}}, values, paths),
                         ["datahubUpgrade.image.tga"])
        self.assertEqual(unknown_keys({"global": {"podLabels": {"a/b": "c"}}}, values, paths),
                         [])


class Rendered(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.docs = render()
        cls.named = {(d["kind"], d["metadata"]["name"]): d for d in cls.docs}
        cls.pods = {d["metadata"]["name"]: pod_template(d) for d in cls.docs if pod_template(d)}
        cls.gms = cls.pods[GMS]["spec"]["containers"][0]
        cls.frontend = cls.pods[FRONTEND]["spec"]["containers"][0]
        cls.jobs = [cls.pods[n]["spec"]["containers"][0]
                    for n in (SYSTEM_UPDATE, SYSTEM_UPDATE_NONBLOCKING)]

    def hook(self, kind, name):
        annotations = self.named[(kind, name)]["metadata"].get("annotations") or {}
        return annotations.get("helm.sh/hook")

    def test_the_kinds_and_names(self):
        # ADR-071 §2: GMS, the frontend, system-update with its ServiceAccount and Role, and
        # restore-indices; no Secret, no actions, no standalone consumers. ADR-076 §1: the
        # ingestion CronJob polaris and its recipe.
        self.assertEqual(sorted(self.named), [
            ("ConfigMap", RECIPE_CONFIGMAP), ("CronJob", INGESTION), ("CronJob", RESTORE_INDICES),
            ("Deployment", FRONTEND), ("Deployment", GMS),
            ("Job", SYSTEM_UPDATE), ("Job", SYSTEM_UPDATE_NONBLOCKING),
            ("Role", f"{OPERATOR}-role"), ("RoleBinding", f"{OPERATOR}-role-binding"),
            ("Service", FRONTEND), ("Service", GMS), ("ServiceAccount", "datahub-app-sa"),
            ("ServiceAccount", f"{OPERATOR}-sa")])

    def test_system_update_blocks_before_install_and_runs_again_after(self):
        self.assertEqual(self.hook("Job", SYSTEM_UPDATE), "pre-install,pre-upgrade")
        self.assertEqual(self.hook("Job", SYSTEM_UPDATE_NONBLOCKING), "post-install,post-upgrade")
        for kind, name in (("ServiceAccount", f"{OPERATOR}-sa"), ("Role", f"{OPERATOR}-role"),
                           ("RoleBinding", f"{OPERATOR}-role-binding")):
            with self.subTest(kind=kind):
                self.assertEqual(self.hook(kind, name), "pre-install,pre-upgrade")
        for name in (SYSTEM_UPDATE, SYSTEM_UPDATE_NONBLOCKING):
            with self.subTest(job=name):
                self.assertEqual(self.pods[name]["spec"]["serviceAccountName"], f"{OPERATOR}-sa")

    def test_one_gms_with_its_consumers_and_one_frontend(self):
        for name in (GMS, FRONTEND):
            with self.subTest(deployment=name):
                self.assertEqual(self.named[("Deployment", name)]["spec"]["replicas"], 1)
        variables = env(self.gms)
        for name in ("MCE_CONSUMER_ENABLED", "MAE_CONSUMER_ENABLED", "PE_CONSUMER_ENABLED"):
            self.assertEqual(variables[name], "true", name)

    def test_managed_ingestion_is_off(self):
        self.assertEqual(env(self.gms)["UI_INGESTION_ENABLED"], "false")

    def test_restore_indices_is_a_suspended_template(self):
        self.assertTrue(self.named[("CronJob", RESTORE_INDICES)]["spec"]["suspend"])

    def test_the_role_stays_in_the_namespace(self):
        self.assertFalse({"ClusterRole", "ClusterRoleBinding"} & {d["kind"] for d in self.docs})
        (subject,) = self.named[("RoleBinding", f"{OPERATOR}-role-binding")]["subjects"]
        self.assertEqual(subject["namespace"], NAMESPACE)

    def test_the_services_are_cluster_ip_on_8080_and_9002(self):
        for name, port in ((GMS, 8080), (FRONTEND, 9002)):
            with self.subTest(service=name):
                spec = self.named[("Service", name)]["spec"]
                self.assertEqual(spec["type"], "ClusterIP")
                self.assertIn(("http", port), [(p["name"], p["port"]) for p in spec["ports"]])
                pod_labels = self.pods[name]["metadata"]["labels"]
                for key, value in spec["selector"].items():
                    self.assertEqual(pod_labels[key], value)

    def test_upstreams_memory_and_non_root_user(self):
        # ADR-071 §2: tuning values, upstream's.
        memory = {GMS: ("1Gi", "2Gi"), FRONTEND: ("512Mi", "1400Mi"),
                  SYSTEM_UPDATE: ("2Gi", "2Gi"), SYSTEM_UPDATE_NONBLOCKING: ("2Gi", "2Gi")}
        for name, (request, limit) in memory.items():
            with self.subTest(pod=name):
                resources = self.pods[name]["spec"]["containers"][0]["resources"]
                self.assertEqual((resources["requests"]["memory"], resources["limits"]["memory"]),
                                 (request, limit))
        for name, pod in self.pods.items():
            for c in containers(pod):
                with self.subTest(pod=name, container=c["name"]):
                    sc = c["securityContext"]
                    self.assertTrue(sc["runAsNonRoot"])
                    self.assertEqual(sc["runAsUser"], DATAHUB_USER)
                    self.assertFalse(sc["allowPrivilegeEscalation"])

    def test_every_container_runs_by_tag_and_digest(self):
        # ADR-071 §3.
        expected = {GMS: "gms", FRONTEND: "frontend", SYSTEM_UPDATE: "upgrade",
                    SYSTEM_UPDATE_NONBLOCKING: "upgrade", RESTORE_INDICES: "upgrade",
                    INGESTION: "ingestion"}
        self.assertEqual(sorted(self.pods), sorted(expected))
        for name, pod in self.pods.items():
            for c in containers(pod):
                with self.subTest(pod=name, container=c["name"]):
                    self.assertRegex(c["image"], DIGEST_RE)
                    self.assertTrue(c["image"].startswith(
                        f"{IMAGES[expected[name]]}:{VERSION}@sha256:"), c["image"])

    def test_every_label_value_is_valid_and_gms_names_its_version_alone(self):
        # ADR-072 §1: the API server refuses a label value with the digest in it.
        for doc in self.docs:
            for found in label_maps(doc):
                for key, value in found.items():
                    with self.subTest(kind=doc["kind"], name=doc["metadata"]["name"], label=key):
                        self.assertLessEqual(len(str(value)), 63)
                        self.assertRegex(str(value), LABEL_VALUE_RE)
        for kind in ("Service", "Deployment"):
            for found in label_maps(self.named[(kind, GMS)]):
                self.assertEqual(found["datahub.acryl.io/datahub-version"], VERSION)

    def test_every_pod_is_a_client_of_the_three_stores_and_of_gms(self):
        # ADR-071 §4: the hook Jobs and restore-indices take global.podLabels only.
        for name, pod in self.pods.items():
            with self.subTest(pod=name):
                self.assertLessEqual(CLIENT_LABELS.items(), pod["metadata"]["labels"].items())

    def test_datahub_pg(self):
        # ADR-071 §4: datahub-pg's chart creates the database and its owner (ADR-066 §2).
        # Upstream's `createDatabase | default "true"` renders a false as "true", and its schema
        # takes a boolean only. On PostgreSQL that step looks the database up in pg_database and
        # creates nothing when it exists (datahub-upgrade, PostgresDatabaseOperations, v1.7.0).
        for c in [self.gms] + self.jobs:
            with self.subTest(container=c["name"]):
                variables = env(c)
                self.assertEqual(variables["EBEAN_DATASOURCE_HOST"], "datahub-pg-rw:5432")
                self.assertEqual(variables["EBEAN_DATASOURCE_URL"],
                                 "jdbc:postgresql://datahub-pg-rw:5432/datahub")
                self.assertEqual(variables["EBEAN_DATASOURCE_DRIVER"], "org.postgresql.Driver")
                self.assertEqual(variables["EBEAN_DATASOURCE_USERNAME"], "datahub")
                self.assertEqual(variables["EBEAN_DATASOURCE_PASSWORD"],
                                 secret("datahub-db", "password"))
        self.assertIs(load("values-lakehouse.yaml")["datahubSystemUpdate"]["sql"]["setup"]
                      ["createDatabase"], False)
        blocking = env(self.jobs[0])
        self.assertEqual(blocking["CREATE_DB"], "true")
        self.assertNotIn("CREATE_USER", blocking)

    def test_datahub_kafka(self):
        # ADR-071 §4, ADR-068 §2: plaintext, the internal registry, 5 MiB.
        for c in [self.gms, self.frontend] + self.jobs:
            with self.subTest(container=c["name"]):
                variables = env(c)
                self.assertEqual(variables["KAFKA_BOOTSTRAP_SERVER"], "datahub-kafka:9092")
                self.assertEqual(variables["KAFKA_PRODUCER_MAX_REQUEST_SIZE"], "5242880")
                self.assertEqual(variables["KAFKA_CONSUMER_MAX_PARTITION_FETCH_BYTES"], "5242880")
                self.assertEqual([n for n in variables if "SASL" in n or "SECURITY_PROTOCOL" in n],
                                 [])
        self.assertEqual(env(self.gms)["SCHEMA_REGISTRY_TYPE"], "INTERNAL")
        for c in self.jobs:
            with self.subTest(container=c["name"]):
                self.assertEqual(env(c)["MAX_MESSAGE_BYTES"], "5242880")
                self.assertEqual(env(c)["DATAHUB_PRECREATE_TOPICS"], "true")

    def test_datahub_opensearch(self):
        # ADR-071 §4, ADR-070: plain HTTP without login, no index replica on one node.
        for c in [self.gms] + self.jobs:
            with self.subTest(container=c["name"]):
                variables = env(c)
                self.assertEqual(variables["ELASTICSEARCH_HOST"], "datahub-opensearch")
                self.assertEqual(variables["ELASTICSEARCH_PORT"], "9200")
                self.assertEqual(variables["ELASTICSEARCH_USE_SSL"], "false")
                self.assertEqual(variables["ELASTICSEARCH_NUM_REPLICAS_PER_INDEX"], "0")
                self.assertNotIn("ELASTICSEARCH_USERNAME", variables)
                self.assertNotIn("INDEX_PREFIX", variables)
        variables = env(self.frontend)
        self.assertEqual((variables["ELASTIC_CLIENT_HOST"], variables["ELASTIC_CLIENT_PORT"],
                          variables["ELASTIC_CLIENT_USE_SSL"]),
                         ("datahub-opensearch", "9200", "false"))

    def test_no_secret_is_rendered_and_every_credential_is_init_envs(self):
        # ADR-071 §6, ADR-072 §2.
        self.assertNotIn("Secret", [d["kind"] for d in self.docs])
        auth = {"DATAHUB_SYSTEM_CLIENT_SECRET": secret("datahub-auth-secrets",
                                                       "system_client_secret"),
                "DATAHUB_TOKEN_SERVICE_SIGNING_KEY": secret("datahub-auth-secrets",
                                                            "token_service_signing_key"),
                "DATAHUB_TOKEN_SERVICE_SALT": secret("datahub-auth-secrets",
                                                     "token_service_salt")}
        for c in [self.gms] + self.jobs:
            with self.subTest(container=c["name"]):
                self.assertLessEqual(auth.items(), env(c).items())
        self.assertEqual(env(self.gms)["SECRET_SERVICE_ENCRYPTION_KEY"],
                         secret("datahub-encryption-secrets", "encryption_key_secret"))
        variables = env(self.frontend)
        self.assertEqual(variables["DATAHUB_SECRET"], secret("datahub-frontend-secret", "secret"))
        self.assertEqual(variables["DATAHUB_SYSTEM_CLIENT_SECRET"],
                         auth["DATAHUB_SYSTEM_CLIENT_SECRET"])

    def test_the_root_users_password_is_mounted_over_the_images(self):
        # ADR-071 §6: the image's user.props (datahub:datahub) never applies.
        volumes = {v["name"]: v for v in self.pods[FRONTEND]["spec"]["volumes"]}
        (mount,) = [m for m in self.frontend["volumeMounts"]
                    if m["mountPath"] == "/datahub-frontend/conf/user.props"]
        self.assertEqual(mount["subPath"], "user.props")
        self.assertTrue(mount["readOnly"])
        self.assertEqual(volumes[mount["name"]]["secret"]["secretName"], "datahub-root-user")

    def test_the_ingestion_cronjob_runs_daily_one_run_at_a_time(self):
        # ADR-076 §1: tuning values in the values file.
        spec = self.named[("CronJob", INGESTION)]["spec"]
        self.assertEqual((spec["schedule"], spec["concurrencyPolicy"],
                          spec["successfulJobsHistoryLimit"], spec["failedJobsHistoryLimit"],
                          spec["suspend"]), ("0 3 * * *", "Forbid", 3, 3, False))
        job = spec["jobTemplate"]["spec"]
        self.assertEqual(job["backoffLimit"], 1)
        self.assertEqual(job["template"]["spec"]["restartPolicy"], "Never")

    def test_the_recipe_is_the_adrs_with_no_credential(self):
        # ADR-076 §2: every value that varies is a ${VAR}; no credential is in it.
        configmap = self.named[("ConfigMap", RECIPE_CONFIGMAP)]
        self.assertEqual(list(configmap["data"]), [RECIPE_FILE])
        self.assertEqual(yaml.safe_load(configmap["data"][RECIPE_FILE]), RECIPE)
        pod = self.pods[INGESTION]["spec"]
        (c,) = pod["containers"]
        (mount,) = c["volumeMounts"]
        self.assertEqual(mount["mountPath"], "/etc/recipe")
        volumes = {v["name"]: v for v in pod["volumes"]}
        self.assertEqual(volumes[mount["name"]]["configMap"]["name"], RECIPE_CONFIGMAP)

    def test_the_ingestion_environment(self):
        # ADR-076 §2: datahub_ingest's credential and the system client's secret from
        # init-env's Secrets; the system client's ID as a value. POLARIS_URI and the catalogs
        # come from the install (install/release.py).
        (c,) = self.pods[INGESTION]["spec"]["containers"]
        self.assertEqual(env(c), {
            "DATAHUB_SYSTEM_CLIENT_ID": "__datahub_system",
            "POLARIS_CLIENT_ID": secret("datahub-polaris-principal", "clientId"),
            "POLARIS_CLIENT_SECRET": secret("datahub-polaris-principal", "clientSecret"),
            "DATAHUB_SYSTEM_CLIENT_SECRET": secret("datahub-auth-secrets",
                                                   "system_client_secret")})

    def test_the_ingestion_command_runs_every_catalog_and_fails_if_one_did(self):
        # ADR-076 §2: one run per catalog, one after another, each named in the log; every
        # catalog runs after a failure; the Job fails if any run failed.
        (c,) = self.pods[INGESTION]["spec"]["containers"]
        self.assertEqual(c["command"][:2], ["/bin/sh", "-c"])
        recipe = "datahub ingest -c /etc/recipe/polaris.yml for"
        with tempfile.TemporaryDirectory() as d:
            stub = os.path.join(d, "datahub")
            with open(stub, "w", encoding="utf-8") as f:
                f.write('#!/bin/sh\necho "datahub $* for $POLARIS_CATALOG"\n'
                        '[ "$POLARIS_CATALOG" != bad ]\n')
            os.chmod(stub, 0o755)
            path = d + os.pathsep + os.environ["PATH"]
            for catalogs, status, lines in (
                    ("one,bad,two", 1, ["catalog one: ingesting", f"{recipe} one",
                                        "catalog one: passed", "catalog bad: ingesting",
                                        f"{recipe} bad", "catalog bad: failed",
                                        "catalog two: ingesting", f"{recipe} two",
                                        "catalog two: passed"]),
                    ("one", 0, ["catalog one: ingesting", f"{recipe} one",
                                "catalog one: passed"])):
                with self.subTest(catalogs=catalogs):
                    done = subprocess.run(c["command"], capture_output=True, text=True,
                                          check=False, env={
                                              "PATH": path,
                                              "DATAHUB_INGEST_POLARIS_CATALOGS": catalogs})
                    self.assertEqual((done.returncode, done.stdout.splitlines()),
                                     (status, lines), done.stderr)

    def test_kubeconform_accepts_the_rendered_chart(self):
        result = kubeconform(helm_template().stdout)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"Valid: {len(self.docs)}", result.stdout)


class InstallValues(unittest.TestCase):
    """ADR-076 §2: the chart rendered with the values install/release.py adds."""

    def render(self, catalogs):
        env = {"NS_CATALOG_POLARIS": "catalog-polaris",
               "DATAHUB_INGEST_POLARIS_CATALOGS": catalogs}
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "install.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(gate.release.datahub_values(env), f)
            result = subprocess.run(["helm", "template", RELEASE, CHART, "--namespace",
                                     NAMESPACE, "-f", VALUES, "-f", path],
                                    capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        (cron,) = [d for d in yaml.safe_load_all(result.stdout)
                   if d and d["kind"] == "CronJob" and d["metadata"]["name"] == INGESTION]
        return cron

    def test_the_install_adds_polaris_uri_and_the_catalogs_to_the_environment(self):
        cron = self.render("gate_ingest_catalog")
        (c,) = pod_template(cron)["spec"]["containers"]
        variables = env(c)
        self.assertEqual(variables["POLARIS_URI"],
                         "http://polaris.catalog-polaris.svc.cluster.local:8181/api/catalog")
        self.assertEqual(variables["DATAHUB_INGEST_POLARIS_CATALOGS"], "gate_ingest_catalog")
        self.assertEqual(variables["DATAHUB_SYSTEM_CLIENT_ID"], "__datahub_system")
        self.assertEqual(variables["POLARIS_CLIENT_ID"],
                         secret("datahub-polaris-principal", "clientId"))
        self.assertIs(cron["spec"]["suspend"], False)

    def test_no_catalog_suspends_the_cronjob(self):
        self.assertIs(self.render("")["spec"]["suspend"], True)


class CronEntry(unittest.TestCase):
    """Upstream's subchart reads each entry of `crons` inside `range`, as `$val.<key>` or
    `.<key>`, and its recipe as `$val.recipe.<key>`: every key of ours is one of those."""

    @classmethod
    def setUpClass(cls):
        cls.text = ""
        for path in CRON_TEMPLATES:
            with open(os.path.join(CHART, path), encoding="utf-8") as f:
                cls.text += f.read()
        cls.crons = load("values-lakehouse.yaml")["datahub-ingestion-cron"]["crons"]

    def read(self, key, recipe=False):
        if recipe:
            return re.search(r"\$val\.recipe\." + re.escape(key) + r"\b", self.text)
        return re.search(r"(\$val|[\s(])\." + re.escape(key) + r"\b", self.text)

    def test_one_entry_polaris(self):
        self.assertEqual(list(self.crons), ["polaris"])

    def test_every_key_of_the_entry_is_read_by_upstreams_templates(self):
        for key in self.crons["polaris"]:
            with self.subTest(key=key):
                self.assertTrue(self.read(key), key)
        for key in self.crons["polaris"]["recipe"]:
            with self.subTest(recipe=key):
                self.assertTrue(self.read(key, recipe=True), key)

    def test_a_misspelt_key_is_not_found(self):
        self.assertFalse(self.read("schedul"))
        self.assertFalse(self.read("fileContents", recipe=True))
        self.assertTrue(self.read("schedule"))


if __name__ == "__main__":
    unittest.main()
