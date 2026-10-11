"""Tests for check_db.sh, run here with a fake psql on PATH that answers as PostgreSQL would.
Run: python3 -m unittest discover -s gates"""

import os
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "check_db.sh")
# Prints the count the script's last statement asks for, unless the sslmode is in $FAIL_MODES.
FAKE_PSQL = """#!/bin/sh
echo "$*" >> "$PSQL_LOG"
case "$1" in *"sslmode=$FAIL_MODE "*) echo "psql: error: connection refused" >&2; exit 2;; esac
echo 1
"""


class CheckDb(unittest.TestCase):
    def run_script(self, fail_mode="none"):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "psql"), "w", encoding="utf-8") as f:
                f.write(FAKE_PSQL)
            os.chmod(os.path.join(d, "psql"), 0o755)
            log = os.path.join(d, "log")
            result = subprocess.run(
                ["sh", SCRIPT, "recorder.standins.svc.cluster.local", "polaris"],
                capture_output=True, text=True, check=False,
                env={"PATH": f"{d}:{os.environ['PATH']}", "PSQL_LOG": log,
                     "FAIL_MODE": fail_mode, "PGUSER": "polaris", "PGPASSWORD": "pw-secret"})
            with open(log, encoding="utf-8") as f:
                calls = f.read().splitlines()
        return result, calls

    def test_a_login_with_and_without_tls_both_write_and_read_a_row(self):
        result, calls = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(calls), 2)
        self.assertIn("sslmode=require ", calls[0])
        self.assertIn("sslmode=disable ", calls[1])
        for call in calls:
            self.assertIn("host=recorder.standins.svc.cluster.local", call)
            self.assertIn("dbname=polaris", call)
            self.assertIn("INSERT INTO gate_check", call)
        self.assertIn("ok   PostgreSQL through the recorder, sslmode=require", result.stdout)

    def test_a_failed_login_fails_and_says_which_mode(self):
        result, _ = self.run_script(fail_mode="disable")
        self.assertEqual(result.returncode, 1)
        self.assertIn("FAIL PostgreSQL through the recorder, sslmode=disable", result.stdout)

    def test_the_password_is_never_printed_or_passed_as_an_argument(self):
        result, calls = self.run_script()
        self.assertNotIn("pw-secret", result.stdout + result.stderr + "\n".join(calls))


if __name__ == "__main__":
    unittest.main()
