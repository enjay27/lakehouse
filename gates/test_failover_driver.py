"""Tests for failover/driver.py's checks on the running cluster (ADR-047 §5).
Run: python3 -m unittest discover -s gates"""

import json
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "failover"))
sys.path.insert(0, HERE)
import driver  # noqa: E402
import setups  # noqa: E402

ENV = {"NS_CATALOG_POLARIS": "catalog-polaris", "KUBE_CONTEXT": "kind-lakehouse-gate"}


def pods(period):
    return {"items": [{"spec": {"containers": [
        {"name": "postgres", "readinessProbe": {"periodSeconds": period}}]}}
        for _ in range(3)]}


class C1Applied(unittest.TestCase):
    def setUp(self):
        self.want = setups.C1_CLUSTER["probes"]["readiness"]["periodSeconds"]
        patches = [mock.patch.object(driver, "primary_of", return_value="polaris-pg-1"),
                   mock.patch.object(driver, "instances", return_value=pods(self.want))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_a_primary_restarting_under_the_patch_is_not_applied_yet(self):
        # failover 38652896291: C1's patch restarted polaris-pg-1 and psql found no socket.
        failed = (2, 'psql: error: connection to server on socket "/controller/run/'
                     '.s.PGSQL.5432" failed: No such file or directory')
        with mock.patch.object(driver, "call", return_value=failed):
            self.assertFalse(driver.c1_applied(ENV))

    def test_applied_once_the_primary_names_its_synchronous_standbys(self):
        with mock.patch.object(driver, "call",
                               return_value=(0, 'ANY 1 ("polaris-pg-2","polaris-pg-3")\n')):
            self.assertTrue(driver.c1_applied(ENV))

    def test_not_applied_while_the_standbys_are_unset(self):
        with mock.patch.object(driver, "call", return_value=(0, "\n")):
            self.assertFalse(driver.c1_applied(ENV))

    def test_not_applied_while_an_instance_has_the_old_probe(self):
        with mock.patch.object(driver, "instances", return_value=pods(self.want + 1)), \
                mock.patch.object(driver, "call", side_effect=AssertionError("no psql")):
            self.assertFalse(driver.c1_applied(ENV))


def answers(standbys='ANY 1 ("polaris-pg-2","polaris-pg-3")\n', quorum=None):
    """call() as the cluster answers: psql's synchronous standbys, the FailoverQuorum."""
    def call(argv, stdin=None):
        if "psql" in argv:
            return 0, standbys
        if "failoverquorum/polaris-pg" in argv:
            if quorum is None:
                return 1, 'Error from server (NotFound): failoverquorums "polaris-pg" not found'
            return 0, json.dumps({"status": quorum})
        raise AssertionError(argv)
    return call


class C1qApplied(C1Applied):
    """C1q is applied once the primary also reports its failover quorum (ADR-056 §4)."""

    def test_c1_needs_no_failover_quorum(self):
        with mock.patch.object(driver, "call", side_effect=answers()):
            self.assertTrue(driver.c1_applied(ENV, "C1"))

    def test_not_applied_before_the_primary_reports_its_quorum(self):
        with mock.patch.object(driver, "call", side_effect=answers()):
            self.assertFalse(driver.c1_applied(ENV, "C1q"))
        with mock.patch.object(driver, "call", side_effect=answers(quorum={})):
            self.assertFalse(driver.c1_applied(ENV, "C1q"))

    def test_applied_once_the_quorum_is_any_one(self):
        quorum = {"method": "ANY", "standbyNumber": 1, "primary": "polaris-pg-1",
                  "standbyNames": ["polaris-pg-2", "polaris-pg-3"]}
        with mock.patch.object(driver, "call", side_effect=answers(quorum=quorum)):
            self.assertTrue(driver.c1_applied(ENV, "C1q"))



class FellBehind(unittest.TestCase):
    """S5 counts only if the survivor fell behind the standby lost with the primary
    (ADR-058 §1)."""

    def setUp(self):
        p = mock.patch.object(driver, "primary_of", return_value="polaris-pg-2")
        p.start()
        self.addCleanup(p.stop)

    @staticmethod
    def answers(primary="0/3001000,0/3001000\n", survivor="0/3000000\n"):
        def call(argv, stdin=None):
            pod = argv[argv.index("exec") + 1]
            if pod == "polaris-pg-2":
                return 0, primary
            if pod == "polaris-pg-1":
                return 0, survivor
            raise AssertionError(argv)
        return call

    def check(self, **answers):
        with mock.patch.object(driver, "call", side_effect=self.answers(**answers)):
            return driver.fell_behind(ENV, "polaris-pg-3", "polaris-pg-1")

    def test_valid_when_the_survivor_received_less_than_the_standby_flushed(self):
        self.assertEqual(self.check(), (True, None, {
            "primary_lsn": "0/3001000", "standby_lsn": "0/3001000",
            "survivor_lsn": "0/3000000"}))

    def test_invalid_when_the_survivor_kept_up(self):
        valid, reason, _ = self.check(survivor="0/3001000\n")
        self.assertFalse(valid)
        self.assertIn("polaris-pg-1 received 0/3001000", reason)

    def test_invalid_when_the_primary_lists_no_such_standby(self):
        valid, reason, lsns = self.check(primary="\n")
        self.assertFalse(valid)
        self.assertIn("polaris-pg-3", reason)
        self.assertIsNone(lsns["standby_lsn"])

    def test_invalid_when_the_standby_reported_no_flush(self):
        valid, reason, _ = self.check(primary="0/3001000,\n")
        self.assertFalse(valid)
        self.assertIn("no flushed LSN", reason)

    def test_invalid_when_an_instance_does_not_answer(self):
        with mock.patch.object(driver, "call", return_value=(2, "psql: error")):
            valid, reason, _ = driver.fell_behind(ENV, "polaris-pg-3", "polaris-pg-1")
        self.assertFalse(valid)
        self.assertIn("psql: error", reason)


if __name__ == "__main__":
    unittest.main()
