"""Tests for release.py. Run: python3 -m unittest discover -s install"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import release  # noqa: E402

ENV = {"KUBE_CONTEXT": "kind-lakehouse-gate", "NS_CNPG_SYSTEM": "cnpg-system"}


def deployments(*images):
    return json.dumps({"items": [
        {"spec": {"template": {"spec": {"containers": [{"name": "manager", "image": i}]}}}}
        for i in images]})


class FakeKubectl:
    """Answers kubectl calls by their arguments; records every call."""

    def __init__(self, crd=True, deploys=deployments("ghcr.io/cloudnative-pg/cloudnative-pg:1.30.1")):
        self.crd, self.deploys, self.calls = crd, deploys, []

    def __call__(self, argv):
        self.calls.append(argv)
        if "crd" in argv:
            return (0, "customresourcedefinition.apiextensions.k8s.io/" + release.CNPG_CRD) \
                if self.crd else (1, "NotFound")
        if "deployments" in argv:
            return 0, self.deploys
        raise AssertionError(f"unexpected call {argv}")


class OperatorVersions(unittest.TestCase):
    def test_tag_is_read(self):
        self.assertEqual(release.operator_versions(
            deployments("ghcr.io/cloudnative-pg/cloudnative-pg:1.30.1")), ["1.30.1"])

    def test_tag_with_a_digest(self):
        self.assertEqual(release.operator_versions(deployments(
            "ghcr.io/cloudnative-pg/cloudnative-pg:1.30.1@sha256:" + "a" * 64)), ["1.30.1"])

    def test_a_mirror_registry_counts(self):
        self.assertEqual(release.operator_versions(
            deployments("registry.example.invalid/mirror/cloudnative-pg:1.29.3")), ["1.29.3"])

    def test_digest_only_or_other_images_give_nothing(self):
        self.assertEqual(release.operator_versions(deployments(
            "ghcr.io/cloudnative-pg/cloudnative-pg@sha256:" + "a" * 64,
            "ghcr.io/cloudnative-pg/postgresql:16")), [])


class Preconditions(unittest.TestCase):
    def check(self, kubectl, env=ENV, supported=("1.30",)):
        return release.check_preconditions(env, kubectl, list(supported))

    def test_present_and_supported_passes(self):
        problems, notes = self.check(FakeKubectl())
        self.assertEqual(problems, [])
        self.assertIn("1.30.1", "\n".join(notes))

    def test_every_call_names_the_context(self):
        kubectl = FakeKubectl()
        self.check(kubectl)
        for argv in kubectl.calls:
            self.assertEqual(argv[:3], ["kubectl", "--context", "kind-lakehouse-gate"])

    def test_the_deployments_are_read_in_the_operator_namespace(self):
        kubectl = FakeKubectl()
        self.check(kubectl)
        call = next(a for a in kubectl.calls if "deployments" in a)
        self.assertIn("-n", call)
        self.assertEqual(call[call.index("-n") + 1], "cnpg-system")

    def test_an_empty_context_stops_before_any_call(self):
        kubectl = FakeKubectl()
        problems, _ = self.check(kubectl, env={**ENV, "KUBE_CONTEXT": ""})
        self.assertTrue(problems)
        self.assertEqual(kubectl.calls, [])

    def test_no_crd_stops(self):
        problems, _ = self.check(FakeKubectl(crd=False))
        self.assertIn(release.CNPG_CRD, "\n".join(problems))

    def test_no_operator_stops(self):
        problems, _ = self.check(FakeKubectl(deploys=deployments()))
        self.assertTrue(problems)

    def test_an_unsupported_version_stops(self):
        problems, _ = self.check(FakeKubectl(
            deploys=deployments("ghcr.io/cloudnative-pg/cloudnative-pg:1.31.0")))
        self.assertIn("1.31.0", "\n".join(problems))

    def test_a_minor_prefix_does_not_match_a_longer_minor(self):
        problems, _ = self.check(FakeKubectl(
            deploys=deployments("ghcr.io/cloudnative-pg/cloudnative-pg:1.300.0")))
        self.assertTrue(problems)


POLARIS_ENV = {**ENV, "NS_CATALOG_POLARIS": "catalog-polaris"}


class FakeCalls:
    """Answers step 1's kubectl and helm calls; records every call."""

    def __init__(self, secret=True, helm=(0, ""), wait=(0, "")):
        self.secret, self.helm, self.wait, self.calls = secret, helm, wait, []

    def __call__(self, argv):
        self.calls.append(argv)
        if argv[0] == "helm":
            return self.helm
        if "secret" in argv:
            return (0, "secret/polaris-db") if self.secret else (1, "NotFound")
        if "wait" in argv:
            return self.wait
        raise AssertionError(f"unexpected call {argv}")


