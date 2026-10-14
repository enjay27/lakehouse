"""Tests for failover/driver.py's checks on the running cluster (ADR-047 §5).
Run: python3 -m unittest discover -s gates"""

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


if __name__ == "__main__":
    unittest.main()
