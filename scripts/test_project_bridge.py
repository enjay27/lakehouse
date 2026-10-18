"""Tests for project_bridge.py (ADR-073), adapted with it from star-resonance/.github at 3396a33.
Run: python3 -m unittest discover -s scripts"""

import datetime as dt
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import project_bridge as pb  # noqa: E402


class ParseCommand(unittest.TestCase):
    def test_known_commands(self):
        self.assertEqual(pb.parse_command("cmd:status-now"), ("Status", "Now"))
        self.assertEqual(pb.parse_command("cmd:verify-not-verified"), ("Verification", "NOT VERIFIED"))
        self.assertEqual(pb.parse_command("cmd:verify-needs-linux"), ("Verification", "Needs Linux test cluster"))

    def test_the_eleven_commands_of_adr_073(self):
        self.assertEqual(sorted(pb.COMMANDS), sorted([
            "status-backlog", "status-next", "status-now", "status-in-review", "status-done",
            "verify-verified", "verify-not-verified", "verify-needs-linux",
            "priority-p1", "priority-p2", "priority-p3",
        ]))

    def test_other_repositories_environments_are_unknown(self):
        # Only a Linux test cluster is an environment here; another device's label is reported, not applied.
        self.assertEqual(pb.VERIFICATION_ENVIRONMENTS, ["Needs Linux test cluster"])
        self.assertIsNone(pb.parse_command("cmd:verify-needs-windows"))
        plan = pb.plan_labels("labeled", "cmd:verify-needs-windows", [], True)
        self.assertEqual((plan.apply, plan.unknown), ([], ["cmd:verify-needs-windows"]))

    def test_the_organization_is_this_repositorys(self):
        self.assertEqual(pb.ORG, "kade-k8s-platform")

    def test_case_and_space_insensitive(self):
        self.assertEqual(pb.parse_command("CMD:Status-Now "), ("Status", "Now"))

    def test_unknown_and_non_command(self):
        self.assertIsNone(pb.parse_command("cmd:status-later"))
        self.assertIsNone(pb.parse_command("bug"))

    def test_every_command_names_a_real_option(self):
        options = {
            "Status": pb.STATUS_OPTIONS,
            "Verification": pb.VERIFICATION_OPTIONS,
            "Priority": pb.PRIORITY_OPTIONS,
        }
        for suffix, (field, option) in pb.COMMANDS.items():
            self.assertIn(option, options[field], suffix)


class PlanLabels(unittest.TestCase):
    def test_opened_by_writer_applies_all_commands(self):
        plan = pb.plan_labels("opened", "", ["bug", "cmd:status-now", "cmd:priority-p1"], True)
        self.assertEqual(plan.apply, ["cmd:status-now", "cmd:priority-p1"])
        self.assertEqual(plan.strip, [])

    def test_opened_by_outsider_strips_commands(self):
        plan = pb.plan_labels("opened", "", ["cmd:status-now", "question"], False)
        self.assertEqual(plan.apply, [])
        self.assertEqual(plan.strip, ["cmd:status-now"])

    def test_labeled_only_counts_the_new_label(self):
        plan = pb.plan_labels("labeled", "cmd:verify-verified", ["cmd:status-now", "cmd:verify-verified"], True)
        self.assertEqual(plan.apply, ["cmd:verify-verified"])

    def test_labeled_with_ordinary_label_does_nothing(self):
        plan = pb.plan_labels("labeled", "bug", ["bug"], True)
        self.assertEqual((plan.apply, plan.strip, plan.unknown), ([], [], []))

    def test_unknown_command_is_reported_not_removed(self):
        plan = pb.plan_labels("labeled", "cmd:status-someday", [], True)
        self.assertEqual(plan.unknown, ["cmd:status-someday"])
        self.assertEqual(plan.apply, [])

    def test_other_actions_do_nothing(self):
        plan = pb.plan_labels("edited", "", ["cmd:status-now"], True)
        self.assertEqual(plan.apply, [])

    def test_last_label_wins_per_field(self):
        self.assertEqual(
            pb.last_wins(["cmd:status-next", "cmd:priority-p2", "cmd:status-now"]),
            {"Status": "Now", "Priority": "P2"},
        )


