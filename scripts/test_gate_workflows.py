"""Every gate workflow deletes its own gate branch when its run ends (ADR-049). Run:
python3 -m unittest discover -s scripts

A gate workflow is any workflow that a push to `gate/**` triggers (ADR-049 §1). The test keeps
no list of them: it reads every file in .github/workflows/. The scripts/ tests run on the
runner's own Python, without PyYAML, so `read_workflow` reads the subset of YAML these files
use: block mappings, `- ` sequences, flow lists, `{}`, and `|` and `>` blocks.
"""

import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOWS = os.path.join(ROOT, ".github", "workflows")
DELETE = "delete-gate-branch.yml"
DELETE_USES = f"./.github/workflows/{DELETE}"
GUARD = "always() && startsWith(github.ref, 'refs/heads/gate/')"


def _scalar(text):
    text = text.strip()
    if text == "{}":
        return {}
    if text.startswith("[") and text.endswith("]"):
        return [_scalar(x) for x in text[1:-1].split(",") if x.strip()]
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    return text


def _lines(text):
    """(indent, content, block) for each line that is not blank or a comment; a `|` or `>`
    line carries its block's text, and the block's own lines are consumed."""
    out, raw = [], text.splitlines()
    i = 0
    while i < len(raw):
        line = raw[i]
        stripped = line.strip()
        i += 1
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        block = None
        style = re.search(r":\s+([|>])-?$", stripped)
        if style:
            stripped, body = stripped[:style.start() + 1], []
            while i < len(raw) and (not raw[i].strip()
                                    or len(raw[i]) - len(raw[i].lstrip(" ")) > indent):
                body.append(raw[i].rstrip())
                i += 1
            margin = min((len(x) - len(x.lstrip(" ")) for x in body if x), default=0)
            body = [x[margin:] for x in body]
            if style.group(1) == ">":
                body = [x.strip() for x in body]
            block = ("\n" if style.group(1) == "|" else " ").join(body).strip()
        out.append((indent, re.sub(r"\s+#.*$", "", stripped), block))
    return out


def _parse(lines, start, indent):
    """The node at `indent` from lines[start:]; returns (node, next index)."""
    if lines[start][1].startswith("- "):
        items, i = [], start
        while i < len(lines) and lines[i][0] == indent and lines[i][1].startswith("- "):
            rest = lines[i][1][2:]
            if ":" in rest:
                sub = [(indent + 2, rest, lines[i][2])] + lines[i + 1:]
                node, used = _parse(sub, 0, indent + 2)
            else:
                node, used = _scalar(rest), 1
            items.append(node)
            i += used
        return items, i
    mapping, i = {}, start
    while i < len(lines) and lines[i][0] == indent and not lines[i][1].startswith("- "):
        key, _, value = lines[i][1].partition(":")
        key, value = _scalar(key), value.strip()
        if lines[i][2] is not None:
            mapping[key], i = lines[i][2], i + 1
        elif value:
            mapping[key], i = _scalar(value), i + 1
        elif i + 1 < len(lines) and lines[i + 1][0] > indent:
            mapping[key], i = _parse(lines, i + 1, lines[i + 1][0])
        elif i + 1 < len(lines) and lines[i + 1][0] == indent and lines[i + 1][1].startswith("- "):
            mapping[key], i = _parse(lines, i + 1, indent)
        else:
            mapping[key], i = None, i + 1
    return mapping, i


def read_workflow(path):
    with open(path, encoding="utf-8") as f:
        lines = _lines(f.read())
    node, _ = _parse(lines, 0, lines[0][0])
    return node


def workflows():
    return {name: read_workflow(os.path.join(WORKFLOWS, name))
            for name in sorted(os.listdir(WORKFLOWS)) if name.endswith(".yml")}


def push_branches(workflow):
    on = workflow.get("on") or {}
    push = on.get("push") if isinstance(on, dict) else None
    return (push or {}).get("branches") or [] if isinstance(push, dict) else []


def gate_workflows():
    return {name: w for name, w in workflows().items()
            if any(str(b).startswith("gate/") for b in push_branches(w))}


