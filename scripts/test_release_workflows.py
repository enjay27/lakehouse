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


AUTO_CANDIDATE = "auto-candidate.yml"


class AutoCandidate(unittest.TestCase):
    def setUp(self):
        self.workflow = read_workflow(os.path.join(WORKFLOWS, AUTO_CANDIDATE))
        self.jobs = self.workflow["jobs"]

    def job_named(self, name):
        (key,) = [k for k, j in self.jobs.items() if j.get("name") == name]
        return self.jobs[key]

    def test_it_follows_statics_completed_runs_on_main_and_has_its_own_lock(self):
        self.assertEqual(list(self.workflow["on"]), ["workflow_run"])
        trigger = self.workflow["on"]["workflow_run"]
        self.assertEqual(trigger["workflows"], ["Static"])
        self.assertEqual(trigger["types"], ["completed"])
        self.assertEqual(trigger["branches"], ["main"])
        self.assertEqual(self.workflow["permissions"], {})
        # Never `release`: a queued run there would cancel Candidate's.
        self.assertEqual(self.workflow["concurrency"],
                         {"group": "auto-candidate", "cancel-in-progress": "false"})

    def test_the_job_names_are_the_ones_the_check_read_leaves_out(self):
        import sys
        sys.path.insert(0, os.path.join(os.path.dirname(WORKFLOWS), "..", "install"))
        from release_tag import CANDIDATE_JOBS
        self.assertEqual({j["name"] for j in self.jobs.values()} - CANDIDATE_JOBS, set())

    def test_the_trigger_names_the_workflow_the_script_waits_for(self):
        script = run_text(self.job_named("decide on a candidate"))
        self.assertIn('release_tag.py ci-done "Static"', script)

    def test_only_a_passed_run_from_a_push_or_auto_merge_decides(self):
        condition = self.job_named("decide on a candidate")["if"]
        for part in ("workflow_run.conclusion == 'success'", "workflow_run.event == 'push'",
                     "workflow_run.event == 'workflow_run'"):
            self.assertIn(part, condition)
        self.assertNotIn("schedule", condition)

    def test_deciding_reads_the_commit_with_its_tags_and_cannot_write(self):
        job = self.job_named("decide on a candidate")
        self.assertEqual(job["permissions"], {"contents": "read", "actions": "read"})
        (checkout,) = [s for s in job["steps"] if "actions/checkout" in str(s.get("uses"))]
        self.assertEqual(checkout["uses"], "actions/checkout@v7")
        self.assertEqual(checkout["with"]["ref"], "${{ github.event.workflow_run.head_sha }}")
        self.assertEqual(checkout["with"]["fetch-depth"], "0")
        self.assertEqual(checkout["with"]["persist-credentials"], "false")
        script = run_text(job)
        self.assertLess(script.index("release_tag.py should-candidate"),
                        script.index("release_tag.py ci-done"))
        self.assertIn("$GITHUB_STEP_SUMMARY", script)

    def test_only_the_push_job_can_write_and_it_checks_nothing_out(self):
        writers = [j["name"] for j in self.jobs.values()
                   if "write" in (j.get("permissions") or {}).values()]
        self.assertEqual(writers, ["push the candidate branch"])
        push = self.job_named("push the candidate branch")
        self.assertEqual(push["permissions"], {"contents": "write", "actions": "read"})
        self.assertNotIn("actions/checkout", uses_text(push))
        self.assertNotIn("python3", run_text(push))
        self.assertIn("decide", push["needs"])
        self.assertIn("== 'true'", push["if"])

    def test_the_push_job_leaves_a_moved_main_and_a_running_candidate_alone(self):
        script = run_text(self.job_named("push the candidate branch"))
        for part in ("git/ref/heads/main", "exit 0", "workflows/candidate.yml/runs",
                     "::error::", "git/matching-refs/heads/candidate/", "-X DELETE",
                     "-X POST", "Run workflow"):
            self.assertIn(part, script)
        self.assertLess(script.index("workflows/candidate.yml/runs"), script.index("-X DELETE"))
        self.assertLess(script.index("-X DELETE"), script.index("-X POST"))


CANDIDATE = "candidate.yml"
GATE_CALLS = {"dry-run": "./.github/workflows/dry-run.yml",
              "smoke-upgrade": "./.github/workflows/smoke.yml",
              "smoke-fresh": "./.github/workflows/smoke.yml"}