PROJECT_DATA = {
    "organization": {
        "projectV2": {
            "id": "PVT_1",
            "fields": {
                "nodes": [
                    {"id": "F_status", "name": "Status", "options": [{"id": "o_now", "name": "Now"}]},
                    {"id": "F_target", "name": "Target"},
                    {},
                ]
            },
        }
    }
}


class ProjectFields(unittest.TestCase):
    def test_option_lookup(self):
        project = pb.parse_project(PROJECT_DATA)
        self.assertEqual(project.option("Status", "Now"), ("F_status", "o_now"))

    def test_missing_field_or_option_is_an_error(self):
        project = pb.parse_project(PROJECT_DATA)
        with self.assertRaises(pb.GitHubError):
            project.option("Priority", "P1")
        with self.assertRaises(pb.GitHubError):
            project.option("Status", "Backlog")

    def test_missing_project_is_an_error(self):
        with self.assertRaises(pb.GitHubError):
            pb.parse_project({"organization": {"projectV2": None}})


def node(kind, number, title, state, values, archived=False, assignees=()):
    content = {"__typename": kind, "title": title}
    if kind != "DraftIssue":
        content.update(
            number=number, url=f"https://github.com/kade-k8s-platform/lakehouse/issues/{number}",
            state=state, repository={"name": "lakehouse"},
            assignees={"nodes": [{"login": login} for login in assignees]},
        )
    field_nodes = []
    for name, value in values.items():
        if name in ("Start", "Target"):
            field_nodes.append({"date": value, "field": {"name": name}})
        else:
            field_nodes.append({"name": value, "field": {"name": name}})
    field_nodes.append({})  # text/number values come back as empty objects
    return {"isArchived": archived, "fieldValues": {"nodes": field_nodes}, "content": content}


def page(*nodes):
    return {"organization": {"projectV2": {"title": "Lakehouse", "items": {
        "pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": list(nodes)}}}}


class Snapshot(unittest.TestCase):
    def setUp(self):
        self.items = pb.parse_items([page(
            node("Issue", 1, "Datahub ingests from Polaris", "OPEN",
                 {"Status": "Now", "Priority": "P1", "Target": "2026-10-20"}),
            node("Issue", 2, "Heap share | knee", "OPEN", {"Status": "Next"}),
            node("Issue", 3, "Restore runs on a test cluster", "CLOSED",
                 {"Status": "Done", "Verification": "Needs Linux test cluster"},
                 assignees=["enjay27"]),
            node("PullRequest", 4, "Log batch", "MERGED",
                 {"Status": "Done", "Verification": "NOT VERIFIED"}),
            node("DraftIssue", None, "Look at the console later", None, {}),
            node("Issue", 5, "Old thing", "CLOSED", {"Status": "Done"}, archived=True),
            node("Issue", 6, "Choose the pooler", "OPEN", {"Status": "Next"},
                 assignees=["enjay27"]),
        )])

    def test_archived_items_are_skipped(self):
        self.assertEqual(len(self.items), 6)

    def test_open_means_open_or_draft_and_not_done(self):
        open_refs = sorted(i.ref() for i in self.items if i.is_open)
        self.assertEqual(open_refs, ["draft", "lakehouse#1", "lakehouse#2", "lakehouse#6"])

    def test_assignees_are_read(self):
        by_ref = {i.ref(): i.assignees for i in self.items}
        self.assertEqual(by_ref["lakehouse#6"], ["enjay27"])
        self.assertEqual(by_ref["lakehouse#1"], [])
        self.assertEqual(by_ref["draft"], [])

    def test_items_query_asks_for_assignees(self):
        self.assertIn("assignees", pb.ITEMS_QUERY)

    def test_render(self):
        when = dt.datetime(2026, 10, 8, 0, 17, tzinfo=dt.timezone.utc)
        text = pb.render_status(self.items, "Lakehouse", when, "https://github.com/orgs/kade-k8s-platform/projects/1")
        self.assertIn("Generated 2026-10-08 00:17 UTC (2026-10-08 09:17 KST)", text)
        self.assertIn("## Now (1)", text)
        self.assertIn("- [lakehouse#1](https://github.com/kade-k8s-platform/lakehouse/issues/1)"
                      " Datahub ingests from Polaris · P1 · target 2026-10-20", text)
        # a decision is an open item assigned to the maintainer; a closed one is not waiting
        self.assertIn("## Waiting on Kade (1)", text)
        self.assertIn("lakehouse#6", text.split("## Waiting on Kade")[1].split("##")[0])
        # closed but still waiting for a check on a Linux test cluster
        self.assertIn("## Needs Linux test cluster (1)", text)
        self.assertIn("lakehouse#3", text.split("## Needs Linux test cluster")[1].split("##")[0])
        self.assertNotIn("Needs Windows", text)
        self.assertIn("## NOT VERIFIED (1)", text)
        # pipes in titles cannot break Markdown tables or lists
        self.assertIn("Heap share / knee", text)
        # the draft has no status, so it is backlog
        self.assertIn("## Backlog (1)", text)
        self.assertIn("- draft Look at the console later", text)

    def test_empty_project_renders(self):
        text = pb.render_status([], "Lakehouse", dt.datetime(2026, 10, 8, tzinfo=dt.timezone.utc))
        self.assertIn("## Now (0)", text)
        self.assertIn("## Waiting on Kade (0)", text)
        self.assertIn("## Needs Linux test cluster (0)", text)
        self.assertIn("- none", text)


