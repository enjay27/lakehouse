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
        step0, pg, endpoints = FakeKubectl(), FakeCalls(), FakeRun()
        boot, pol, claim = FakeBootstrap(), FakePolaris(), FakeClaim()

        def run(argv, stdin=None):
            if "crd" in argv or "deployments" in argv:
                return step0(argv)
            if "endpoint-check" in " ".join(argv):
                return endpoints(argv, stdin)
            if "storageclass" in argv or "apply" in argv:
                return claim(argv, stdin)
            if "polaris-root" in argv or "run" in argv:
                return boot(argv, stdin)
            if release.BATCH_CHART in argv or "cronjob/polaris-log-batch" in argv:
                return batch(argv, stdin)
            if "polaris-s3" in argv or "rollout" in argv or release.POLARIS_CHART in argv:
                return pol(argv, stdin)
            return pg(argv)
        batch = FakeBatch()
        problems, notes = release.install({**BOOTSTRAP_ENV, **LOGS_ENV}, run, ["1.30"],
                                          dry_run=False)
        self.assertEqual(problems, [])
        self.assertEqual([n.split(":")[0] for n in notes],
                         ["Step 0"] + ["Step 1"] * 5 + ["Step 2"] * 2)
        self.assertIn("polaris-pg", notes[1])
        self.assertIn("answer", notes[2])
        self.assertIn("bootstrapped", notes[3])
        self.assertIn("polaris-logs", notes[4])
        self.assertIn("deployment/polaris", notes[5])
        self.assertIn("cronjob/polaris-log-batch", notes[6])
        self.assertIn("console is not enabled", notes[7])

    def test_a_failed_batch_stops_the_install_before_the_console(self):
        step0, pg, endpoints = FakeKubectl(), FakeCalls(), FakeRun()
        boot, pol, claim = FakeBootstrap(), FakePolaris(), FakeClaim()
        batch, console = FakeBatch(helm=(1, "image tag invalid")), FakeConsole()

        def run(argv, stdin=None):
            if "crd" in argv or "deployments" in argv:
                return step0(argv)
            if "endpoint-check" in " ".join(argv):
                return endpoints(argv, stdin)
            if "storageclass" in argv or "apply" in argv:
                return claim(argv, stdin)
            if "polaris-root" in argv or "run" in argv:
                return boot(argv, stdin)
            if release.BATCH_CHART in argv or "cronjob/polaris-log-batch" in argv:
                return batch(argv, stdin)
            if release.CONSOLE_CHART in argv or "deployment/polaris-console" in argv:
                return console(argv, stdin)
            if "polaris-s3" in argv or "rollout" in argv or release.POLARIS_CHART in argv:
                return pol(argv, stdin)
            return pg(argv)
        problems, notes = release.install({**BOOTSTRAP_ENV, **LOGS_ENV, **CONSOLE_ENV}, run,
                                          ["1.30"], dry_run=False)
        self.assertIn("image tag invalid", "\n".join(problems))
        self.assertEqual(console.calls, [])
        self.assertIn("deployment/polaris", notes[-1])

    def test_a_missing_storage_class_stops_before_polaris(self):
        step0, pg, endpoints = FakeKubectl(), FakeCalls(), FakeRun()
        boot, pol = FakeBootstrap(), FakePolaris()
        claim = FakeClaim(storage_class=(1, "NotFound"))

        def run(argv, stdin=None):
            if "crd" in argv or "deployments" in argv:
                return step0(argv)
            if "endpoint-check" in " ".join(argv):
                return endpoints(argv, stdin)
            if "storageclass" in argv or "apply" in argv:
                return claim(argv, stdin)
            if "polaris-root" in argv or "run" in argv:
                return boot(argv, stdin)
            if "polaris-s3" in argv or "rollout" in argv or release.POLARIS_CHART in argv:
                return pol(argv, stdin)
            return pg(argv)
        problems, _ = release.install({**BOOTSTRAP_ENV, **LOGS_ENV}, run, ["1.30"],
                                      dry_run=False)
        self.assertIn("ReadWriteMany", "\n".join(problems))
        self.assertEqual(pol.calls, [])

    def test_a_console_that_fails_is_reported_and_the_install_still_passes(self):
        step0, pg, endpoints = FakeKubectl(), FakeCalls(), FakeRun()
        boot, pol, claim = FakeBootstrap(), FakePolaris(), FakeClaim()
        console = FakeConsole(rollout=(1, "ErrImageNeverPull"))

        def run(argv, stdin=None):
            if "crd" in argv or "deployments" in argv:
                return step0(argv)
            if "endpoint-check" in " ".join(argv):
                return endpoints(argv, stdin)
            if "storageclass" in argv or "apply" in argv:
                return claim(argv, stdin)
            if "polaris-root" in argv or "run" in argv:
                return boot(argv, stdin)
            if release.BATCH_CHART in argv or "cronjob/polaris-log-batch" in argv:
                return FakeBatch()(argv, stdin)
            if release.CONSOLE_CHART in argv or "deployment/polaris-console" in argv:
                return console(argv, stdin)
            if "polaris-s3" in argv or "rollout" in argv or release.POLARIS_CHART in argv:
                return pol(argv, stdin)
            return pg(argv)
        problems, notes = release.install({**BOOTSTRAP_ENV, **LOGS_ENV, **CONSOLE_ENV}, run,
                                          ["1.30"], dry_run=False)
        self.assertEqual(problems, [])
        self.assertIn("ErrImageNeverPull", notes[-1])
        self.assertIn("not blocking", notes[-1])

    def test_a_failed_bootstrap_stops_before_polaris(self):
        step0, pg, endpoints = FakeKubectl(), FakeCalls(), FakeRun()
        boot, pol = FakeBootstrap(answer=(1, "connection refused")), FakePolaris()
        claim = FakeClaim()

        def run(argv, stdin=None):
            if "crd" in argv or "deployments" in argv:
                return step0(argv)
            if "endpoint-check" in " ".join(argv):
                return endpoints(argv, stdin)
            if "storageclass" in argv or "apply" in argv:
                return claim(argv, stdin)
            if "polaris-root" in argv or "run" in argv:
                return boot(argv, stdin)
            if "polaris-s3" in argv or "rollout" in argv or release.POLARIS_CHART in argv:
                return pol(argv, stdin)
            return pg(argv)
        problems, _ = release.install(BOOTSTRAP_ENV, run, ["1.30"], dry_run=False)
        self.assertIn("connection refused", "\n".join(problems))
        self.assertEqual(pol.calls, [])

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
        step0, step1, endpoints, claim = FakeKubectl(), FakeCalls(), FakeRun(), FakeClaim()

        def run(argv, stdin=None):
            if "crd" in argv or "deployments" in argv:
                return step0(argv)
            if "storageclass" in argv or "apply" in argv:
                return claim(argv, stdin)
            return endpoints(argv, stdin) if "run" in argv else step1(argv)
        problems, _ = release.install({**ENDPOINT_ENV, **LOGS_ENV}, run, ["1.30"], dry_run=True)
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


