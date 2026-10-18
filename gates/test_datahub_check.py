"""Tests for datahub/check.py, with GMS and the frontend faked: one fake answers each request as
the real ones should, and each test breaks one answer.
Run: python3 -m unittest discover -s gates"""

import importlib.util
import io
import json
import os
import unittest
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
# Loaded under its own name: gates/console/check.py is `check` in the same test run.
_SPEC = importlib.util.spec_from_file_location("datahub_check",
                                               os.path.join(HERE, "datahub", "check.py"))
check = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(check)

GMS = "http://datahub-datahub-gms.catalog-datahub.svc.cluster.local:8080"
FRONTEND = "http://datahub-datahub-frontend.catalog-datahub.svc.cluster.local:9002"
NAME = "gate_check_123"
URN = "urn:li:dataset:(urn:li:dataPlatform:hive,gate_check_123,PROD)"
SECRET = "SystemSecret0123456789abcdefABCD"
PASSWORD = "RootPassword0123456789abcdefABCD"
ASPECT = f"{GMS}/aspects/{urllib.parse.quote(URN, safe='')}?aspect=datasetProperties&version=0"


class FakeGms:
    """GMS as it should answer the system client. `broken` names one answer to spoil;
    `indexed_after` is how many searches miss before the dataset is found."""

    def __init__(self, broken=None, indexed_after=0):
        self.broken, self.indexed_after = broken, indexed_after
        self.calls, self.stored, self.searches = [], None, 0

    def __call__(self, method, url, headers, body=None):
        self.calls.append((method, url, headers, body))
        if url == f"{GMS}/health":
            return (503 if self.broken == "health" else 200), {}, b""
        if headers.get("Authorization") != f"Basic __datahub_system:{SECRET}":
            return 401, {}, b"{}"
        if url == f"{GMS}/aspects?action=ingestProposal" and method == "POST":
            if self.broken == "write":
                return 500, {}, b"{}"
            proposal = body["proposal"]
            assert proposal["entityUrn"] == URN and proposal["aspectName"] == "datasetProperties"
            self.stored = json.loads(proposal["aspect"]["value"])
            return 200, {}, json.dumps({"value": URN}).encode()
        if url == ASPECT and method == "GET":
            if self.stored is None or self.broken == "read":
                return 404, {}, b"{}"
            return 200, {}, json.dumps({"version": 0, "aspect": {
                "com.linkedin.dataset.DatasetProperties": self.stored}}).encode()
        if url == f"{GMS}/entities?action=search" and method == "POST":
            self.searches += 1
            found = (self.stored is not None and self.broken != "search"
                     and self.searches > self.indexed_after and body["input"] == NAME)
            entities = [{"entity": URN}] if found else []
            return 200, {}, json.dumps({"value": {"numEntities": len(entities),
                                                  "entities": entities}}).encode()
        if url == f"{GMS}/entities?action=delete" and method == "POST":
            if self.broken == "delete":
                return 500, {}, b"{}"
            if self.broken != "soft-delete":
                self.stored = None
            return 200, {}, b"{}"
        raise AssertionError(f"unexpected {method} {url}")


class FakeFrontend:
    """The frontend with user.props `datahub:<password>`. `broken` names one answer to spoil."""

    def __init__(self, password=PASSWORD, broken=None):
        self.password, self.broken, self.calls = password, broken, []

    def __call__(self, method, url, headers, body=None):
        self.calls.append((method, url, headers, body))
        assert method == "POST" and url == f"{FRONTEND}/logIn", url
        accepted = body["username"] == "datahub" and (body["password"] == self.password
                                                      or self.broken == "image-password")
        if not accepted:
            # datahub-frontend's AuthenticationController.logIn at v1.7.0: a bad request.
            return 400, {}, json.dumps({"message": "Invalid Credentials"}).encode()
        cookies = {} if self.broken == "no-cookie" else {"set-cookie": "PLAY_SESSION=x"}
        return 200, cookies, b"{}"


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def run_gms(http, seconds=30):
    out, clock = io.StringIO(), Clock()
    code = check.run(["gms", GMS, NAME, str(seconds)], SECRET, http=http, out=out,
                     sleep=clock.sleep, clock=clock)
    return code, out.getvalue()


def run_login(http, password=PASSWORD):
    out = io.StringIO()
    return check.run(["login", FRONTEND], password, http=http, out=out), out.getvalue()