class PolarisPostgres(unittest.TestCase):
    def step(self, calls, env=POLARIS_ENV, dry_run=False):
        return release.polaris_postgres(env, calls, dry_run=dry_run)

    def test_installs_the_chart_then_waits_for_the_cluster(self):
        calls = FakeCalls()
        problems, notes = self.step(calls)
        self.assertEqual(problems, [])
        self.assertEqual([c[0] for c in calls.calls], ["kubectl", "helm", "kubectl"])
        helm = calls.calls[1]
        self.assertEqual(helm[:3], ["helm", "--kube-context", "kind-lakehouse-gate"])
        self.assertEqual(helm[helm.index("-n") + 1], "catalog-polaris")
        self.assertEqual(helm[helm.index("upgrade"):helm.index("upgrade") + 4],
                         ["upgrade", "--install", "polaris-postgres", release.POLARIS_PG_CHART])
        self.assertNotIn("--dry-run=server", helm)
        wait = calls.calls[2]
        self.assertIn("--for=condition=Ready", wait)
        self.assertIn("cluster/polaris-pg", wait)
        self.assertIn("polaris-pg", "\n".join(notes))

    def test_every_call_names_the_context_and_the_namespace(self):
        calls = FakeCalls()
        self.step(calls)
        for argv in calls.calls:
            flag = "--kube-context" if argv[0] == "helm" else "--context"
            self.assertEqual(argv[1:3], [flag, "kind-lakehouse-gate"])
            self.assertEqual(argv[argv.index("-n") + 1], "catalog-polaris")

    def test_the_chart_is_this_repositorys(self):
        self.assertTrue(os.path.isfile(os.path.join(release.POLARIS_PG_CHART, "Chart.yaml")))

    def test_dry_run_is_server_side_and_does_not_wait(self):
        calls = FakeCalls()
        problems, notes = self.step(calls, dry_run=True)
        self.assertEqual(problems, [])
        self.assertIn("--dry-run=server", calls.calls[1])
        self.assertFalse(any("wait" in c for c in calls.calls))
        self.assertIn("dry-run", "\n".join(notes))

    def test_an_empty_namespace_stops_before_any_call(self):
        calls = FakeCalls()
        problems, _ = self.step(calls, env={**POLARIS_ENV, "NS_CATALOG_POLARIS": ""})
        self.assertIn("NS_CATALOG_POLARIS", "\n".join(problems))
        self.assertEqual(calls.calls, [])

    def test_a_missing_secret_stops_before_helm_and_names_init_env(self):
        calls = FakeCalls(secret=False)
        problems, _ = self.step(calls)
        self.assertIn("init_env.py --apply-secrets", "\n".join(problems))
        self.assertFalse(any(c[0] == "helm" for c in calls.calls))

    def test_a_failed_helm_stops_with_its_error(self):
        problems, _ = self.step(FakeCalls(helm=(1, "admission webhook denied")))
        self.assertIn("admission webhook denied", "\n".join(problems))

    def test_a_cluster_that_never_gets_ready_stops(self):
        problems, _ = self.step(FakeCalls(wait=(1, "timed out")))
        self.assertIn("polaris-pg", "\n".join(problems))
        self.assertIn("timed out", "\n".join(problems))


class Steps(unittest.TestCase):
    def test_step_0_problems_stop_before_step_1(self):
        calls = []

        def run(argv):
            calls.append(argv)
            return 1, "NotFound"
        problems, _ = release.install(ENDPOINT_ENV, run, ["1.30"], dry_run=False)
        self.assertIn(release.CNPG_CRD, "\n".join(problems))
        self.assertTrue(all(c[0] == "kubectl" and "crd" in c for c in calls))

    def test_every_step_passes_in_order(self):
        step0, step1, endpoints = FakeKubectl(), FakeCalls(), FakeRun()

        def run(argv, stdin=None):
            if "crd" in argv or "deployments" in argv:
                return step0(argv)
            return endpoints(argv, stdin) if "run" in argv else step1(argv)
        problems, notes = release.install(ENDPOINT_ENV, run, ["1.30"], dry_run=False)
        self.assertEqual(problems, [])
        self.assertEqual([n.split(":")[0] for n in notes], ["Step 0", "Step 1", "Step 1"])
        self.assertIn("answer", notes[2])

    def test_endpoints_that_do_not_answer_stop_the_install(self):
        step0, step1 = FakeKubectl(), FakeCalls()

        def run(argv, stdin=None):
            if "crd" in argv or "deployments" in argv:
                return step0(argv)
            return (1, "http://s3.example:8333 did not answer") if "run" in argv \
                else step1(argv)
        problems, _ = release.install(ENDPOINT_ENV, run, ["1.30"], dry_run=False)
        self.assertIn("did not answer", "\n".join(problems))

    def test_a_dry_run_checks_the_endpoints_too(self):
        step0, step1, endpoints = FakeKubectl(), FakeCalls(), FakeRun()

        def run(argv, stdin=None):
            if "crd" in argv or "deployments" in argv:
                return step0(argv)
            return endpoints(argv, stdin) if "run" in argv else step1(argv)
        problems, _ = release.install(ENDPOINT_ENV, run, ["1.30"], dry_run=True)
        self.assertEqual(problems, [])
        self.assertEqual(len(endpoints.calls), 1)


