"""Tests for standins.py. Run: python3 -m unittest discover -s gates"""

import base64
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import gate  # noqa: E402
import standins  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ENV = {"NS_STANDINS": "standins", "NS_CATALOG_POLARIS": "catalog-polaris",
       "POLARIS_S3_ACCESS_KEY_ID": "gatekey",
       "POLARIS_S3_SECRET_ACCESS_KEY": "gatesecret",
       "SMOKE_S3_ACCESS_KEY_ID": "smokekey",
       "SMOKE_S3_SECRET_ACCESS_KEY": "smokesecret",
       "POLARIS_BACKUP_S3_ACCESS_KEY_ID": "backupkey",
       "POLARIS_BACKUP_S3_SECRET_ACCESS_KEY": "backupsecret",
       "POLARIS_WAREHOUSE_BUCKET": "polaris-warehouse",
       "POLARIS_BACKUP_BUCKET": "polaris-backup"}
PINS = gate.load_pins(os.path.join(HERE, "pins.json"))


TLS = ("certificate-pem\n", "key-pem\n")


def items(env=ENV):
    return standins.manifests(env, PINS, "package polaris.authz\n", "# proxy.py\n", TLS)["items"]


def objects(env=ENV):
    return {(o["kind"], o["metadata"]["name"]): o for o in items(env)}


def container(deployment):
    (c,) = deployment["spec"]["template"]["spec"]["containers"]
    return c


class Pins(unittest.TestCase):
    def test_each_stand_in_image_is_pinned_by_digest(self):
        for name in ("seaweedfs", "opa", "recorder"):
            with self.subTest(name=name):
                self.assertRegex(PINS["standin_images"][name]["image"],
                                 r"^([a-z]+/)?[a-z]+:\d+\.\d+(\.\d+)?(-slim)?@sha256:[0-9a-f]{64}$")


