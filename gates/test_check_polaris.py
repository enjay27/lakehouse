"""Tests for check_polaris.sh, run here with this machine's curl against local fakes of Polaris
replicas. Run: python3 -m unittest discover -s gates"""

import http.server
import json
import os
import subprocess
import threading
import unittest
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "check_polaris.sh")
CREDS = {"ROOT_CLIENT_ID": "rootid0123456789", "ROOT_CLIENT_SECRET": "RootSecretValue0123"}
TOKEN = "token-value-0123456789"


def fake_polaris(signing_key=TOKEN, ready=True):
    """A Polaris replica: readiness on /q/health/ready, a token for root's client credentials,
    and the catalogs for a request with the token this replica's key accepts."""

    class Replica(http.server.BaseHTTPRequestHandler):
        seen = []
        protocol_version = "HTTP/1.1"

        def reply(self, code, body=None):
            data = json.dumps(body).encode() if body is not None else b""
            self.send_response(code)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self.seen.append(("GET", self.path, dict(self.headers)))
            if self.path == "/q/health/ready":
                return self.reply(200 if ready else 503, {"status": "UP" if ready else "DOWN"})
            if self.path == "/api/management/v1/catalogs":
                if self.headers.get("Authorization") == f"Bearer {signing_key}":
                    return self.reply(200, {"catalogs": []})
                return self.reply(401, {"error": {"code": 401}})
            self.reply(404)

        def do_POST(self):
            form = urllib.parse.parse_qs(
                self.rfile.read(int(self.headers["Content-Length"])).decode())
            self.seen.append(("POST", self.path, dict(self.headers)))
            if (self.path == "/api/catalog/v1/oauth/tokens"
                    and form.get("grant_type") == ["client_credentials"]
                    and form.get("client_id") == [CREDS["ROOT_CLIENT_ID"]]
                    and form.get("client_secret") == [CREDS["ROOT_CLIENT_SECRET"]]):
                return self.reply(200, {"access_token": signing_key, "token_type": "bearer",
                                        "expires_in": 3600})
            self.reply(401, {"error": "unauthorized_client"})

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Replica)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, Replica.seen


class CheckPolaris(unittest.TestCase):
    def setUp(self):
        self.servers = []

    def tearDown(self):
        for server in self.servers:
            server.shutdown()
            server.server_close()

    def replica(self, **kwargs):
        server, seen = fake_polaris(**kwargs)
        self.servers.append(server)
        return f"http://127.0.0.1:{server.server_port}", seen

    def run_script(self, api, pods, creds=CREDS, script=None):
        with open(SCRIPT, encoding="utf-8") as f:
            text = script or f.read()
        return subprocess.run(["sh", "-s", "--", api, api, "POLARIS", *pods], input=text,
                              capture_output=True, text=True, check=False,
                              env={"PATH": os.environ["PATH"], **creds})

    def test_two_replicas_with_one_key_pass(self):
        (one, seen_one), (two, seen_two) = self.replica(), self.replica()
        result = self.run_script(one, [one, two])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.count("ok   "), 4, result.stdout)
        for seen in (seen_one, seen_two):
            (listing,) = [h for m, p, h in seen if p == "/api/management/v1/catalogs"]
            self.assertEqual(listing["Polaris-Realm"], "POLARIS")
        (token,) = [h for m, p, h in seen_one if m == "POST"]
        self.assertEqual(token["Polaris-Realm"], "POLARIS")

    def test_a_replica_signing_with_another_key_fails_by_name(self):
        (one, _), (other, _) = self.replica(), self.replica(signing_key="another-key")
        result = self.run_script(one, [one, other])
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"FAIL {other} with root's token: HTTP 401", result.stdout)
        self.assertIn(f"ok   {one} accepts", result.stdout)

    def test_a_wrong_root_secret_gets_no_token(self):
        one, _ = self.replica()
        result = self.run_script(one, [one], creds={**CREDS, "ROOT_CLIENT_SECRET": "wrong"})
        self.assertEqual(result.returncode, 1)
        self.assertIn("FAIL root got no token", result.stdout)

    def test_a_replica_that_is_not_ready_fails(self):
        one, _ = self.replica(ready=False)
        result = self.run_script(one, [one])
        self.assertEqual(result.returncode, 1)
        self.assertIn("FAIL Polaris is not ready", result.stdout)

    def test_no_pod_to_ask_fails(self):
        one, _ = self.replica()
        result = self.run_script(one, [])
        self.assertEqual(result.returncode, 1)
        self.assertIn("FAIL no pod to ask", result.stdout)

    def test_neither_the_credentials_nor_the_token_are_printed(self):
        one, _ = self.replica()
        result = self.run_script(one, [one])
        for secret in (CREDS["ROOT_CLIENT_SECRET"], CREDS["ROOT_CLIENT_ID"], TOKEN):
            self.assertNotIn(secret, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
