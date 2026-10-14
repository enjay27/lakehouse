"""Tests for check_restore.sh, run here with a fake psql on PATH that answers as PostgreSQL would.
Run: python3 -m unittest discover -s gates"""

import os
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "check_restore.sh")
# Answers 1 to every count, but 0 to the one whose SQL holds $MISSING.
FAKE_PSQL = """#!/bin/sh
echo "$*" >> "$PSQL_LOG"
case "$*" in *"$MISSING"*) echo 0; exit 0;; esac
echo 1
"""


class CheckRestore(unittest.TestCase):
    def run_script(self, missing="nothing-is-missing"):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "psql"), "w", encoding="utf-8") as f:
                f.write(FAKE_PSQL)
            os.chmod(os.path.join(d, "psql"), 0o755)
            log = os.path.join(d, "log")
            result = subprocess.run(
                ["sh", SCRIPT, "polaris-pg-restore-rw", "polaris", "POLARIS", "gate_catalog"],
                capture_output=True, text=True, check=False,
                env={"PATH": f"{d}:{os.environ['PATH']}", "PSQL_LOG": log, "MISSING": missing,
                     "PGUSER": "polaris", "PGPASSWORD": "pw-secret"})
            with open(log, encoding="utf-8") as f:
                calls = f.read().splitlines()
        return result, calls

    def test_the_root_principal_and_the_catalog_are_read_from_the_restored_database(self):
        result, calls = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(calls), 2)
        for call in calls:
            self.assertIn("host=polaris-pg-restore-rw port=5432 dbname=polaris sslmode=require",
                          call)
            self.assertIn("FROM POLARIS_SCHEMA.ENTITIES WHERE realm_id = 'POLARIS'", call)
        # Polaris's PolarisEntityType: PRINCIPAL is 2, CATALOG is 4.
        self.assertIn("type_code = 2 AND name = 'root'", calls[0])
        self.assertIn("type_code = 4 AND name = 'gate_catalog'", calls[1])
        self.assertEqual(result.stdout.count("ok "), 2)

    def test_a_missing_row_fails_and_is_named(self):
        for missing, name in (("'root'", "root principal"), ("'gate_catalog'", "gate_catalog")):
            with self.subTest(missing=missing):
                result, calls = self.run_script(missing)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(len(calls), 2)
                (failed,) = [line for line in result.stdout.splitlines()
                             if line.startswith("FAIL")]
                self.assertIn(name, failed)

    def test_the_password_is_never_printed(self):
        result, _ = self.run_script("'root'")
        self.assertNotIn("pw-secret", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