class Manifests(unittest.TestCase):
    def test_the_namespace_comes_first_and_holds_every_other_object(self):
        listed = items()
        self.assertEqual((listed[0]["kind"], listed[0]["metadata"]["name"]),
                         ("Namespace", "standins"))
        for o in listed[1:]:
            with self.subTest(object=o["metadata"]["name"]):
                self.assertEqual(o["metadata"]["namespace"], "standins")

    def test_the_namespace_name_comes_from_env(self):
        self.assertIn(("Namespace", "other-ns"), objects({**ENV, "NS_STANDINS": "other-ns"}))

    def test_seaweedfs_runs_mini_with_the_bucket_and_without_telemetry(self):
        c = container(objects()[("Deployment", "seaweedfs")])
        self.assertEqual(c["image"], PINS["standin_images"]["seaweedfs"]["image"])
        self.assertEqual(c["args"][0], "mini")
        for arg in ("-s3.port=8333", "-bucket=polaris-warehouse", "-master.telemetry=false"):
            self.assertIn(arg, c["args"])

    def test_seaweedfs_reads_its_identities_from_the_secret(self):
        o = objects()
        c = container(o[("Deployment", "seaweedfs")])
        self.assertIn("-s3.config=/etc/seaweedfs/s3.json", c["args"])
        self.assertNotIn("env", c)
        self.assertIn({"name": "s3-config", "mountPath": "/etc/seaweedfs", "readOnly": True},
                      c["volumeMounts"])
        volumes = o[("Deployment", "seaweedfs")]["spec"]["template"]["spec"]["volumes"]
        self.assertIn({"name": "s3-config", "secret": {"secretName": "seaweedfs-s3"}}, volumes)

    def test_polaris_key_may_do_everything_and_the_smoke_key_only_the_bucket(self):
        config = json.loads(base64.b64decode(
            objects()[("Secret", "seaweedfs-s3")]["data"]["s3.json"]))
        by_name = {i["name"]: i for i in config["identities"]}
        self.assertEqual(sorted(by_name), ["backup", "polaris", "smoke"])
        self.assertEqual(by_name["polaris"]["credentials"],
                         [{"accessKey": "gatekey", "secretKey": "gatesecret"}])
        self.assertIn("Admin", by_name["polaris"]["actions"])
        self.assertEqual(by_name["smoke"]["credentials"],
                         [{"accessKey": "smokekey", "secretKey": "smokesecret"}])
        self.assertEqual(sorted(by_name["smoke"]["actions"]),
                         ["List:polaris-warehouse", "Read:polaris-warehouse",
                          "Write:polaris-warehouse"])

    def test_the_backup_key_may_read_write_and_list_its_own_bucket_only(self):
        # ADR-043 §2: the backups' own key, never Polaris's warehouse key.
        config = json.loads(base64.b64decode(
            objects()[("Secret", "seaweedfs-s3")]["data"]["s3.json"]))
        backup = next(i for i in config["identities"] if i["name"] == "backup")
        self.assertEqual(backup["credentials"],
                         [{"accessKey": "backupkey", "secretKey": "backupsecret"}])
        self.assertEqual(sorted(backup["actions"]),
                         ["List:polaris-backup", "Read:polaris-backup", "Write:polaris-backup"])

    def test_the_credentials_appear_only_in_the_secret(self):
        o = objects()
        del o[("Secret", "seaweedfs-s3")]
        text = json.dumps(list(o.values()))
        for value in ("gatekey", "gatesecret", "smokekey", "smokesecret", "backupkey",
                      "backupsecret"):
            self.assertNotIn(value, text)
            self.assertNotIn(base64.b64encode(value.encode()).decode(), text)

    def test_seaweedfs_is_ready_only_once_its_bucket_exists(self):
        probe = container(objects()[("Deployment", "seaweedfs")])["readinessProbe"]
        self.assertIn("/buckets/polaris-warehouse/", " ".join(probe["exec"]["command"]))

    def test_opa_serves_the_policy_from_its_configmap_on_all_addresses(self):
        o = objects()
        c = container(o[("Deployment", "opa")])
        self.assertEqual(c["image"], PINS["standin_images"]["opa"]["image"])
        self.assertEqual(c["args"][:2], ["run", "--server"])
        self.assertIn("--addr=0.0.0.0:8181", c["args"])
        self.assertIn("--ignore=.*", c["args"])
        self.assertEqual(c["readinessProbe"]["httpGet"]["path"], "/health")
        self.assertEqual(o[("ConfigMap", "opa-policy")]["data"]["policy.rego"],
                         "package polaris.authz\n")

    def test_the_recorder_listens_for_each_connection_and_forwards_to_its_service(self):
        c = container(objects()[("Deployment", "recorder")])
        self.assertEqual(c["image"], PINS["standin_images"]["recorder"]["image"])
        self.assertEqual(c["command"][:2], ["python3", "/recorder/proxy.py"])
        args = c["command"][2:]
        pairs = list(zip(args[0::2], args[1::2]))
        self.assertIn(("--http", "opa=8181:opa.standins.svc.cluster.local:8181"), pairs)
        self.assertIn(("--http", "s3=8333:seaweedfs.standins.svc.cluster.local:8333"), pairs)
        self.assertIn(("--pg", "pg=5432:polaris-pg-rw.catalog-polaris.svc.cluster.local:5432"),
                      pairs)
        self.assertIn(("--tls-cert", "/tls/tls.crt"), pairs)
        self.assertIn(("--tls-key", "/tls/tls.key"), pairs)

    def test_the_recorders_readiness_opens_no_connection_it_would_relay(self):
        c = container(objects()[("Deployment", "recorder")])
        probe = c["readinessProbe"]
        self.assertNotIn("tcpSocket", probe)
        self.assertNotIn("httpGet", probe)
        args = c["command"][2:]
        ready = dict(zip(args[0::2], args[1::2]))["--ready-file"]
        self.assertEqual(probe["exec"]["command"], ["test", "-f", ready])

    def test_the_recorder_runs_its_code_from_a_configmap_and_its_certificate_from_a_secret(self):
        o = objects()
        self.assertEqual(o[("ConfigMap", "recorder-code")]["data"]["proxy.py"], "# proxy.py\n")
        tls = o[("Secret", "recorder-tls")]
        self.assertEqual(tls["type"], "kubernetes.io/tls")
        self.assertEqual(base64.b64decode(tls["data"]["tls.crt"]).decode(), TLS[0])
        mounts = {m["mountPath"] for m in container(o[("Deployment", "recorder")])["volumeMounts"]}
        self.assertEqual(mounts, {"/recorder", "/tls"})

    def test_the_services_expose_s3_on_8333_and_opa_on_8181(self):
        o = objects()
        for name, port in (("seaweedfs", 8333), ("opa", 8181)):
            with self.subTest(name=name):
                svc = o[("Service", name)]["spec"]
                self.assertEqual(svc["ports"][0]["port"], port)
                self.assertEqual(svc["selector"], {"app.kubernetes.io/name": name})
        ports = {p["port"] for p in o[("Service", "recorder")]["spec"]["ports"]}
        self.assertEqual(ports, {8181, 8333, 5432})

    def test_the_committed_policy_is_the_package_polaris_reads(self):
        with open(os.path.join(HERE, "opa", "policy.rego"), encoding="utf-8") as f:
            self.assertRegex(f.read(), r"(?m)^package polaris\.authz$")


class Cases(unittest.TestCase):
    def test_each_case_has_a_name_a_decision_and_polaris_input(self):
        cases = standins.load_cases(os.path.join(HERE, "opa", "cases.json"))
        self.assertTrue(any(c["allow"] for c in cases))
        self.assertTrue(any(not c["allow"] for c in cases))
        for c in cases:
            with self.subTest(case=c["name"]):
                self.assertEqual(set(c["input"]), {"actor", "action", "resource", "context"})
                self.assertIsInstance(c["allow"], bool)


if __name__ == "__main__":
    unittest.main()