BOOTSTRAP_ENV = {**ENDPOINT_ENV, "POLARIS_DB_HOST": "",
                 "POLARIS_ROOT_CLIENT_ID": "rootid0123456789",
                 "POLARIS_ROOT_CLIENT_SECRET": "RootSecretValue0123456789abcdefg",
                 "POLARIS_DB_PASSWORD": "DbPasswordValue0123456789abcdefgh"}
ADMIN = "apache/polaris-admin-tool:1.6.0@sha256:" + "c" * 64


class FakeBootstrap:
    """Answers the bootstrap's Secret check and its `kubectl run`; records every call."""

    def __init__(self, secret=True, answer=(0, "Realm 'POLARIS' successfully bootstrapped.")):
        self.secret, self.answer, self.calls, self.stdin = secret, answer, [], None

    def __call__(self, argv, stdin=None):
        self.calls.append(argv)
        if "secret" in argv:
            return (0, "secret/polaris-root") if self.secret else (1, "NotFound")
        if "run" in argv:
            self.stdin = stdin
            return self.answer
        raise AssertionError(f"unexpected call {argv}")


class Bootstrap(unittest.TestCase):
    """ADR-026: the realm and root principal, by Polaris's admin tool, once."""

    def step(self, calls, env=BOOTSTRAP_ENV, dry_run=False):
        return release.bootstrap(env, calls, image=ADMIN, name="polaris-bootstrap-test",
                                 dry_run=dry_run)

    def run_call(self, calls):
        (argv,) = [c for c in calls.calls if "run" in c]
        return argv

    def overrides(self, argv):
        flag = next(a for a in argv if a.startswith("--overrides="))
        return json.loads(flag.split("=", 1)[1])

    def test_one_short_lived_admin_tool_pod_in_polaris_namespace(self):
        calls = FakeBootstrap()
        problems, notes = self.step(calls)
        self.assertEqual(problems, [])
        argv = self.run_call(calls)
        self.assertEqual(argv[:5], ["kubectl", "--context", "kind-lakehouse-gate",
                                    "-n", "catalog-polaris"])
        self.assertEqual(argv[5:7], ["run", "polaris-bootstrap-test"])
        for flag in ("--rm", "-i", "--restart=Never", f"--image={ADMIN}",
                     "--override-type=strategic"):
            self.assertIn(flag, argv)
        self.assertIn("bootstrapped", "\n".join(notes))

    def test_it_bootstraps_the_realm_with_the_root_credential_from_its_secret(self):
        calls = FakeBootstrap()
        self.step(calls)
        argv = self.run_call(calls)
        tail = argv[argv.index("--") + 1:]
        self.assertEqual(tail[-5:], ["bootstrap", "-r", "POLARIS", "-c",
                                     "POLARIS,$(POLARIS_ROOT_CLIENT_ID),"
                                     "$(POLARIS_ROOT_CLIENT_SECRET)"])
        (container,) = self.overrides(argv)["spec"]["containers"]
        self.assertEqual((container["name"], container["image"], container["stdin"],
                          container["stdinOnce"]), ("polaris-bootstrap-test", ADMIN, True, True))
        env = {e["name"]: e for e in container["env"]}
        self.assertEqual(env["POLARIS_ROOT_CLIENT_ID"]["valueFrom"]["secretKeyRef"],
                         {"name": "polaris-root", "key": "clientId"})
        self.assertEqual(env["POLARIS_ROOT_CLIENT_SECRET"]["valueFrom"]["secretKeyRef"],
                         {"name": "polaris-root", "key": "clientSecret"})

    def test_it_reaches_the_database_as_polaris_does(self):
        calls = FakeBootstrap()
        self.step(calls, env={**BOOTSTRAP_ENV, "POLARIS_DB_HOST": "recorder.standins.svc"})
        (container,) = self.overrides(self.run_call(calls))["spec"]["containers"]
        env = {e["name"]: e for e in container["env"]}
        self.assertEqual(env["QUARKUS_DATASOURCE_JDBC_URL"]["value"],
                         "jdbc:postgresql://recorder.standins.svc:5432/polaris?sslmode=require")
        self.assertEqual(env["QUARKUS_DATASOURCE_USERNAME"]["valueFrom"]["secretKeyRef"],
                         {"name": "polaris-db", "key": "username"})
        self.assertEqual(env["QUARKUS_DATASOURCE_PASSWORD"]["valueFrom"]["secretKeyRef"],
                         {"name": "polaris-db", "key": "password"})

    def test_no_credential_is_in_the_command_line(self):
        calls = FakeBootstrap()
        self.step(calls)
        text = " ".join(self.run_call(calls))
        for var in ("POLARIS_ROOT_CLIENT_ID", "POLARIS_ROOT_CLIENT_SECRET", "POLARIS_DB_PASSWORD"):
            self.assertNotIn(BOOTSTRAP_ENV[var], text)

    def test_the_tool_starts_only_once_kubectl_has_attached(self):
        calls = FakeBootstrap()
        self.step(calls)
        self.assertIsNotNone(calls.stdin)
        argv = self.run_call(calls)
        tail = argv[argv.index("--") + 1:]
        self.assertEqual(tail[:3], ["sh", "-c", release.BOOTSTRAP_WRAPPER])

    def test_an_already_bootstrapped_realm_is_kept(self):
        answer = (3, "java.lang.IllegalArgumentException: It appears this metastore manager "
                     "has already been bootstrapped. To continue bootstrapping, please first "
                     "purge the metastore with the `purge` command.")
        problems, notes = self.step(FakeBootstrap(answer=answer))
        self.assertEqual(problems, [])
        self.assertIn("already bootstrapped", "\n".join(notes))

    def test_any_other_failure_stops_and_never_prints_a_credential(self):
        answer = (3, "Invalid credentials format: POLARIS,rootid0123456789,"
                     "RootSecretValue0123456789abcdefg\nBootstrap encountered errors")
        problems, _ = self.step(FakeBootstrap(answer=answer))
        text = "\n".join(problems)
        self.assertIn("Bootstrap encountered errors", text)
        self.assertNotIn("RootSecretValue0123456789abcdefg", text)
        self.assertNotIn("rootid0123456789", text)

    def test_a_missing_root_secret_stops_before_the_pod_and_names_init_env(self):
        calls = FakeBootstrap(secret=False)
        problems, _ = self.step(calls)
        self.assertIn("polaris-root", "\n".join(problems))
        self.assertIn("init_env.py --apply-secrets", "\n".join(problems))
        self.assertFalse(any("run" in c for c in calls.calls))

    def test_a_dry_run_makes_no_call(self):
        calls = FakeBootstrap()
        problems, notes = self.step(calls, dry_run=True)
        self.assertEqual((problems, calls.calls), ([], []))
        self.assertIn("dry-run", "\n".join(notes))

    def test_the_admin_tool_is_pinned_by_digest(self):
        self.assertRegex(release.images()["polaris_admin_tool"],
                         r"^apache/polaris-admin-tool:\d+\.\d+\.\d+@sha256:[0-9a-f]{64}$")