class Candidate(unittest.TestCase):
    def setUp(self):
        self.workflow = read_workflow(os.path.join(WORKFLOWS, CANDIDATE))
        self.jobs = self.workflow["jobs"]

    def job_named(self, name):
        (key,) = [k for k, j in self.jobs.items() if j.get("name") == name]
        return self.jobs[key]

    def test_kade_starts_it_by_hand_and_it_shares_the_release_lock_with_promote(self):
        self.assertEqual(list(self.workflow["on"]), ["workflow_dispatch"])
        self.assertEqual(self.workflow["permissions"], {})
        self.assertEqual(self.workflow["concurrency"],
                         {"group": "release", "cancel-in-progress": "false"})

    def test_it_runs_on_a_candidate_branch_only(self):
        self.assertIn("refs/heads/candidate/*)", run_text(self.job_named("check the candidate")))

    def test_the_job_names_are_the_ones_the_check_read_leaves_out(self):
        import sys
        sys.path.insert(0, os.path.join(os.path.dirname(WORKFLOWS), "..", "install"))
        from release_tag import CANDIDATE_JOBS, CANDIDATE_GATE_PREFIXES
        own = {j["name"] for j in self.jobs.values() if "uses" not in j}
        self.assertEqual(own - CANDIDATE_JOBS, set())
        for key, called in GATE_CALLS.items():
            self.assertTrue(self.jobs[key]["name"] + " / " in CANDIDATE_GATE_PREFIXES, key)

    def test_the_check_reads_the_branch_the_head_and_the_commits_checks_before_any_gate(self):
        job = self.job_named("check the candidate")
        self.assertEqual(job["permissions"], {"contents": "read", "checks": "read"})
        script = run_text(job)
        for part in ("release_tag.py check-candidate", "release_tag.py checks-passed",
                     "git/ref/heads/$GITHUB_REF_NAME", "moved from"):
            self.assertIn(part, script)

    def test_it_names_the_final_and_candidate_tags_and_refuses_a_tagged_commit(self):
        script = run_text(self.job_named("check the candidate"))
        for part in ("release_tag.py next", "release_tag.py rc ", "release_tag.py superseded",
                     "tag --points-at"):
            self.assertIn(part, script)

    def test_the_gates_are_the_called_dry_run_and_two_smokes_after_the_check(self):
        for key, uses in GATE_CALLS.items():
            with self.subTest(job=key):
                job = self.jobs[key]
                self.assertEqual(job["uses"], uses)
                self.assertEqual(job["needs"], "check")
                self.assertEqual(job["permissions"]["contents"], "write")
                self.assertEqual(job["permissions"].get("issues"),
                                 None if key == "dry-run" else "write")
        self.assertEqual(self.jobs["smoke-upgrade"]["with"], {"fresh": "false"})
        self.assertEqual(self.jobs["smoke-fresh"]["with"], {"fresh": "true"})

    def test_only_the_gates_image_tag_and_cleanup_can_write(self):
        writers = {k for k, j in self.jobs.items()
                   if "write" in (j.get("permissions") or {}).values()}
        self.assertEqual(writers, set(GATE_CALLS) | {"image", "tag", "cleanup"})

    def test_the_image_follows_the_gates_and_is_the_only_job_with_packages(self):
        gates = sorted(GATE_CALLS)
        image = self.job_named("the log batch's image on GHCR")
        self.assertEqual(sorted(image["needs"]), sorted(["check"] + gates))
        self.assertEqual(image["permissions"], {"contents": "read", "packages": "write"})
        self.assertEqual([k for k, j in self.jobs.items()
                          if "packages" in (j.get("permissions") or {})], ["image"])
        script = run_text(image)
        for part in ("release.py batch-image", "imagetools inspect", "docker push",
                     "DOCKER_CONFIG=$anonymous", "cannot be pulled without credentials"):
            self.assertIn(part, script)

    def test_the_tag_job_runs_none_of_the_repositorys_code(self):
        tag = self.job_named("rc tag and prerelease")
        self.assertEqual(tag["permissions"], {"contents": "write"})
        self.assertNotIn("actions/checkout", uses_text(tag))
        self.assertNotIn("python3", run_text(tag))

    def test_the_tag_job_needs_the_check_the_notes_and_the_image(self):
        tag = self.job_named("rc tag and prerelease")
        keys = {k for k, j in self.jobs.items() if j["name"] in ("check the candidate",
                "notes of the candidate", "the log batch's image on GHCR")}
        self.assertEqual(set(tag["needs"]), keys)

    def test_the_tag_job_reads_the_branch_again_then_tags_the_same_commit_without_a_stamp(self):
        script = run_text(self.job_named("rc tag and prerelease"))
        self.assertLess(script.index("moved from"), script.index("gh release create"))
        for part in ('--target "$GITHUB_SHA"', "--prerelease", 'refs/heads/rc/$RC',
                     "BATCH_IMAGE_DIGEST", "$SUPERSEDED"):
            self.assertIn(part, script)
        for part in ("git/commits", "git/trees", "git/blobs"):
            self.assertNotIn(part, script)

    def test_the_last_job_deletes_the_branch_only_after_the_tag_job_succeeded(self):
        cleanup = self.jobs["cleanup"]
        self.assertNotIn("always()", cleanup["if"], "a failed or refused run keeps its branch")
        self.assertIn("needs.tag.result == 'success'", cleanup["if"])
        self.assertIn("startsWith(github.ref, 'refs/heads/candidate/')", cleanup["if"])
        self.assertEqual(cleanup["permissions"], {"contents": "write"})
        others = sorted(k for k in self.jobs if k != "cleanup")
        self.assertEqual(sorted(cleanup["needs"]), others, "it needs every other job")
        self.assertIn("git/refs/heads/$GITHUB_REF_NAME", run_text(cleanup))