class Gms(unittest.TestCase):
    def test_a_round_trip_through_every_store_passes(self):
        http = FakeGms()
        code, out = run_gms(http)
        self.assertEqual(code, 0, out)
        self.assertEqual([line.split()[0] for line in out.splitlines()], ["ok"] * 6)
        self.assertIsNone(http.stored)

    def test_the_search_is_retried_until_the_index_has_it(self):
        http = FakeGms(indexed_after=3)
        code, out = run_gms(http)
        self.assertEqual(code, 0, out)
        self.assertEqual(http.searches, 4)
        self.assertIn("found by search after 4 tries", out)

    def test_a_dataset_never_indexed_fails_within_the_time_limit_and_is_still_deleted(self):
        http = FakeGms(indexed_after=1000)
        code, out = run_gms(http, seconds=30)
        self.assertEqual(code, 1)
        self.assertIn("FAIL", out)
        self.assertIn("not within 30 s", out)
        self.assertLessEqual(http.searches, 30 // check.POLL_SECONDS + 1)
        self.assertIsNone(http.stored)
        self.assertIn(("POST", f"{GMS}/entities?action=delete"),
                      [(c[0], c[1]) for c in http.calls])

    def test_each_broken_answer_fails_the_check(self):
        for broken in ("health", "write", "read", "search", "delete", "soft-delete"):
            with self.subTest(broken=broken):
                code, out = run_gms(FakeGms(broken=broken))
                self.assertEqual(code, 1, out)
                self.assertIn("FAIL", out)

    def test_nothing_is_written_when_gms_is_not_healthy(self):
        http = FakeGms(broken="health")
        run_gms(http)
        self.assertEqual([c[1] for c in http.calls], [f"{GMS}/health"])

    def test_every_call_but_health_is_the_system_clients(self):
        http = FakeGms()
        run_gms(http)
        for method, url, headers, _ in http.calls[1:]:
            with self.subTest(url=url):
                self.assertEqual(headers["Authorization"], f"Basic __datahub_system:{SECRET}")
                self.assertEqual(headers["X-RestLi-Protocol-Version"], "2.0.0")

    def test_the_dataset_is_named_for_the_search(self):
        self.assertEqual(check.dataset_urn(NAME), URN)

    def test_the_secret_is_never_printed(self):
        for broken in (None, "write", "search"):
            with self.subTest(broken=broken):
                _, out = run_gms(FakeGms(broken=broken))
                self.assertNotIn(SECRET, out)


class Login(unittest.TestCase):
    def test_the_root_users_password_signs_in_and_the_images_does_not(self):
        http = FakeFrontend()
        code, out = run_login(http)
        self.assertEqual(code, 0, out)
        self.assertEqual([c[3] for c in http.calls], [
            {"username": "datahub", "password": PASSWORD},
            {"username": "datahub", "password": "datahub"}])

    def test_a_wrong_password_fails(self):
        code, out = run_login(FakeFrontend(password="other"))
        self.assertEqual(code, 1)
        self.assertIn("FAIL", out)

    def test_the_images_password_still_accepted_fails(self):
        code, out = run_login(FakeFrontend(broken="image-password"))
        self.assertEqual(code, 1)
        self.assertIn("FAIL", out)

    def test_another_bad_request_is_not_a_refused_password(self):
        class Broken(FakeFrontend):
            def __call__(self, method, url, headers, body=None):
                if body["password"] == "datahub":
                    return 400, {}, json.dumps({"message": "User name must not be empty."}).encode()
                return super().__call__(method, url, headers, body)
        code, out = run_login(Broken())
        self.assertEqual(code, 1)
        self.assertIn("FAIL", out)

    def test_a_sign_in_without_a_session_cookie_fails(self):
        code, _ = run_login(FakeFrontend(broken="no-cookie"))
        self.assertEqual(code, 1)

    def test_a_secret_holding_the_images_password_fails(self):
        code, out = run_login(FakeFrontend(password="datahub"), password="datahub")
        self.assertEqual(code, 1)
        self.assertIn("holds the image's own password", out)

    def test_the_password_is_never_printed(self):
        for http in (FakeFrontend(), FakeFrontend(password="other")):
            _, out = run_login(http)
            self.assertNotIn(PASSWORD, out)


class Main(unittest.TestCase):
    def test_wrong_arguments_exit_2(self):
        for argv in (["check.py"], ["check.py", "gms", GMS], ["check.py", "login"],
                     ["check.py", "other", GMS]):
            with self.subTest(argv=argv):
                self.assertEqual(check.main(argv, stdin=io.StringIO(SECRET + "\n")), 2)

    def test_no_credential_on_stdin_fails(self):
        self.assertEqual(check.main(["check.py", "login", FRONTEND], stdin=io.StringIO("")), 1)


if __name__ == "__main__":
    unittest.main()