class BootstrapWrapper(unittest.TestCase):
    """The wrapper, run here with sh: the tool starts after stdin ends, with its arguments, and
    all its output goes to stderr, which is what a failed `kubectl run` returns."""

    def test_the_entrypoint_gets_the_arguments_after_stdin_ends(self):
        with tempfile.TemporaryDirectory() as d:
            entrypoint = os.path.join(d, "run-java.sh")
            with open(entrypoint, "w", encoding="utf-8") as f:
                f.write('#!/bin/sh\necho "args: $*"\nexit 3\n')
            os.chmod(entrypoint, 0o755)
            wrapper = release.BOOTSTRAP_WRAPPER.replace(release.ADMIN_TOOL_ENTRYPOINT, entrypoint)
            result = subprocess.run(["sh", "-c", wrapper, "admin-tool", "bootstrap", "-r", "R"],
                                    input="\n", capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 3)
        self.assertEqual((result.stdout, result.stderr), ("", "args: bootstrap -r R\n"))


class FakePolaris:
    """Answers the Polaris step's Secret check, helm and the rollout wait; records every call."""

    def __init__(self, secrets_found=True, helm=(0, ""), rollout=(0, "")):
        self.secrets_found, self.helm, self.rollout = secrets_found, helm, rollout
        self.calls, self.values = [], []

    def __call__(self, argv, stdin=None):
        self.calls.append(argv)
        if argv[0] == "helm":
            files = [argv[i + 1] for i, a in enumerate(argv) if a == "-f"]
            with open(files[-1], encoding="utf-8") as f:
                self.values.append(json.load(f))
            return self.helm
        if "secret" in argv:
            return (0, "") if self.secrets_found else (1, "NotFound")
        if "rollout" in argv:
            return self.rollout
        raise AssertionError(f"unexpected call {argv}")