class Promote(unittest.TestCase):
    def setUp(self):
        self.workflow = read_workflow(os.path.join(WORKFLOWS, "promote.yml"))
        self.jobs = self.workflow["jobs"]

    def test_kade_starts_it_by_hand_under_the_release_lock(self):
        self.assertEqual(list(self.workflow["on"]), ["workflow_dispatch"])
        self.assertEqual(self.workflow["permissions"], {})
        self.assertEqual(self.workflow["concurrency"],
                         {"group": "release", "cancel-in-progress": "false"})

    def test_it_runs_on_an_rc_branch_only(self):
        self.assertIn("refs/heads/rc/platform-*-rc.*)", run_text(self.jobs["check"]))

    def test_only_the_tag_job_writes_and_it_runs_none_of_the_repositorys_code(self):
        self.assertEqual({k for k, j in self.jobs.items()
                          if "write" in (j.get("permissions") or {}).values()}, {"tag"})
        self.assertEqual(self.jobs["tag"]["permissions"], {"contents": "write"})
        self.assertEqual(self.jobs["tag"]["needs"], "check")
        self.assertNotIn("actions/checkout", uses_text(self.jobs["tag"]))
        self.assertNotIn("python3", run_text(self.jobs["tag"]))

    def test_it_builds_nothing_and_reads_no_packages(self):
        text = run_text(self.jobs["check"]) + run_text(self.jobs["tag"])
        for part in ("docker", "packages", "buildx"):
            self.assertNotIn(part, text)
        self.assertFalse(any("packages" in (j.get("permissions") or {})
                             for j in self.jobs.values()))

    def test_the_check_refuses_unless_the_branch_is_at_its_tag_and_the_prerelease_exists(self):
        script = run_text(self.jobs["check"])
        for part in ("release_tag.py rc-branch", "release_tag.py check-promote",
                     'refs/tags/$rc^{commit}', "isPrerelease", "release_tag.py released"):
            self.assertIn(part, script)

    def test_the_tag_job_reads_the_branch_again_then_tags_the_same_commit_with_the_rcs_notes(self):
        script = run_text(self.jobs["tag"])
        self.assertLess(script.index("moved from"), script.index("gh release create"))
        for part in ('--target "$COMMIT"', "--latest", "$RELEASED", "base64 -d"):
            self.assertIn(part, script)


class RetiredFlow(unittest.TestCase):
    def test_the_rc_gate_and_release_workflows_are_gone(self):
        names = set(workflows())
        self.assertFalse(names & {"release.yml", "release-gate.yml"})
        self.assertLessEqual({"candidate.yml", "promote.yml", GUARD}, names)

    def test_no_workflow_runs_on_a_pull_request_into_rc(self):
        for name, workflow in workflows().items():
            on = workflow.get("on") or {}
            pr = on.get("pull_request") if isinstance(on, dict) else None
            with self.subTest(workflow=name):
                self.assertNotIn("rc", (pr or {}).get("branches") or [])
                self.assertNotIn("rc", (pr or {}).get("branches-ignore") or [])


if __name__ == "__main__":
    unittest.main()
