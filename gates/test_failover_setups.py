"""Tests for failover/setups.py: what each setup and failure is (ADR-047 §2, §3, §5).
Run: python3 -m unittest discover -s gates"""

import json
import os
import sys
import unittest
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "failover"))
sys.path.insert(0, HERE)
import gate  # noqa: E402
import network  # noqa: E402
import release  # noqa: E402
import setups  # noqa: E402

NS = "catalog-polaris"


def query(url):
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url[len("jdbc:"):]).query))


class Setups(unittest.TestCase):
    def test_the_cnpg_setups_of_this_pr(self):
        self.assertEqual(setups.SETUPS, ("C0", "C1", "C2"))
        self.assertEqual(setups.VARIANTS, ("C1q",))
        self.assertEqual(setups.SCENARIOS, ("S1", "S2", "S3"))
        setups.check("C1q", "S3")
        with self.assertRaises(ValueError):
            setups.check("C3a", "S1")
        with self.assertRaises(ValueError):
            setups.check("C0", "S4")

    def test_c0_is_the_chart_as_released(self):
        self.assertIsNone(setups.cluster_patch("C0"))
        self.assertIsNone(setups.pooler("C0", NS))

    def test_c1_turns_detection_shutdown_and_synchronous_replication(self):
        spec = setups.cluster_patch("C1")["spec"]
        self.assertEqual(spec["postgresql"]["synchronous"],
                         {"method": "any", "number": 1, "dataDurability": "required"})
        self.assertEqual(spec["failoverDelay"], 0)
        self.assertEqual(spec["smartShutdownTimeout"], 0)
        readiness = spec["probes"]["readiness"]
        self.assertLessEqual(readiness["periodSeconds"] * readiness["failureThreshold"], 5)
        self.assertNotIn("liveness", spec["probes"])  # the isolation check stays as CNPG's

    def test_c1q_is_c1_with_a_failover_quorum(self):
        # ADR-056 §4: failoverQuorum is measured as a C1 variant before any release has it.
        c1, c1q = setups.cluster_patch("C1")["spec"], setups.cluster_patch("C1q")["spec"]
        self.assertIs(c1q["postgresql"]["synchronous"]["failoverQuorum"], True)
        self.assertNotIn("failoverQuorum", c1["postgresql"]["synchronous"])
        without = json.loads(json.dumps(c1q))
        del without["postgresql"]["synchronous"]["failoverQuorum"]
        self.assertEqual(without, c1)
        self.assertEqual(setups.jdbc_url("C1q"), setups.jdbc_url("C1"))
        self.assertIsNone(setups.pooler("C1q", NS))

    def test_c2_is_c1_with_a_session_pooler_on_the_control_plane(self):
        self.assertEqual(setups.cluster_patch("C2"), setups.cluster_patch("C1"))
        pooler = setups.pooler("C2", NS)
        self.assertEqual(pooler["kind"], "Pooler")
        self.assertEqual(pooler["metadata"], {"name": "polaris-pg-pooler-rw", "namespace": NS})
        spec = pooler["spec"]
        self.assertEqual(spec["cluster"], {"name": release.POLARIS_PG})
        self.assertEqual((spec["type"], spec["instances"]), ("rw", 1))
        self.assertEqual(spec["pgbouncer"]["poolMode"], "session")
        template = spec["template"]
        # ADR-044's rule lets polaris-pg's clients reach the instances on 5432.
        self.assertEqual(template["metadata"]["labels"], {release.PG_CLIENT_LABEL: "true"})
        self.assertEqual(template["spec"]["nodeSelector"],
                         {"kubernetes.io/hostname": setups.CONTROL_PLANE})

    def test_c0_connects_as_polaris_does_and_c1_adds_client_timeouts(self):
        polaris = query(release.jdbc_url({}))
        c0 = setups.jdbc_url("C0")
        self.assertTrue(c0.startswith("jdbc:postgresql://polaris-pg-rw:5432/failover_probe?"))
        self.assertEqual(query(c0), polaris)
        c1 = query(setups.jdbc_url("C1"))
        self.assertEqual({k: c1[k] for k in polaris}, polaris)
        for timeout in ("connectTimeout", "socketTimeout", "loginTimeout"):
            self.assertGreater(int(c1[timeout]), 0)
        self.assertEqual(c1["tcpKeepAlive"], "true")
        self.assertTrue(setups.jdbc_url("C2").startswith(
            "jdbc:postgresql://polaris-pg-pooler-rw:5432/failover_probe?"))
        self.assertEqual(query(setups.jdbc_url("C2")), c1)

    def test_settings_name_every_input_of_the_setup(self):
        got = setups.settings("C1", grace_s=20)
        self.assertEqual(got["cluster"], setups.cluster_patch("C1")["spec"])
        self.assertEqual(got["jdbc_url"], setups.jdbc_url("C1"))
        self.assertEqual(got["node_monitor_grace_period_s"], 20)
        self.assertIsNone(setups.settings("C0")["node_monitor_grace_period_s"])


