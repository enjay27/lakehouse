"""Tests for check_standins.sh, run here with this machine's curl against local fakes of the S3
and OPA stand-ins. Run: python3 -m unittest discover -s gates"""

import http.server
import json
import os
import subprocess
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "check_standins.sh")
CREDS = {"AWS_ACCESS_KEY_ID": "gatekey", "AWS_SECRET_ACCESS_KEY": "gatesecret"}


class Fakes(http.server.BaseHTTPRequestHandler):
    """S3: a signed request (one with a SigV4 Authorization header) may PUT, GET and DELETE
    objects; an unsigned one gets 403. OPA: POST answers with the package document Polaris reads,
    {"result": {"allow": <allow>, ...}}, from the input (docs/contracts/polaris-opa-input.md)."""

    objects = {}
    s3_broken = False
    bare_boolean = False
    protocol_version = "HTTP/1.1"

    def reply(self, code, body=b""):
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def signed(self):
        return self.headers.get("Authorization", "").startswith("AWS4-HMAC-SHA256 ")

    def body(self):
        return self.rfile.read(int(self.headers.get("Content-Length", 0)))

    def do_PUT(self):
        data = self.body()
        if not self.signed():
            return self.reply(403)
        if not self.s3_broken:
            self.objects[self.path] = data
        self.reply(200)

    def do_GET(self):
        if not self.signed():
            return self.reply(403)
        self.reply(200, self.objects.get(self.path, b"")) if self.path in self.objects \
            else self.reply(404)

    def do_DELETE(self):
        if not self.signed():
            return self.reply(403)
        self.objects.pop(self.path, None)
        self.reply(204)

    def do_POST(self):
        payload = json.loads(self.body())
        allow = payload["input"].get("allow", False)
        if self.bare_boolean:
            return self.reply(200, json.dumps({"result": allow}).encode())
        self.reply(200, json.dumps({"result": {"allow": allow, "other_rule": 1}}).encode())

    def log_message(self, *args):
        pass


class CheckStandins(unittest.TestCase):
    def setUp(self):
        Fakes.objects, Fakes.s3_broken = {}, False
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Fakes)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def run_script(self, *cases):
        args = [self.base, "warehouse", "us-east-1", f"{self.base}/v1/data/polaris/authz"]
        for name, want, allow in cases:
            args += [name, want, json.dumps({"allow": allow})]
        return subprocess.run(["sh", SCRIPT, *args], capture_output=True, text=True,
                              env={"PATH": os.environ["PATH"], **CREDS}, check=False)

    def test_a_working_s3_and_matching_decisions_pass(self):
        result = self.run_script(("root", "true", True), ("stranger", "false", False))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("FAIL", result.stdout)
        for line in ("S3 signed PUT", "S3 signed GET", "S3 anonymous GET refused",
                     "S3 signed DELETE", "OPA root", "OPA stranger"):
            self.assertIn(line, result.stdout)

    def test_the_round_trip_object_is_deleted_afterwards(self):
        self.run_script()
        self.assertEqual(Fakes.objects, {})

    def test_an_object_that_does_not_come_back_fails(self):
        Fakes.s3_broken = True
        result = self.run_script()
        self.assertEqual(result.returncode, 1)
        self.assertIn("FAIL S3 signed GET", result.stdout)

    def test_a_decision_other_than_expected_fails_and_says_which(self):
        result = self.run_script(("stranger", "false", True))
        self.assertEqual(result.returncode, 1)
        self.assertIn("FAIL OPA stranger", result.stdout)

    def test_a_bare_boolean_result_fails_because_polaris_denies_on_it(self):
        Fakes.bare_boolean = True
        try:
            result = self.run_script(("root", "true", True))
        finally:
            Fakes.bare_boolean = False
        self.assertEqual(result.returncode, 1)
        self.assertIn("FAIL OPA root", result.stdout)

    def test_the_credentials_are_never_printed(self):
        result = self.run_script(("root", "true", True))
        for value in CREDS.values():
            self.assertNotIn(value, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