class Reader(unittest.TestCase):
    def test_reads_mappings_lists_and_blocks(self):
        text = ("on:\n  push:\n    branches: [\"gate/x/**\"]  # comment\n"
                "jobs:\n  a:\n    needs: [b, c]\n    steps:\n      - name: one\n"
                "        run: |\n          echo 1\n          echo 2\n      - uses: x@v1\n")
        lines = _lines(text)
        node, _ = _parse(lines, 0, 0)
        self.assertEqual(node["on"]["push"]["branches"], ["gate/x/**"])
        self.assertEqual(node["jobs"]["a"]["needs"], ["b", "c"])
        self.assertEqual(node["jobs"]["a"]["steps"][0]["run"], "echo 1\necho 2")
        self.assertEqual(node["jobs"]["a"]["steps"][1], {"uses": "x@v1"})

    def test_reads_folded_blocks(self):
        lines = _lines("a:\n  if: >-\n    always()\n    && x\n  b: c\n")
        node, _ = _parse(lines, 0, 0)
        self.assertEqual(node, {"a": {"if": "always() && x", "b": "c"}})


class GateWorkflows(unittest.TestCase):
    def test_the_known_gate_workflows_are_found(self):
        # A reader that finds nothing would pass every check below.
        self.assertLessEqual({"dry-run.yml", "smoke.yml", "log-batch.yml"}, set(gate_workflows()))

    def test_every_gate_workflow_ends_with_the_delete_job(self):
        for name, workflow in gate_workflows().items():
            with self.subTest(workflow=name):
                jobs = workflow["jobs"]
                deleting = [j for j, job in jobs.items() if job.get("uses") == DELETE_USES]
                self.assertEqual(len(deleting), 1, "one job calls delete-gate-branch.yml")
                job = jobs[deleting[0]]
                self.assertEqual(job.get("if"), GUARD)
                self.assertEqual(job.get("permissions"), {"contents": "write"})
                others = sorted(j for j in jobs if j != deleting[0])
                self.assertEqual(sorted(job.get("needs") or []), others,
                                 "it needs every other job, so it runs last")

    def test_no_other_gate_job_can_write(self):
        for name, workflow in gate_workflows().items():
            for job_name, job in workflow["jobs"].items():
                if job.get("uses") == DELETE_USES:
                    continue
                with self.subTest(workflow=name, job=job_name):
                    self.assertNotIn("write", (job.get("permissions") or {}).values())

    def test_a_workflow_calling_a_gate_workflow_grants_it_contents_write(self):
        # A called workflow's job gets at most what its caller grants; the release gate calls
        # dry-run.yml and smoke.yml, whose delete job asks for contents: write.
        gates = {f"./.github/workflows/{n}" for n in gate_workflows()}
        for name, workflow in workflows().items():
            for job_name, job in (workflow.get("jobs") or {}).items():
                if job.get("uses") in gates:
                    with self.subTest(workflow=name, job=job_name):
                        self.assertEqual((job.get("permissions") or {}).get("contents"),
                                         "write")


class DeleteWorkflow(unittest.TestCase):
    def setUp(self):
        self.workflow = read_workflow(os.path.join(WORKFLOWS, DELETE))

    def test_it_is_only_called(self):
        self.assertEqual(list(self.workflow["on"]), ["workflow_call"])
        self.assertEqual(self.workflow["permissions"], {})

    def test_one_job_writes_contents_without_a_checkout(self):
        (job,) = self.workflow["jobs"].values()
        self.assertEqual(job["permissions"], {"contents": "write"})
        self.assertFalse(any("actions/checkout" in str(s.get("uses", "")) for s in job["steps"]))

    def test_it_deletes_only_its_own_commits_gate_branch_and_never_fails(self):
        (job,) = self.workflow["jobs"].values()
        script = "\n".join(s.get("run", "") for s in job["steps"])
        self.assertIn('refs/heads/gate/*)', script)
        self.assertIn('--force-with-lease="$REF:$SHA"', script)
        self.assertNotIn("exit 1", script)
        self.assertIn("::warning::", script)


if __name__ == "__main__":
    unittest.main()
