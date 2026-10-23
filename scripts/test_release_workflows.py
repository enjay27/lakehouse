"""The release flow's workflows keep their guarantees (ADR-080). Run:
python3 -m unittest discover -s scripts

Candidate, Promote and the candidate guard have never run on GitHub when they merge; these
tests pin what their comments promise: who can write, where they run, what they do not do.
"""

import os
import unittest

from test_gate_workflows import WORKFLOWS, read_workflow, workflows

GUARD = "candidate-guard.yml"


def run_text(job):
    return "\n".join(s.get("run", "") for s in job.get("steps") or [])


def uses_text(job):
    return "\n".join(str(s.get("uses", "")) for s in job.get("steps") or [])


class CandidateGuard(unittest.TestCase):
    def setUp(self):
        self.workflow = read_workflow(os.path.join(WORKFLOWS, GUARD))
        (self.job,) = self.workflow["jobs"].values()

    def test_it_runs_on_a_push_to_candidate_and_rc_branches_only(self):
        self.assertEqual(list(self.workflow["on"]), ["push"])
        self.assertEqual(self.workflow["on"]["push"]["branches"], ["candidate/**", "rc/**"])

    def test_it_can_only_delete_a_branch_and_checks_nothing_out(self):
        self.assertEqual(self.workflow["permissions"], {})
        self.assertEqual(self.job["permissions"], {"contents": "write"})
        self.assertNotIn("actions/checkout", uses_text(self.job))

    def test_a_deletion_is_ignored(self):
        self.assertEqual(self.job["if"], "${{ !github.event.deleted }}")

    def test_it_refuses_a_push_to_rc_a_second_push_a_bad_name_and_a_commit_off_main(self):
        script = run_text(self.job)
        for part in ('== rc/*', 'CREATED" != true', "candidate/[0-9a-f]", "AFTER:0:12",
                     "compare/main...$AFTER", "identical|behind", "git/refs/heads/$GITHUB_REF_NAME"):
            self.assertIn(part, script)


if __name__ == "__main__":
    unittest.main()
