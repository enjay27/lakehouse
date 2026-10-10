"""Tests for repo_check.py. Run: python3 -m unittest discover -s scripts"""

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import repo_check as rc  # noqa: E402

GITATTRIBUTES = "* text=auto eol=lf\n"
# Built from parts so that this file does not itself match the patterns it tests.
PRIVATE_KEY = "-----BEGIN " + "RSA PRIVATE KEY-----\nMIIE\n-----END " + "RSA PRIVATE KEY-----\n"
AWS_KEY = "AKIA" + "ABCDEFGHIJKLMNOP"
GITHUB_TOKEN = "ghp_" + "a1" * 18


class RepoCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self.files = []
        self.write(".gitattributes", GITATTRIBUTES)

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, rel, data):
        full = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as f:
            f.write(data.encode("utf-8") if isinstance(data, str) else data)
        if rel not in self.files:
            self.files.append(rel)

    def errors(self):
        return rc.check_repo(self.root, self.files)


class Clean(RepoCase):
    def test_a_clean_tree_passes(self):
        self.write("README.md", "# lakehouse\n")
        self.write(".env.template", "KUBE_CONTEXT=\nNS_STORAGE=storage\nMINIO_ROOT_PASSWORD=\n")
        self.assertEqual(self.errors(), [])


class EnvFiles(RepoCase):
    def test_a_committed_env_file_fails(self):
        for name in (".env", ".env.local", "suite/.env", "suite/.env.prod"):
            with self.subTest(name=name):
                self.files = [".gitattributes"]
                self.write(name, "X=1\n")
                self.assertEqual(self.errors(), [f"{name}: environment file is committed (ADR-002)"])

    def test_the_template_is_allowed(self):
        self.write(".env.template", "KUBE_CONTEXT=\n")
        self.assertEqual(self.errors(), [])

    def test_a_credential_with_a_value_in_the_template_fails_without_printing_it(self):
        self.write(".env.template", "# comment\nNS_STORAGE=storage\nMINIO_ROOT_PASSWORD=hunter2\n"
                   "POLARIS_ROOT_CLIENT_ID=root\nOPENSEARCH_USER=admin\nAPI_TOKEN=x\n"
                   "SOME_KEY=y\nPOLARIS_ROOT_CLIENT_SECRET=  \n")
        errors = self.errors()
        self.assertEqual(errors, [
            ".env.template:3: MINIO_ROOT_PASSWORD has a value; credentials stay empty (ADR-006)",
            ".env.template:4: POLARIS_ROOT_CLIENT_ID has a value; credentials stay empty (ADR-006)",
            ".env.template:5: OPENSEARCH_USER has a value; credentials stay empty (ADR-006)",
            ".env.template:6: API_TOKEN has a value; credentials stay empty (ADR-006)",
            ".env.template:7: SOME_KEY has a value; credentials stay empty (ADR-006)",
        ])
        self.assertNotIn("hunter2", "\n".join(errors))


class KeyFiles(RepoCase):
    def test_key_and_keystore_files_fail(self):
        names = ["tls.pem", "certs/server.key", "a.p12", "a.pfx", "a.jks", "release.keystore",
                 "id_rsa", "home/id_ed25519"]
        for name in names:
            self.write(name, "x\n")
        self.assertEqual(self.errors(), [
            f"{name}: key or keystore file is committed (ADR-006)" for name in sorted(names)])


class Contents(RepoCase):
    def test_a_private_key_block_fails(self):
        self.write("charts/x/values.yaml", "a: 1\nkey: |\n" + PRIVATE_KEY)
        self.assertEqual(self.errors(), [
            "charts/x/values.yaml:3: private key block (ADR-006)"])

    def test_an_aws_access_key_fails_without_printing_it(self):
        self.write("suite/conf.py", f"KEY = '{AWS_KEY}'\n")
        errors = self.errors()
        self.assertEqual(errors, ["suite/conf.py:1: AWS access key ID (ADR-006)"])
        self.assertNotIn(AWS_KEY, "\n".join(errors))

    def test_a_github_token_fails(self):
        self.write("notes.md", f"token {GITHUB_TOKEN}\n")
        self.assertEqual(self.errors(), ["notes.md:1: GitHub token (ADR-006)"])

    def test_binary_files_are_not_scanned(self):
        self.write("logo.png", b"\x89PNG\x00\x00" + AWS_KEY.encode() + b"\r\n")
        self.assertEqual(self.errors(), [])


class LineEndings(RepoCase):
    def test_crlf_fails(self):
        self.write("README.md", "# a\r\nb\r\n")
        self.assertEqual(self.errors(), ["README.md: CRLF line endings (.gitattributes)"])

    def test_gitattributes_must_keep_lf(self):
        self.write(".gitattributes", "*.png binary\n")
        self.assertEqual(self.errors(), [".gitattributes: no `* text=auto eol=lf` line"])

    def test_missing_gitattributes_fails(self):
        os.remove(os.path.join(self.root, ".gitattributes"))
        self.files.remove(".gitattributes")
        self.assertEqual(self.errors(), [".gitattributes: no `* text=auto eol=lf` line"])


class Main(unittest.TestCase):
    def test_main_checks_the_files_git_tracks(self):
        with tempfile.TemporaryDirectory() as root:
            def git(*args):
                subprocess.run(["git", "-C", root, *args], check=True, capture_output=True)
            git("init", "-q")
            for rel, text in ((".gitattributes", GITATTRIBUTES), (".env", "X=1\n"),
                              ("README.md", "# a\n")):
                with open(os.path.join(root, rel), "w", encoding="utf-8", newline="") as f:
                    f.write(text)
            git("add", ".gitattributes", "README.md")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(rc.main(["repo_check.py", root]), 0)
            self.assertEqual(out.getvalue(), "Repository checks pass\n")
            git("add", ".env")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(rc.main(["repo_check.py", root]), 1)
            self.assertEqual(out.getvalue(), ".env: environment file is committed (ADR-002)\n")


class RealRepository(unittest.TestCase):
    def test_this_repository_passes(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        self.assertEqual(rc.check_repo(root, rc.tracked_files(root)), [])


if __name__ == "__main__":
    unittest.main()