class SyncCommand(unittest.TestCase):
    """cmd_sync end to end, with GitHub replaced by a recorder."""

    def run_sync(self, env, writer=True):
        calls = []

        class FakeGitHub:
            def __init__(self, token):
                self.token = token

            def graphql(self, query, variables):
                calls.append(("graphql", query.split("(")[0].split()[-1], variables))
                if "addProjectV2ItemById" in query:
                    return {"addProjectV2ItemById": {"item": {"id": "ITEM_1"}}}
                if "updateProjectV2ItemFieldValue" in query:
                    return {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "ITEM_1"}}}
                return {"organization": {"projectV2": {"id": "PVT_1", "fields": {"nodes": [
                    {"id": "F_s", "name": "Status", "options": [{"id": "o_now", "name": "Now"}]},
                    {"id": "F_p", "name": "Priority", "options": [{"id": "o_p1", "name": "P1"}]},
                ]}}}}

            def rest(self, method, path, body=None):
                calls.append((method, path))
                if path.endswith("/permission"):
                    return {"permission": "write" if writer else "read", "role_name": "write" if writer else "read"}
                return None

        base = {
            "REPO": "kade-k8s-platform/lakehouse", "ISSUE_NUMBER": "7", "ISSUE_NODE_ID": "I_7", "ORG": "kade-k8s-platform",
            "PROJECT_NUMBER": "1", "PROJECT_TOKEN": "p", "GITHUB_TOKEN": "g", "SENDER": "enjay27",
        }
        base.update(env)
        with mock.patch.object(pb, "GitHub", FakeGitHub), mock.patch.dict(os.environ, base, clear=True), \
                mock.patch("builtins.print"):
            code = pb.cmd_sync(None)
        return code, calls

    def test_opened_with_commands_sets_fields_then_removes_labels(self):
        code, calls = self.run_sync({
            "EVENT_ACTION": "opened",
            "ISSUE_LABELS": json.dumps(["cmd:status-now", "cmd:priority-p1", "bug"]),
        })
        self.assertEqual(code, 0)
        sets = [c[2]["option"] for c in calls if c[0] == "graphql" and "option" in c[2]]
        self.assertEqual(sorted(sets), ["o_now", "o_p1"])
        deletes = [c[1] for c in calls if c[0] == "DELETE"]
        self.assertEqual(deletes, [
            "/repos/kade-k8s-platform/lakehouse/issues/7/labels/cmd%3Astatus-now",
            "/repos/kade-k8s-platform/lakehouse/issues/7/labels/cmd%3Apriority-p1",
        ])
        # field updates happen before any label is removed
        first_delete = next(i for i, c in enumerate(calls) if c[0] == "DELETE")
        last_set = max(i for i, c in enumerate(calls) if c[0] == "graphql" and "option" in c[2])
        self.assertLess(last_set, first_delete)

    def test_outsider_is_added_but_commands_are_stripped(self):
        code, calls = self.run_sync({
            "EVENT_ACTION": "opened", "ISSUE_LABELS": json.dumps(["cmd:status-now"]),
        }, writer=False)
        self.assertEqual(code, 0)
        self.assertFalse([c for c in calls if c[0] == "graphql" and "option" in c[2]])
        self.assertTrue([c for c in calls if c[0] == "graphql" and c[2].get("content") == "I_7"])
        self.assertEqual([c[1] for c in calls if c[0] == "DELETE"],
                         ["/repos/kade-k8s-platform/lakehouse/issues/7/labels/cmd%3Astatus-now"])