class Polaris(unittest.TestCase):
    def step(self, calls, env=BOOTSTRAP_ENV, dry_run=False):
        return release.polaris(env, calls, dry_run=dry_run)

    def test_installs_the_chart_with_both_values_files_then_waits_for_the_rollout(self):
        calls = FakePolaris()
        problems, notes = self.step(calls)
        self.assertEqual(problems, [])
        self.assertEqual([c[0] for c in calls.calls], ["kubectl", "helm", "kubectl"])
        helm = calls.calls[1]
        self.assertEqual(helm[:3], ["helm", "--kube-context", "kind-lakehouse-gate"])
        self.assertEqual(helm[helm.index("-n") + 1], "catalog-polaris")
        self.assertEqual(helm[helm.index("upgrade"):helm.index("upgrade") + 4],
                         ["upgrade", "--install", "polaris", release.POLARIS_CHART])
        files = [helm[i + 1] for i, a in enumerate(helm) if a == "-f"]
        self.assertEqual(files[0], release.POLARIS_VALUES)
        self.assertEqual(calls.values, [release.polaris_values(BOOTSTRAP_ENV)])
        self.assertFalse(os.path.exists(files[1]), "the generated values file is left behind")
        rollout = calls.calls[2]
        self.assertEqual(rollout[rollout.index("rollout"):rollout.index("rollout") + 3],
                         ["rollout", "status", "deployment/polaris"])
        self.assertIn("polaris", "\n".join(notes))

    def test_every_call_names_the_context_and_the_namespace(self):
        calls = FakePolaris()
        self.step(calls)
        for argv in calls.calls:
            flag = "--kube-context" if argv[0] == "helm" else "--context"
            self.assertEqual(argv[1:3], [flag, "kind-lakehouse-gate"])
            self.assertEqual(argv[argv.index("-n") + 1], "catalog-polaris")

    def test_the_secrets_the_chart_reads_are_checked_first(self):
        calls = FakePolaris()
        self.step(calls)
        check = calls.calls[0]
        for name in ("polaris-db", "polaris-s3", "polaris-token-signing"):
            self.assertIn(name, check)

    def test_a_missing_secret_stops_before_helm_and_names_init_env(self):
        calls = FakePolaris(secrets_found=False)
        problems, _ = self.step(calls)
        self.assertIn("init_env.py --apply-secrets", "\n".join(problems))
        self.assertFalse(any(c[0] == "helm" for c in calls.calls))

    def test_dry_run_is_server_side_and_does_not_wait(self):
        calls = FakePolaris()
        problems, notes = self.step(calls, dry_run=True)
        self.assertEqual(problems, [])
        self.assertIn("--dry-run=server", calls.calls[1])
        self.assertFalse(any("rollout" in c for c in calls.calls))
        self.assertIn("dry-run", "\n".join(notes))

    def test_a_failed_helm_stops_with_its_error(self):
        problems, _ = self.step(FakePolaris(helm=(1, "admission webhook denied")))
        self.assertIn("admission webhook denied", "\n".join(problems))

    def test_a_rollout_that_never_finishes_stops(self):
        problems, _ = self.step(FakePolaris(rollout=(1, "timed out waiting")))
        self.assertIn("timed out waiting", "\n".join(problems))