class ProbePod(unittest.TestCase):
    def pod(self, setup="C1"):
        return setups.probe_pod(setup, NS, "lakehouse/failover-probe:abc", "probe-c1-s1-1",
                                "C1-S1-1")

    def test_runs_on_the_control_plane_with_the_check_label(self):
        pod = self.pod()
        self.assertEqual(pod["metadata"]["labels"], {network.CHECK_LABEL: "true"})
        self.assertEqual(pod["spec"]["nodeSelector"],
                         {"kubernetes.io/hostname": setups.CONTROL_PLANE})
        self.assertEqual(pod["spec"]["restartPolicy"], "Never")

    def test_logs_in_as_polaris_from_its_secret_and_writes_to_its_own_database(self):
        container = self.pod()["spec"]["containers"][0]
        self.assertEqual(container["imagePullPolicy"], "Never")
        env = {e["name"]: e for e in container["env"]}
        self.assertEqual(env["PGUSER"]["valueFrom"]["secretKeyRef"],
                         {"name": release.POLARIS_DB_SECRET, "key": "username"})
        self.assertEqual(env["PGPASSWORD"]["valueFrom"]["secretKeyRef"],
                         {"name": release.POLARIS_DB_SECRET, "key": "password"})
        self.assertEqual(env["PROBE_URL"]["value"], setups.jdbc_url("C1"))
        self.assertEqual(env["PROBE_RUN"]["value"], "C1-S1-1")
        self.assertEqual(env["PROBE_PERIOD_MS"]["value"], "100")
        self.assertNotEqual(setups.PROBE_DATABASE, gate.polaris_pg_values()["database"])


class KindConfig(unittest.TestCase):
    BASE = ("kind: Cluster\napiVersion: kind.x-k8s.io/v1alpha4\nnodes:\n"
            "  - role: control-plane\n  - role: worker\n")

    def test_unchanged_without_a_grace_period(self):
        self.assertEqual(setups.kind_config(self.BASE, None), self.BASE)

    def test_a_grace_period_patches_the_controller_manager(self):
        text = setups.kind_config(self.BASE, 20)
        self.assertTrue(text.startswith("kind: Cluster\napiVersion: kind.x-k8s.io/v1alpha4\n"))
        self.assertTrue(text.endswith("nodes:\n  - role: control-plane\n  - role: worker\n"))
        self.assertIn("kubeadmConfigPatches:\n", text)
        self.assertIn("    kind: ClusterConfiguration\n", text)
        self.assertIn("        - name: node-monitor-grace-period\n          value: 20s\n", text)

    def test_the_real_config_has_one_nodes_list_to_patch_before(self):
        with open(gate.KIND_CONFIG, encoding="utf-8") as f:
            base = f.read()
        self.assertEqual(base.count("\nnodes:\n"), 1)
        self.assertNotIn("kubeadmConfigPatches", base)
        self.assertIn("node-monitor-grace-period", setups.kind_config(base, 20))

    def test_the_grace_period_is_checked_on_the_running_controller_manager(self):
        command = ["kube-controller-manager", "--node-monitor-grace-period=20s", "--v=2"]
        self.assertTrue(setups.grace_applied(command, 20))
        self.assertFalse(setups.grace_applied(command[:1], 20))
        self.assertFalse(setups.grace_applied(command, 30))