def labeled(label, actor):
    return {"event": "labeled", "label": {"name": label}, "actor": {"login": actor}}


class BackfillPlan(unittest.TestCase):
    def test_labelers_keep_the_latest_actor_in_labelling_order(self):
        events = [
            labeled("cmd:status-next", "enjay27"),
            labeled("cmd:priority-p2", "outsider"),
            {"event": "unlabeled", "label": {"name": "cmd:status-next"}, "actor": {"login": "enjay27"}},
            {"event": "closed", "actor": {"login": "enjay27"}},
            labeled("cmd:status-now", "enjay27"),
            labeled("cmd:priority-p2", "enjay27"),
            {"event": "labeled", "label": {"name": "bug"}, "actor": None},
        ]
        actors = pb.labelers(events)
        self.assertEqual(
            list(actors.items()),
            [("cmd:status-next", "enjay27"), ("cmd:status-now", "enjay27"),
             ("cmd:priority-p2", "enjay27"), ("bug", "")],
        )

    def test_plan_backfill_honours_only_labels_a_writer_added(self):
        plan = pb.plan_backfill(
            ["bug", "cmd:priority-p1", "cmd:status-now", "cmd:verify-needs-linux", "cmd:status-someday"],
            {"cmd:status-now": "enjay27", "cmd:priority-p1": "outsider", "cmd:status-someday": "enjay27"},
            {"enjay27"}.__contains__,
        )
        self.assertEqual(plan.apply, ["cmd:status-now"])
        # added by someone without write access, or by nobody the events record
        self.assertEqual(sorted(plan.strip), ["cmd:priority-p1", "cmd:verify-needs-linux"])
        self.assertEqual(plan.unknown, ["cmd:status-someday"])

    def test_plan_backfill_orders_commands_by_when_they_were_added(self):
        # the issue lists its labels alphabetically; the later label wins, as in sync
        plan = pb.plan_backfill(
            ["cmd:status-next", "cmd:status-now"],
            {"cmd:status-now": "enjay27", "cmd:status-next": "enjay27"},
            {"enjay27"}.__contains__,
        )
        self.assertEqual(plan.apply, ["cmd:status-now", "cmd:status-next"])
        self.assertEqual(pb.last_wins(plan.apply), {"Status": "Next"})