LOGS_ENV = {"POLARIS_LOGS_STORAGE_CLASS": "shared", "POLARIS_LOGS_SIZE": "1Gi"}


class FakeClaim:
    """Answers the claim step's storage class check and its apply; records every call."""

    def __init__(self, storage_class=(0, "storageclass.storage.k8s.io/shared"),
                 apply=(0, "persistentvolumeclaim/polaris-logs created")):
        self.storage_class, self.apply, self.calls, self.stdin = storage_class, apply, [], []

    def __call__(self, argv, stdin=None):
        self.calls.append(argv)
        self.stdin.append(stdin)
        if "storageclass" in argv:
            return self.storage_class
        if "apply" in argv:
            return self.apply
        raise AssertionError(f"unexpected call {argv}")


class LogsClaim(unittest.TestCase):
    """Step 1, before Polaris: the claim polaris-logs that Polaris and the batch share
    (ADR-036)."""

    def step(self, calls, env=None, dry_run=False):
        return release.logs_claim({**BOOTSTRAP_ENV, **LOGS_ENV, **(env or {})}, calls,
                                  dry_run=dry_run)

    def test_the_storage_class_is_checked_then_the_claim_applied(self):
        calls = FakeClaim()
        problems, notes = self.step(calls)
        self.assertEqual(problems, [])
        self.assertEqual(calls.calls[0][calls.calls[0].index("get"):],
                         ["get", "storageclass", "shared", "-o", "name"])
        apply = calls.calls[1]
        self.assertEqual(apply[apply.index("apply"):], ["apply", "-f", "-"])
        self.assertIn("polaris-logs", notes[0])
        self.assertIn("shared", notes[0])

    def test_the_claim_is_read_write_many_with_the_class_and_size_from_env(self):
        calls = FakeClaim()
        self.step(calls)
        claim = json.loads(calls.stdin[1])
        self.assertEqual((claim["kind"], claim["metadata"]["name"]),
                         ("PersistentVolumeClaim", release.POLARIS_LOGS))
        self.assertEqual(claim["metadata"]["namespace"], "catalog-polaris")
        self.assertEqual(claim["spec"]["accessModes"], ["ReadWriteMany"])
        self.assertEqual(claim["spec"]["storageClassName"], "shared")
        self.assertEqual(claim["spec"]["resources"], {"requests": {"storage": "1Gi"}})

    def test_the_claim_belongs_to_no_helm_release(self):
        calls = FakeClaim()
        self.step(calls)
        claim = json.loads(calls.stdin[1])
        self.assertFalse(any(k.startswith("meta.helm.sh")
                             for k in claim["metadata"].get("annotations", {})))
        self.assertFalse(any(c[0] == "helm" for c in calls.calls))

    def test_every_call_names_the_context_and_the_namespace(self):
        calls = FakeClaim()
        self.step(calls)
        for argv in calls.calls:
            self.assertEqual(argv[1:3], ["--context", "kind-lakehouse-gate"])
            self.assertEqual(argv[argv.index("-n") + 1], "catalog-polaris")

    def test_an_empty_class_or_size_stops_before_any_call(self):
        for var in LOGS_ENV:
            calls = FakeClaim()
            problems, _ = self.step(calls, env={var: ""})
            self.assertIn(var, "\n".join(problems))
            self.assertIn("ADR-036", "\n".join(problems))
            self.assertEqual(calls.calls, [])

    def test_a_missing_storage_class_stops_before_the_claim(self):
        calls = FakeClaim(storage_class=(1, 'storageclasses.storage.k8s.io "shared" not found'))
        problems, _ = self.step(calls)
        self.assertIn("shared", "\n".join(problems))
        self.assertIn("ReadWriteMany", "\n".join(problems))
        self.assertEqual(len(calls.calls), 1)

    def test_dry_run_applies_server_side(self):
        calls = FakeClaim()
        problems, notes = self.step(calls, dry_run=True)
        self.assertEqual(problems, [])
        self.assertIn("--dry-run=server", calls.calls[1])
        self.assertIn("dry-run", "\n".join(notes))

    def test_a_refused_apply_stops_with_its_error(self):
        problems, _ = self.step(FakeClaim(apply=(1, "spec is immutable after creation")))
        self.assertIn("spec is immutable", "\n".join(problems))