class Commands(unittest.TestCase):
    def test_everything_but_the_instances_is_pinned_to_the_control_plane(self):
        env = {"NS_CNPG_SYSTEM": "cnpg-system", "NS_STANDINS": "standins"}
        self.assertEqual(setups.system_namespaces(env),
                         ["kube-system", "local-path-storage", "cert-manager", "cnpg-system"])
        self.assertEqual(setups.pin_patch(), {"spec": {"template": {"spec": {
            "nodeSelector": {"kubernetes.io/hostname": setups.CONTROL_PLANE}}}}})
        self.assertEqual(setups.CONTROL_PLANE, f"{gate.CLUSTER}-control-plane")

    def test_s1_deletes_the_primary_pod(self):
        argv = setups.inject_command("S1", NS, "polaris-pg-2", "lakehouse-gate-worker")
        self.assertEqual(argv, ["kubectl", "--context", gate.CONTEXT, "-n", NS, "delete", "pod",
                                "polaris-pg-2", "--wait=false"])

    def test_s2_kills_the_postmaster(self):
        argv = setups.inject_command("S2", NS, "polaris-pg-2", "lakehouse-gate-worker")
        self.assertEqual(argv[:9], ["kubectl", "--context", gate.CONTEXT, "-n", NS, "exec",
                                    "polaris-pg-2", "-c", "postgres"])
        self.assertIn("kill -9 $(head -n 1 /var/lib/postgresql/data/pgdata/postmaster.pid)",
                      argv[-1])

    def test_s3_pauses_the_node_and_refuses_the_control_plane(self):
        self.assertEqual(setups.inject_command("S3", NS, "polaris-pg-2", "lakehouse-gate-worker"),
                         ["docker", "pause", "lakehouse-gate-worker"])
        self.assertEqual(setups.resume_command("lakehouse-gate-worker"),
                         ["docker", "unpause", "lakehouse-gate-worker"])
        with self.assertRaises(ValueError):
            setups.inject_command("S3", NS, "polaris-pg-1", setups.CONTROL_PLANE)

    def test_switchover_patches_the_status_as_cnpgs_promote_does(self):
        patch = setups.switchover_patch("polaris-pg-3", "2026-10-13T05:00:00.000000Z")
        self.assertEqual(patch, {"status": {
            "targetPrimary": "polaris-pg-3",
            "targetPrimaryTimestamp": "2026-10-13T05:00:00.000000Z",
            "phase": "Switchover in progress",
            "phaseReason": "Switching over to polaris-pg-3"}})

    def test_the_rows_are_read_for_one_run(self):
        sql = setups.rows_query("C1-S2-3")
        self.assertIn("FROM probe_writes WHERE run = 'C1-S2-3'", sql)
        with self.assertRaises(ValueError):
            setups.rows_query("x'; drop table probe_writes; --")

    def test_observation_from_the_cluster_and_its_endpoint_slices(self):
        cluster = {"status": {"phase": "Failing over", "currentPrimary": "polaris-pg-2",
                              "currentPrimaryTimestamp": "2026-10-13T05:00:00.000000Z"}}
        slices = {"items": [{"endpoints": [
            {"conditions": {"ready": True}, "targetRef": {"name": "polaris-pg-2"}},
            {"conditions": {"ready": False}, "targetRef": {"name": "polaris-pg-1"}}]}]}
        self.assertEqual(setups.observation(5, cluster, slices), {
            "t": 5, "phase": "Failing over", "current_primary": "polaris-pg-2",
            "current_primary_timestamp": "2026-10-13T05:00:00.000000Z",
            "endpoints": ["polaris-pg-2"]})