ENDPOINT_ENV = {**POLARIS_ENV, "S3_ENDPOINT": "http://s3.example:8333",
                "POLARIS_OPA_URL": "http://opa.example:8181/v1/data/polaris/allow"}
CURL = "curlimages/curl:8.0.0@sha256:" + "b" * 64


class FakeRun:
    """Answers the endpoint check's `kubectl run`; records every call."""

    def __init__(self, answer=(0, "answered")):
        self.answer, self.calls = answer, []

    def __call__(self, argv, stdin=None):
        self.calls.append(argv)
        self.stdin = stdin
        return self.answer


class Endpoints(unittest.TestCase):
    """ADR-029: before Polaris, the S3 and policy endpoints answer, seen from its namespace."""

    def check(self, calls, env=ENDPOINT_ENV):
        return release.endpoints_answer(env, calls, image=CURL, name="endpoint-check-test")

    def test_one_short_lived_pod_in_polaris_namespace_asks_both_endpoints(self):
        calls = FakeRun()
        problems, notes = self.check(calls)
        self.assertEqual(problems, [])
        (argv,) = calls.calls
        self.assertEqual(argv[:5], ["kubectl", "--context", "kind-lakehouse-gate",
                                    "-n", "catalog-polaris"])
        self.assertEqual(argv[5:7], ["run", "endpoint-check-test"])
        for flag in ("--rm", "-i", "--restart=Never", f"--image={CURL}"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[-2:], ["http://s3.example:8333",
                                     "http://opa.example:8181/v1/data/polaris/allow"])
        self.assertIn("answer", "\n".join(notes))

    def test_the_script_goes_on_stdin_and_the_endpoints_are_its_arguments(self):
        calls = FakeRun()
        self.check(calls)
        argv = calls.calls[0]
        self.assertEqual(argv[-5:-2], ["sh", "-s", "--"])
        self.assertEqual(calls.stdin, release.ENDPOINT_CHECK)
        self.assertNotIn("example", calls.stdin)

    def test_an_empty_endpoint_stops_before_any_call(self):
        for name in ("S3_ENDPOINT", "POLARIS_OPA_URL"):
            with self.subTest(name=name):
                calls = FakeRun()
                problems, _ = self.check(calls, env={**ENDPOINT_ENV, name: ""})
                self.assertIn(name, "\n".join(problems))
                self.assertEqual(calls.calls, [])

    def test_no_answer_stops_with_what_the_pod_said(self):
        problems, _ = self.check(FakeRun(answer=(1, "http://s3.example:8333 did not answer")))
        self.assertIn("did not answer", "\n".join(problems))

    def test_the_curl_image_is_pinned_by_digest(self):
        self.assertRegex(release.images()["curl"],
                         r"^curlimages/curl:\d+\.\d+\.\d+@sha256:[0-9a-f]{64}$")


class EndpointScript(unittest.TestCase):
    """The pod's script, run here with this machine's curl against a local server."""

    def run_script(self, *urls):
        return subprocess.run(["sh", "-s", "--", *urls], input=release.ENDPOINT_CHECK,
                              capture_output=True, text=True, check=False)

    def test_any_http_status_is_an_answer_and_no_connection_is_not(self):
        import http.server
        import threading

        class Forbidden(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(403)
                self.end_headers()

            def log_message(self, *args):
                pass
        server = http.server.HTTPServer(("127.0.0.1", 0), Forbidden)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            up = f"http://127.0.0.1:{server.server_port}/"
            result = self.run_script(up)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("HTTP 403", result.stdout)
            closed = "http://127.0.0.1:9/"
            result = self.run_script(up, closed)
            self.assertEqual(result.returncode, 1)
            self.assertIn(f"{closed} did not answer", result.stdout)
        finally:
            server.shutdown()
            server.server_close()


class LoadEnv(unittest.TestCase):
    def test_env_file_then_environment_wins(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, ".env"), "w", encoding="utf-8") as f:
                f.write("KUBE_CONTEXT=from-file\nNS_CNPG_SYSTEM=cnpg-system\n")
            env = release.load_env(d, {"KUBE_CONTEXT": "from-environ"})
        self.assertEqual(env["KUBE_CONTEXT"], "from-environ")
        self.assertEqual(env["NS_CNPG_SYSTEM"], "cnpg-system")

    def test_no_env_file_uses_the_environment(self):
        with tempfile.TemporaryDirectory() as d:
            env = release.load_env(d, dict(ENV))
        self.assertEqual(env, ENV)


class Main(unittest.TestCase):
    def test_stops_with_exit_1_when_kube_context_is_empty(self):
        with tempfile.TemporaryDirectory() as d:
            result = subprocess.run(
                [sys.executable, release.__file__], cwd=d, capture_output=True, text=True,
                env={"PATH": os.environ.get("PATH", ""), "KUBE_CONTEXT": ""}, check=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn("KUBE_CONTEXT", result.stdout + result.stderr)


    def test_an_unknown_option_exits_2(self):
        result = subprocess.run([sys.executable, release.__file__, "--upgrade"],
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