CONSOLE_ENV = {"POLARIS_CONSOLE_ENABLED": "true",
               "POLARIS_CONSOLE_ORIGIN": "http://console.example:8080",
               "POLARIS_CONSOLE_API_URL": "http://polaris.example:8181"}


class FakeConsole(FakePolaris):
    """Answers the console step's helm and rollout wait; it checks no Secret."""


class Console(unittest.TestCase):
    def step(self, calls, env=None, dry_run=False):
        return release.console({**BOOTSTRAP_ENV, **CONSOLE_ENV} if env is None else env,
                               calls, dry_run=dry_run)

    def test_off_unless_enabled_and_then_nothing_is_called(self):
        for value in (None, "false", ""):
            with self.subTest(value=value):
                env = dict(BOOTSTRAP_ENV)
                if value is not None:
                    env["POLARIS_CONSOLE_ENABLED"] = value
                calls = FakeConsole()
                problems, notes = self.step(calls, env)
                self.assertEqual((problems, calls.calls), ([], []))
                self.assertIn("Step 2: the console is not enabled", "\n".join(notes))

    def test_installs_the_chart_with_both_values_files_then_waits_for_the_rollout(self):
        calls = FakeConsole()
        problems, notes = self.step(calls)
        self.assertEqual(problems, [])
        self.assertEqual([c[0] for c in calls.calls], ["helm", "kubectl"])
        helm = calls.calls[0]
        self.assertEqual(helm[:3], ["helm", "--kube-context", "kind-lakehouse-gate"])
        self.assertEqual(helm[helm.index("-n") + 1], "catalog-polaris")
        self.assertEqual(helm[helm.index("upgrade"):helm.index("upgrade") + 4],
                         ["upgrade", "--install", "polaris-console", release.CONSOLE_CHART])
        files = [helm[i + 1] for i, a in enumerate(helm) if a == "-f"]
        self.assertEqual(files[0], release.CONSOLE_VALUES)
        self.assertFalse(os.path.exists(files[1]), "the generated values file is left behind")
        rollout = calls.calls[1]
        self.assertEqual(rollout[1:3], ["--context", "kind-lakehouse-gate"])
        self.assertEqual(rollout[rollout.index("-n") + 1], "catalog-polaris")
        self.assertEqual(rollout[rollout.index("rollout"):rollout.index("rollout") + 3],
                         ["rollout", "status", "deployment/polaris-console"])
        self.assertIn("Step 2: deployment/polaris-console", "\n".join(notes))

    def test_the_browser_reaches_polaris_at_the_api_url_from_env(self):
        calls = FakeConsole()
        self.step(calls)
        self.assertEqual(calls.values, [{"config": {"api": {
            "polarisApiUrl": "http://polaris.example:8181",
            "oauthTokenUrl": "http://polaris.example:8181/api/catalog/v1/oauth/tokens",
            "polarisRealm": "POLARIS"}}}])

    def test_enabled_without_its_origin_or_api_url_stops_before_helm(self):
        for var in ("POLARIS_CONSOLE_ORIGIN", "POLARIS_CONSOLE_API_URL"):
            with self.subTest(var=var):
                calls = FakeConsole()
                problems, _ = self.step(calls, {**BOOTSTRAP_ENV, **CONSOLE_ENV, var: ""})
                self.assertIn(var, "\n".join(problems))
                self.assertEqual(calls.calls, [])

    def test_anything_but_true_or_false_is_refused(self):
        calls = FakeConsole()
        problems, _ = self.step(calls, {**BOOTSTRAP_ENV, **CONSOLE_ENV,
                                        "POLARIS_CONSOLE_ENABLED": "yes"})
        self.assertIn("POLARIS_CONSOLE_ENABLED", "\n".join(problems))
        self.assertEqual(calls.calls, [])

    def test_dry_run_is_server_side_and_does_not_wait(self):
        calls = FakeConsole()
        problems, notes = self.step(calls, dry_run=True)
        self.assertEqual(problems, [])
        self.assertIn("--dry-run=server", calls.calls[0])
        self.assertEqual(len(calls.calls), 1)
        self.assertIn("dry-run", "\n".join(notes))

    def test_a_failed_helm_or_rollout_is_a_problem_with_its_error(self):
        problems, _ = self.step(FakeConsole(helm=(1, "schema invalid")))
        self.assertIn("schema invalid", "\n".join(problems))
        problems, _ = self.step(FakeConsole(rollout=(1, "timed out waiting")))
        self.assertIn("timed out waiting", "\n".join(problems))


