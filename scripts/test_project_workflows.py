"""The Project bridge's two workflows (ADR-073 §5). Run: python3 -m unittest discover -s scripts

They are this repository's own: they run scripts/project_bridge.py from the checkout, with a
token from the organization's bridge App, and reach nothing of another organization's. The
workflows are read with test_gate_workflows' reader (no PyYAML on the runner).
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from test_gate_workflows import WORKFLOWS, read_workflow, workflows  # noqa: E402

SYNC = "project-sync.yml"
SNAPSHOT = "project-snapshot.yml"
APP_TOKEN = "actions/create-github-app-token@v3"


def job(workflow):
    (only,) = workflow["jobs"].values()
    return only


def step(workflow, uses=None, run=None):
    for s in job(workflow)["steps"]:
        if uses and str(s.get("uses", "")).startswith(uses):
            return s
        if run and run in str(s.get("run", "")):
            return s
    raise AssertionError(f"no step with uses={uses} run={run}")


class Sync(unittest.TestCase):
    def setUp(self):
        self.w = read_workflow(os.path.join(WORKFLOWS, SYNC))

    def test_runs_on_issues_opened_reopened_and_labeled_only(self):
        self.assertEqual(self.w["on"], {"issues": {"types": ["opened", "reopened", "labeled"]}})

    def test_its_job_only_writes_issues_and_waits_for_the_project_number(self):
        self.assertEqual(self.w["permissions"], {})
        self.assertEqual(job(self.w)["permissions"], {"issues": "write"})
        self.assertEqual(job(self.w)["if"], "vars.PROJECT_NUMBER != ''")

    def test_the_app_token_writes_projects_of_this_repositorys_owner(self):
        token = step(self.w, uses=APP_TOKEN)["with"]
        self.assertEqual(token["owner"], "${{ github.repository_owner }}")
        self.assertEqual(token["permission-organization-projects"], "write")
        self.assertEqual(token["permission-issues"], "read")
        self.assertEqual(token["client-id"], "${{ vars.BRIDGE_APP_CLIENT_ID }}")
        self.assertEqual(token["private-key"], "${{ secrets.BRIDGE_APP_PRIVATE_KEY }}")

    def test_runs_this_repositorys_bridge(self):
        sync = step(self.w, run="project_bridge.py")
        self.assertEqual(sync["run"], "python3 scripts/project_bridge.py sync")
        self.assertEqual(sync["env"]["PROJECT_TOKEN"], "${{ steps.app-token.outputs.token }}")
        self.assertEqual(sync["env"]["ORG"], "${{ github.repository_owner }}")


class Snapshot(unittest.TestCase):
    def setUp(self):
        self.w = read_workflow(os.path.join(WORKFLOWS, SNAPSHOT))

    def test_runs_on_a_schedule_and_by_hand_only(self):
        self.assertEqual(set(self.w["on"]), {"schedule", "workflow_dispatch"})

    def test_its_job_only_writes_contents_and_waits_for_the_project_number(self):
        self.assertEqual(self.w["permissions"], {})
        self.assertEqual(job(self.w)["permissions"], {"contents": "write"})
        self.assertEqual(job(self.w)["if"], "vars.PROJECT_NUMBER != ''")

    def test_the_app_token_only_reads(self):
        token = step(self.w, uses=APP_TOKEN)["with"]
        self.assertEqual(token["owner"], "${{ github.repository_owner }}")
        self.assertEqual(token["permission-organization-projects"], "read")
        self.assertEqual(token["permission-issues"], "read")
        self.assertEqual(token["permission-pull-requests"], "read")

    def test_writes_status_md_to_the_status_branch(self):
        self.assertIn("python3 scripts/project_bridge.py snapshot", step(self.w, run="snapshot")["run"])
        publish = step(self.w, run="git push")["run"]
        self.assertIn("git init -q -b status", publish)
        self.assertIn("status", publish.split("git push")[1])


class Both(unittest.TestCase):
    def test_checkouts_keep_no_credentials(self):
        for name in (SYNC, SNAPSHOT):
            with self.subTest(workflow=name):
                checkout = step(read_workflow(os.path.join(WORKFLOWS, name)), uses="actions/checkout@")
                self.assertEqual(checkout["with"]["persist-credentials"], "false")

    def test_no_workflow_reaches_another_organizations_bridge(self):
        # ADR-073 §5: nothing is checked out from, called in or shared with star-resonance.
        for name in workflows():
            with open(os.path.join(WORKFLOWS, name), encoding="utf-8") as f:
                self.assertNotIn("star-resonance", f.read(), name)


if __name__ == "__main__":
    unittest.main()
