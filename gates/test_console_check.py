"""Tests for console/check.py, with the console and Polaris faked: one fake answers each
request as the real ones should, and each test breaks one answer.
Run: python3 -m unittest discover -s gates"""

import io
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "console"))
import check  # noqa: E402

ORIGIN = "http://polaris-console.catalog-polaris.svc.cluster.local:8080"
API = "http://polaris.catalog-polaris.svc.cluster.local:8181"
ARGS = [ORIGIN, API, "POLARIS"]
ENV = {"ROOT_CLIENT_ID": "rootid0123456789", "ROOT_CLIENT_SECRET": "RootSecret0123456789"}
TOKENS = f"{API}/api/catalog/v1/oauth/tokens"
CATALOGS = f"{API}/api/management/v1/catalogs"
CONFIG_JS = f"""window.APP_CONFIG = {{
  VITE_POLARIS_API_URL: '{API}',
  VITE_POLARIS_REALM: 'POLARIS',
  VITE_OAUTH_TOKEN_URL: '{TOKENS}',
}};
"""


class FakeHttp:
    """The console and Polaris with CORS for ORIGIN only. `broken` names one answer to spoil."""

    def __init__(self, broken=None, principal=201):
        self.broken, self.principal, self.calls = broken, principal, []

    def cors(self, headers):
        origin = headers.get("Origin")
        if origin == ORIGIN and self.broken != "cors":
            return {"access-control-allow-origin": origin}
        return {}

    def __call__(self, method, url, headers, body=None, form=None):
        self.calls.append((method, url, headers, body, form))
        if url == f"{ORIGIN}/health":
            return (200, {}, b"healthy\n") if self.broken != "health" else (502, {}, b"")
        if url == f"{ORIGIN}/config.js":
            text = CONFIG_JS.replace(API, "http://polaris:8181") \
                if self.broken == "config" else CONFIG_JS
            return 200, {"content-type": "application/javascript"}, text.encode()
        if url == f"{ORIGIN}/":
            return 200, {"content-type": "text/html"}, b"<!doctype html><div id=\"root\">"
        cors = self.cors(headers)
        if method == "OPTIONS":
            return (200 if cors else 403), cors, b""
        if url == TOKENS:
            if form["client_id"] == ENV["ROOT_CLIENT_ID"]:
                return 200, {}, json.dumps({"access_token": "root-token"}).encode()
            if self.broken == "maintainer-token":
                return 401, cors, b"{}"
            return 200, cors, json.dumps({"access_token": "maintainer-token"}).encode()
        if url.startswith(f"{API}/api/management/v1/principals"):
            status = 200 if url.endswith("/rotate") else self.principal
            if status == 409:
                return 409, {}, b"{}"
            creds = {"clientId": "maintainerid0123", "clientSecret": "MaintainerSecret0123"}
            return status, {}, json.dumps({"credentials": creds}).encode()
        if url == CATALOGS and method == "GET":
            if self.broken == "list":
                return 403, cors, b"{}"
            return 200, cors, json.dumps({"catalogs": [{"name": "gate_catalog"}]}).encode()
        if url == CATALOGS and method == "POST":
            return (201 if self.broken == "write" else 403), cors, b"{}"
        raise AssertionError(f"unexpected {method} {url}")


def run(http):
    out = io.StringIO()
    code = check.run(ARGS, ENV, http=http, out=out)
    return code, out.getvalue()


class Check(unittest.TestCase):
    def test_a_console_and_polaris_as_they_should_be_pass(self):
        code, out = run(FakeHttp())
        self.assertEqual(code, 0, out)
        self.assertNotIn("FAIL", out)
        for line in ("console answers /health", "config.js points the browser at",
                     "preflight", "gate_maintainer got a token",
                     "gate_maintainer listed 1 catalogs", "gate_maintainer creating a catalog: refused",
                     "another origin"):
            self.assertIn(line, out)

    def test_each_broken_answer_fails_the_check(self):
        for broken in ("health", "config", "cors", "maintainer-token", "list", "write"):
            with self.subTest(broken=broken):
                code, out = run(FakeHttp(broken=broken))
                self.assertEqual(code, 1, out)
                self.assertIn("FAIL", out)

    def test_every_polaris_request_from_the_browser_carries_the_origin_and_realm(self):
        http = FakeHttp()
        run(http)
        browser = [c for c in http.calls if c[1].startswith(API)
                   and not c[1].startswith(f"{API}/api/management/v1/principals")
                   and (c[4] or {}).get("client_id") != ENV["ROOT_CLIENT_ID"]]
        self.assertGreaterEqual(len(browser), 6)
        for method, url, headers, _, _ in browser:
            with self.subTest(method=method, url=url):
                if headers.get("Origin") != ORIGIN:
                    self.assertEqual(method, "OPTIONS")  # the other origin's preflight
                    continue
                if method == "OPTIONS":
                    self.assertIn("polaris-realm",
                                  headers["Access-Control-Request-Headers"].split(","))
                else:
                    self.assertEqual(headers["Polaris-Realm"], "POLARIS")

    def test_another_origin_allowed_by_polaris_fails(self):
        class Wildcard(FakeHttp):
            def cors(self, headers):
                return {"access-control-allow-origin": headers.get("Origin", "*")}
        code, out = run(Wildcard())
        self.assertEqual(code, 1)
        self.assertIn("FAIL another origin", out)

    def test_an_existing_maintainer_gets_new_credentials(self):
        http = FakeHttp(principal=409)
        code, out = run(http)
        self.assertEqual(code, 0, out)
        self.assertTrue(any(c[1].endswith("/principals/gate_maintainer/rotate")
                            for c in http.calls))

    def test_no_credential_is_ever_printed(self):
        _, out = run(FakeHttp(broken="maintainer-token"))
        for secret in list(ENV.values()) + ["MaintainerSecret0123", "maintainer-token",
                                            "root-token"]:
            self.assertNotIn(secret, out)

    def test_wrong_arguments_exit_2(self):
        self.assertEqual(check.main(["check.py", ORIGIN]), 2)


if __name__ == "__main__":
    unittest.main()