class FakeBatch:
    """Answers the batch step's helm and its read of the CronJob's image; records every call."""

    def __init__(self, helm=(0, ""), image=None):
        self.helm, self.calls, self.values = helm, [], []
        self.image = release.batch_image() if image is None else image

    def __call__(self, argv, stdin=None):
        self.calls.append(argv)
        if argv[0] == "helm":
            files = [argv[i + 1] for i, a in enumerate(argv) if a == "-f"]
            with open(files[-1], encoding="utf-8") as f:
                self.values.append(json.load(f))
            return self.helm
        if "cronjob/polaris-log-batch" in argv:
            return (0, self.image) if self.image else (1, "NotFound")
        raise AssertionError(f"unexpected call {argv}")


class LogBatch(unittest.TestCase):
    """ADR-036, ADR-037: step 2 installs the batch's CronJob; a failure stops the install."""

    def step(self, calls, dry_run=False):
        return release.log_batch(BOOTSTRAP_ENV, calls, dry_run=dry_run)

    def test_installs_the_chart_with_the_image_built_from_this_checkout(self):
        calls = FakeBatch()
        problems, notes = self.step(calls)
        self.assertEqual(problems, [])
        self.assertEqual([c[0] for c in calls.calls], ["helm", "kubectl"])
        helm = calls.calls[0]
        self.assertEqual(helm[:3], ["helm", "--kube-context", "kind-lakehouse-gate"])
        self.assertEqual(helm[helm.index("-n") + 1], "catalog-polaris")
        self.assertEqual(helm[helm.index("upgrade"):helm.index("upgrade") + 4],
                         ["upgrade", "--install", "polaris-log-batch", release.BATCH_CHART])
        self.assertEqual(calls.values, [release.batch_values(BOOTSTRAP_ENV)])
        files = [helm[i + 1] for i, a in enumerate(helm) if a == "-f"]
        self.assertFalse(os.path.exists(files[0]), "the generated values file is left behind")
        self.assertIn(f"Step 2: cronjob/polaris-log-batch in catalog-polaris runs "
                      f"{release.batch_image()}", "\n".join(notes))

    def test_the_cronjob_is_read_back_in_its_namespace(self):
        calls = FakeBatch()
        self.step(calls)
        read = calls.calls[1]
        self.assertEqual(read[1:3], ["--context", "kind-lakehouse-gate"])
        self.assertEqual(read[read.index("-n") + 1], "catalog-polaris")
        self.assertIn("cronjob/polaris-log-batch", read)

    def test_a_cronjob_with_another_image_stops(self):
        problems, _ = self.step(FakeBatch(image="lakehouse/polaris-log-batch:000000000000"))
        self.assertIn("000000000000", "\n".join(problems))

    def test_a_failed_helm_or_a_missing_cronjob_stops_with_its_error(self):
        problems, _ = self.step(FakeBatch(helm=(1, "schema invalid")))
        self.assertIn("schema invalid", "\n".join(problems))
        problems, _ = self.step(FakeBatch(image=""))
        self.assertIn("NotFound", "\n".join(problems))

    def test_dry_run_is_server_side_and_reads_nothing_back(self):
        calls = FakeBatch()
        problems, notes = self.step(calls, dry_run=True)
        self.assertEqual(problems, [])
        self.assertIn("--dry-run=server", calls.calls[0])
        self.assertEqual(len(calls.calls), 1)
        self.assertIn("dry-run", "\n".join(notes))

    def test_a_registry_in_env_names_the_image_installed_and_read_back(self):
        env = {**BOOTSTRAP_ENV, "IMAGE_REGISTRY": MIRROR}
        image = release.batch_image(registry=MIRROR)
        calls = FakeBatch(image=image)
        problems, notes = release.log_batch(env, calls)
        self.assertEqual(problems, [])
        self.assertEqual(calls.values[0]["image"]["repository"], f"{MIRROR}/polaris-log-batch")
        self.assertIn(f"runs {image}", "\n".join(notes))
        problems, _ = release.log_batch(env, FakeBatch())
        self.assertIn(f"expected {image}", "\n".join(problems))


