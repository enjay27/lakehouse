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
ENV = {"NS_STANDINS": "standins", "POLARIS_S3_ACCESS_KEY_ID": "gatekey",
       "POLARIS_S3_SECRET_ACCESS_KEY": "gatesecret",
       "POLARIS_WAREHOUSE_BUCKET": "polaris-warehouse"}
PINS = gate.load_pins(os.path.join(HERE, "pins.json"))


def objects(env=ENV):
    return {(o["kind"], o["metadata"]["name"]): o
            for o in standins.manifests(env, PINS, "package polaris.authz\n")["items"]}


def container(deployment):
    (c,) = deployment["spec"]["template"]["spec"]["containers"]
    return c


class Pins(unittest.TestCase):
    def test_each_stand_in_image_is_pinned_by_digest(self):
        for name in ("seaweedfs", "opa"):
            with self.subTest(name=name):
                self.assertRegex(PINS["standin_images"][name]["image"],
                                 r"^[a-z]+/[a-z]+:\d+\.\d+(\.\d+)?@sha256:[0-9a-f]{64}$")


class Manifests(unittest.TestCase):
    def test_the_namespace_comes_first_and_holds_every_other_object(self):
        items = standins.manifests(ENV, PINS, "")["items"]
        self.assertEqual((items[0]["kind"], items[0]["metadata"]["name"]),
                         ("Namespace", "standins"))
        for o in items[1:]:
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

    def test_seaweedfs_takes_its_credentials_from_the_secret(self):
        o = objects()
        env = {e["name"]: e["valueFrom"]["secretKeyRef"] for e in container(
            o[("Deployment", "seaweedfs")])["env"]}
        self.assertEqual(env["AWS_ACCESS_KEY_ID"], {"name": "seaweedfs-s3", "key": "accessKeyId"})
        self.assertEqual(env["AWS_SECRET_ACCESS_KEY"],
                         {"name": "seaweedfs-s3", "key": "secretAccessKey"})
        data = o[("Secret", "seaweedfs-s3")]["data"]
        self.assertEqual(base64.b64decode(data["accessKeyId"]).decode(), "gatekey")
        self.assertEqual(base64.b64decode(data["secretAccessKey"]).decode(), "gatesecret")

    def test_the_credentials_appear_only_in_the_secret(self):
        o = objects()
        del o[("Secret", "seaweedfs-s3")]
        text = json.dumps(list(o.values()))
        for value in ("gatekey", "gatesecret"):
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

    def test_the_services_expose_s3_on_8333_and_opa_on_8181(self):
        o = objects()
        for name, port in (("seaweedfs", 8333), ("opa", 8181)):
            with self.subTest(name=name):
                svc = o[("Service", name)]["spec"]
                self.assertEqual(svc["ports"][0]["port"], port)
                self.assertEqual(svc["selector"], {"app.kubernetes.io/name": name})

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