class Switchover(unittest.TestCase):
    @staticmethod
    def pods(*placed):
        return {"items": [{"metadata": {"name": name}, "spec": {"nodeName": node},
                           "status": {"conditions": [{"type": "Ready", "status": ready}]}}
                          for name, node, ready in placed]}

    def test_a_ready_standby_on_a_worker(self):
        pods = self.pods(("polaris-pg-1", setups.CONTROL_PLANE, "True"),
                         ("polaris-pg-2", "lakehouse-gate-worker", "False"),
                         ("polaris-pg-3", "lakehouse-gate-worker2", "True"))
        self.assertEqual(setups.switchover_target(pods, "polaris-pg-1"), "polaris-pg-3")

    def test_none_when_the_primary_is_already_on_a_worker(self):
        pods = self.pods(("polaris-pg-1", "lakehouse-gate-worker", "True"),
                         ("polaris-pg-2", setups.CONTROL_PLANE, "True"))
        self.assertIsNone(setups.switchover_target(pods, "polaris-pg-1"))

    def test_no_standby_to_take_it_is_an_error(self):
        pods = self.pods(("polaris-pg-1", setups.CONTROL_PLANE, "True"),
                         ("polaris-pg-2", "lakehouse-gate-worker", "False"))
        with self.assertRaises(ValueError):
            setups.switchover_target(pods, "polaris-pg-1")


class Matrix(unittest.TestCase):
    def test_every_setup_and_the_grace_job_by_default(self):
        self.assertEqual(setups.matrix("", "", True), [
            {"name": "C0", "setup": "C0", "grace": "", "scenarios": "S1,S2,S3"},
            {"name": "C1", "setup": "C1", "grace": "", "scenarios": "S1,S2,S3"},
            {"name": "C2", "setup": "C2", "grace": "", "scenarios": "S1,S2,S3"},
            {"name": "C1-grace20", "setup": "C1", "grace": "20", "scenarios": "S3"}])

    def test_inputs_narrow_it(self):
        self.assertEqual(setups.matrix("C2", "S1,S3", False), [
            {"name": "C2", "setup": "C2", "grace": "", "scenarios": "S1,S3"}])
        self.assertEqual(setups.matrix("C0", "S1", True), [
            {"name": "C0", "setup": "C0", "grace": "", "scenarios": "S1"}])

    def test_a_variant_runs_only_when_named(self):
        self.assertNotIn("C1q", [j["setup"] for j in setups.matrix("", "", True)])
        self.assertEqual(setups.matrix("C1q", "", True), [
            {"name": "C1q", "setup": "C1q", "grace": "", "scenarios": "S1,S2,S3"}])
        self.assertEqual([j["name"] for j in setups.matrix("C1q,C1", "S1", False)],
                         ["C1", "C1q"])

    def test_unknown_inputs_are_refused(self):
        with self.assertRaises(ValueError):
            setups.matrix("C0,C3a", "", False)
        with self.assertRaises(ValueError):
            setups.matrix("", "S1,S9", False)


class ProbeImage(unittest.TestCase):
    def test_the_dockerfile_builds_on_the_pinned_jdk(self):
        pins = gate.load_pins()["failover"]
        with open(os.path.join(setups.PROBE_DIR, "Dockerfile"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn(f"FROM {pins['jdk_image']['image']}\n", text)
        self.assertIn("@sha256:", pins["jdk_image"]["image"])

    def test_pgjdbc_is_pinned_at_polaris_version_from_maven_central(self):
        pin = gate.load_pins()["failover"]["pgjdbc"]
        self.assertEqual(pin["url"], "https://repo1.maven.org/maven2/org/postgresql/postgresql/"
                                     f"{pin['version']}/postgresql-{pin['version']}.jar")
        self.assertEqual(len(pin["sha256"]), 64)

    def test_the_image_tag_follows_the_sources(self):
        files = {"Probe.java": b"a", "Dockerfile": b"b", "postgresql.jar": b"c"}
        tag = setups.probe_image(files)
        self.assertTrue(tag.startswith("lakehouse/failover-probe:"))
        self.assertNotEqual(tag, setups.probe_image({**files, "Probe.java": b"x"}))
        self.assertEqual(tag, setups.probe_image(dict(reversed(list(files.items())))))


if __name__ == "__main__":
    unittest.main()