MIRROR = "mirror.example.invalid:5000/platform"


class BatchImage(unittest.TestCase):
    """ADR-042: the release pushes the batch's image to GHCR; IMAGE_REGISTRY names a mirror."""

    def test_the_default_is_the_github_container_registry(self):
        self.assertEqual(release.DEFAULT_IMAGE_REGISTRY, "ghcr.io/enjay27/lakehouse")
        self.assertTrue(release.batch_image().startswith(
            "ghcr.io/enjay27/lakehouse/polaris-log-batch:"))

    def test_an_empty_or_missing_registry_is_the_default(self):
        for env in ({}, {"IMAGE_REGISTRY": ""}):
            with self.subTest(env=env):
                self.assertEqual(release.image_registry(env), release.DEFAULT_IMAGE_REGISTRY)

    def test_a_registry_in_env_is_used_without_a_trailing_slash(self):
        self.assertEqual(release.image_registry({"IMAGE_REGISTRY": MIRROR}), MIRROR)
        self.assertEqual(release.image_registry({"IMAGE_REGISTRY": MIRROR + "/"}), MIRROR)

    def test_a_mirror_keeps_the_name_and_the_tag(self):
        default, mirrored = release.batch_image(), release.batch_image(registry=MIRROR)
        self.assertEqual(mirrored, f"{MIRROR}/polaris-log-batch:{default.rsplit(':', 1)[1]}")

    def test_the_values_split_the_image_at_its_tag(self):
        self.assertEqual(release.batch_values({"IMAGE_REGISTRY": MIRROR}),
                         {"image": {"repository": f"{MIRROR}/polaris-log-batch",
                                    "tag": release.batch_image().rsplit(":", 1)[1]}})


class Cors(unittest.TestCase):
    def test_polaris_allows_the_consoles_origin_only_while_it_is_enabled(self):
        enabled = release.polaris_values({**BOOTSTRAP_ENV, **CONSOLE_ENV})
        self.assertEqual(enabled["cors"], {"enabled": True,
                                           "allowedOrigins": ["http://console.example:8080"]})
        for env in (BOOTSTRAP_ENV, {**BOOTSTRAP_ENV, **CONSOLE_ENV,
                                    "POLARIS_CONSOLE_ENABLED": "false"},
                    {**BOOTSTRAP_ENV, **CONSOLE_ENV, "POLARIS_CONSOLE_ORIGIN": ""}):
            with self.subTest(env=env.get("POLARIS_CONSOLE_ENABLED")):
                self.assertNotIn("cors", release.polaris_values(env))


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


    def test_batch_image_prints_the_image_the_release_pushes_whatever_env_says(self):
        result = subprocess.run([sys.executable, release.__file__, "batch-image"],
                                capture_output=True, text=True, check=False,
                                env={"PATH": os.environ.get("PATH", ""),
                                     "IMAGE_REGISTRY": MIRROR})
        self.assertEqual((result.returncode, result.stdout), (0, release.batch_image() + "\n"))

    def test_an_unknown_option_exits_2(self):
        result = subprocess.run([sys.executable, release.__file__, "--upgrade"],
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