class BackfillCommand(unittest.TestCase):
    """cmd_backfill end to end, with GitHub replaced by a recorder."""

    ISSUES = [
        {"number": 9, "node_id": "I_9", "title": "secret title",
         "labels": [{"name": "bug"}, {"name": "cmd:status-now"}]},
        {"number": 10, "node_id": "PR_10", "title": "a PR", "labels": [], "pull_request": {}},
        {"number": 11, "node_id": "I_11", "title": "secret title", "labels": []},
        {"number": 12, "node_id": "I_12", "title": "secret title",
         "labels": [{"name": "cmd:verify-needs-linux"}, {"name": "cmd:status-someday"}]},
    ]
    EVENTS = {
        9: [labeled("cmd:status-now", "enjay27")],
        12: [labeled("cmd:verify-needs-linux", "outsider"), labeled("cmd:status-someday", "enjay27")],
    }

    def run_backfill(self, argv):
        calls, printed = [], []
        issues, events = self.ISSUES, self.EVENTS

        class FakeGitHub:
            def __init__(self, token):
                self.token = token

            def graphql(self, query, variables):
                calls.append(("graphql", variables))
                if "addProjectV2ItemById" in query:
                    return {"addProjectV2ItemById": {"item": {"id": "ITEM_" + variables["content"]}}}
                if "updateProjectV2ItemFieldValue" in query:
                    return {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": variables["item"]}}}
                return {"organization": {"projectV2": {"id": "PVT_9", "fields": {"nodes": [
                    {"id": "F_s", "name": "Status", "options": [{"id": "o_now", "name": "Now"}]},
                ]}}}}

            def rest(self, method, path, body=None):
                calls.append((method, path))
                if path.endswith("/permission"):
                    writer = path.endswith("/collaborators/enjay27/permission")
                    return {"permission": "write" if writer else "read", "role_name": "write" if writer else "read"}
                if "/issues?" in path:
                    return issues if path.endswith("&page=1") else []
                if "/events?" in path:
                    number = int(path.split("/issues/")[1].split("/")[0])
                    return events.get(number, []) if path.endswith("&page=1") else []
                return None

        env = {"GH_TOKEN": "t"}
        with mock.patch.object(pb, "GitHub", FakeGitHub), mock.patch.dict(os.environ, env, clear=True), \
                mock.patch("builtins.print", lambda *a, **k: printed.append(" ".join(map(str, a)))):
            code = pb.main(argv)
        return code, calls, "\n".join(printed)

    def test_adds_open_issues_applies_writers_commands_and_removes_them(self):
        code, calls, out = self.run_backfill(
            ["backfill", "--org", "kade-k8s-platform", "--project", "9", "--repo", "kade-k8s-platform/lakehouse"])
        self.assertEqual(code, 0)
        added = [c[1]["content"] for c in calls if c[0] == "graphql" and "content" in c[1]]
        self.assertEqual(added, ["I_9", "I_11", "I_12"])  # the pull request is not an issue
        sets = [(c[1]["item"], c[1]["option"]) for c in calls if c[0] == "graphql" and "option" in c[1]]
        self.assertEqual(sets, [("ITEM_I_9", "o_now")])
        deletes = [c[1] for c in calls if c[0] == "DELETE"]
        self.assertEqual(deletes, [
            "/repos/kade-k8s-platform/lakehouse/issues/9/labels/cmd%3Astatus-now",
            "/repos/kade-k8s-platform/lakehouse/issues/12/labels/cmd%3Averify-needs-linux",
        ])
        # an issue with no command labels needs no events call
        self.assertFalse([c for c in calls if c[0] == "GET" and "/issues/11/events" in c[1]])
        self.assertIn("kade-k8s-platform/lakehouse#9", out)
        self.assertIn("cmd:status-someday", out)  # unknown, left in place, reported
        self.assertNotIn("secret title", out)

    def test_dry_run_writes_nothing(self):
        code, calls, out = self.run_backfill(
            ["backfill", "--org", "kade-k8s-platform", "--project", "9", "--repo", "kade-k8s-platform/lakehouse", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertFalse([c for c in calls if c[0] == "DELETE"])
        self.assertFalse([c for c in calls if c[0] == "graphql" and ("content" in c[1] or "option" in c[1])])
        self.assertIn("set Status = Now", out)
        self.assertIn("dry run", out)

    def test_an_option_the_project_lacks_stops_before_anything_is_written(self):
        issues = self.ISSUES + [{"number": 13, "node_id": "I_13", "title": "secret title",
                                 "labels": [{"name": "cmd:priority-p1"}]}]
        events = {**self.EVENTS, 13: [labeled("cmd:priority-p1", "enjay27")]}
        with mock.patch.object(BackfillCommand, "ISSUES", issues), mock.patch.object(BackfillCommand, "EVENTS", events):
            code, calls, out = self.run_backfill(
                ["backfill", "--org", "kade-k8s-platform", "--project", "9", "--repo", "kade-k8s-platform/lakehouse"])
        self.assertEqual(code, 1)
        self.assertFalse([c for c in calls if c[0] == "DELETE"])
        self.assertFalse([c for c in calls if c[0] == "graphql" and ("content" in c[1] or "option" in c[1])])
        self.assertIn("kade-k8s-platform/lakehouse#13", out)
        self.assertIn("Priority", out)

    def test_repo_is_required(self):
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit):
            pb.main(["backfill", "--org", "kade-k8s-platform", "--project", "9"])


if __name__ == "__main__":
    unittest.main()
