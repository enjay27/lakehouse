"""Tests for adr_check.py. Run: python3 -m unittest discover -s scripts

Ported from stella-rain/app scripts/test_adr_check.py at ebd1081.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import adr_check as ac  # noqa: E402


def adr(number, title, status="Accepted", date="2026-10-08", related=None, body=""):
    lines = [f"# ADR-{number}: {title}", "", f"- **Status:** {status}"]
    if date is not None:
        lines.append(f"- **Date:** {date}")
    if related is not None:
        lines.append(f"- **Related:** {related}")
    return "\n".join(lines) + "\n\n## Context\n\nText.\n" + body


class AdrCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self.dir = os.path.join(self.root, "docs", "adr")
        os.makedirs(self.dir)
        self.index_rows = []

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, name, text, base=None):
        path = os.path.join(base or self.dir, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(text)

    def add(self, name, title, status="Accepted", index_status=None, index_title=None, **kw):
        self.write(name, adr(name[:3], title, status=status, **kw))
        self.index_rows.append(
            f"| [{name[:3]}]({name}) | {index_title or title} | {index_status or status} |")

    def errors(self, index_text=""):
        index = "# ADRs\n\n| # | Title | Status |\n|---|---|---|\n" + "\n".join(self.index_rows) + "\n"
        self.write("README.md", index + index_text)
        return ac.check_dir(self.dir)

    def citation_errors(self, index_text=""):
        self.errors(index_text)
        return ac.check_citations(self.root, self.dir)


class Valid(AdrCase):
    def test_a_consistent_set_passes(self):
        self.add("000-process.md", "Process")
        self.add("001-engine.md", "Engine: Godot 4", status="Proposed", related="ADR-000")
        self.assertEqual(self.errors(), [])

    def test_every_allowed_status_passes(self):
        statuses = ["Accepted", "Accepted (details proposed)", "Proposed", "Deferred",
                    "Rejected", "Superseded by ADR-000"]
        for i, s in enumerate(statuses, start=1):
            self.add(f"{i:03d}-s{i}.md", f"S{i}", status=s)
        self.add("000-base.md", "Base")
        self.assertEqual(self.errors(), [])

    def test_template_placeholders_below_the_header_are_ignored(self):
        self.add("000-process.md", "Process", body="\n```\n- **Related:** ADR-xxx, ADR-999\n```\n")
        self.assertEqual(self.errors(), [])


class FileNames(AdrCase):
    def test_bad_file_name_fails(self):
        self.add("000-process.md", "Process")
        self.write("001_Engine.md", adr("001", "Engine"))
        self.assertIn("001_Engine.md: file name must be NNN-kebab-case.md", self.errors())

    def test_duplicate_number_fails(self):
        self.add("000-process.md", "Process")
        self.add("001-engine.md", "Engine")
        self.add("001-other.md", "Other")
        self.assertIn("ADR-001: number used by 2 files", self.errors())

    def test_gap_in_numbers_fails(self):
        self.add("000-process.md", "Process")
        self.add("002-engine.md", "Engine")
        self.assertIn("ADR-001: missing (numbers must be sequential from 000)", self.errors())


class Header(AdrCase):
    def test_title_number_must_match_file(self):
        self.write("000-process.md", adr("007", "Process"))
        self.index_rows.append("| [000](000-process.md) | Process | Accepted |")
        self.assertIn("000-process.md: first line must be '# ADR-000: Title'", self.errors())

    def test_unknown_status_fails(self):
        self.add("000-process.md", "Process", status="Done")
        self.assertIn("000-process.md: unknown status 'Done'", self.errors())

    def test_superseded_by_a_missing_adr_fails(self):
        self.add("000-process.md", "Process", status="Superseded by ADR-005")
        self.assertIn("000-process.md: superseded by ADR-005, which does not exist", self.errors())

    def test_missing_status_fails(self):
        self.write("000-process.md", "# ADR-000: Process\n\n- **Date:** 2026-10-08\n")
        self.index_rows.append("| [000](000-process.md) | Process | Accepted |")
        self.assertIn("000-process.md: no '- **Status:**' line", self.errors())

    def test_missing_or_malformed_date_fails(self):
        self.add("000-process.md", "Process", date=None)
        self.add("001-engine.md", "Engine", date="8 Oct 2026")
        errors = self.errors()
        self.assertIn("000-process.md: no '- **Date:** YYYY-MM-DD' line", errors)
        self.assertIn("001-engine.md: no '- **Date:** YYYY-MM-DD' line", errors)

    def test_amended_by_an_adr_that_lists_it_passes(self):
        self.add("000-process.md", "Process", status="Amended by ADR-001")
        self.add("001-change.md", "Change", related="ADR-000")
        self.assertEqual(self.errors(), [])

    def test_amended_by_a_missing_adr_fails(self):
        self.add("000-process.md", "Process", status="Amended by ADR-005")
        self.assertIn("000-process.md: amended by ADR-005, which does not exist", self.errors())

    def test_amending_adr_must_list_the_amended_one_as_related(self):
        self.add("000-process.md", "Process", status="Amended by ADR-001")
        self.add("001-change.md", "Change")
        self.assertIn("000-process.md: amended by ADR-001, whose Related line does not list "
                      "ADR-000", self.errors())

    def test_related_adr_must_exist(self):
        self.add("000-process.md", "Process", related="ADR-000, ADR-042")
        self.assertIn("000-process.md: related ADR-042 does not exist", self.errors())


class Index(AdrCase):
    def test_adr_missing_from_index_fails(self):
        self.add("000-process.md", "Process")
        self.write("001-engine.md", adr("001", "Engine"))
        self.assertIn("001-engine.md: no row in README.md", self.errors())

    def test_index_status_must_match(self):
        self.add("000-process.md", "Process", status="Accepted", index_status="Proposed")
        self.assertIn("000-process.md: README.md says 'Proposed', the file says 'Accepted'",
                      self.errors())

    def test_index_title_must_match(self):
        self.add("000-process.md", "Process", index_title="Old title")
        self.assertIn("000-process.md: README.md title 'Old title' differs from 'Process'",
                      self.errors())

    def test_index_row_for_a_missing_file_fails(self):
        self.add("000-process.md", "Process")
        self.index_rows.append("| [001](001-gone.md) | Gone | Accepted |")
        self.assertIn("README.md: row links 001-gone.md, which does not exist", self.errors())


class Citations(AdrCase):
    """Living documents cite the ADR in force, not one it superseded."""

    def chain(self):
        # 001 -> 002 -> 003: 003 is in force for both.
        self.add("000-process.md", "Process", status="Amended by ADR-004")
        self.add("001-old.md", "Old", status="Superseded by ADR-002")
        self.add("002-mid.md", "Mid", status="Superseded by ADR-003")
        self.add("003-new.md", "New")
        self.add("004-change.md", "Change", related="ADR-000")

    def test_citing_a_superseded_adr_in_a_plan_fails_and_names_the_adr_in_force(self):
        self.chain()
        self.write("docs/plans/plan.md", "# Plan\n\nDecision: ADR-001.\n", base=self.root)
        self.assertEqual(self.citation_errors(), [
            "docs/plans/plan.md:3: cites superseded ADR-001; cite ADR-003"])

    def test_every_living_document_is_checked(self):
        self.chain()
        for path in ["CLAUDE.md", ".claude/rules/r.md", ".claude/skills/s/SKILL.md",
                     "docs/setup/run.md"]:
            self.write(path, "See ADR-002.\n", base=self.root)
        self.assertEqual(sorted(self.citation_errors()), sorted([
            ".claude/rules/r.md:1: cites superseded ADR-002; cite ADR-003",
            ".claude/skills/s/SKILL.md:1: cites superseded ADR-002; cite ADR-003",
            "CLAUDE.md:1: cites superseded ADR-002; cite ADR-003",
            "docs/setup/run.md:1: cites superseded ADR-002; cite ADR-003"]))

    def test_index_text_is_checked_but_its_rows_are_not(self):
        self.chain()
        errors = self.citation_errors("\n- Roadmap: ADR-001.\n")
        self.assertEqual(errors, ["docs/adr/README.md:11: cites superseded ADR-001; cite ADR-003"])

    def test_a_line_that_also_names_the_adr_in_force_passes(self):
        self.chain()
        self.write("docs/setup/run.md", "Setup for ADR-003 (the bridge of ADR-001).\n",
                   base=self.root)
        self.assertEqual(self.citation_errors(), [])

    def test_amended_and_accepted_adrs_may_be_cited(self):
        self.chain()
        self.write("CLAUDE.md", "ADR-000, ADR-003 and ADR-004.\n", base=self.root)
        self.assertEqual(self.citation_errors(), [])

    def test_adrs_and_other_files_are_not_checked(self):
        self.chain()
        self.write("docs/other.md", "ADR-001\n", base=self.root)
        self.write("005-later.md", adr("005", "Later", body="Replaces ADR-001.\n"))
        self.index_rows.append("| [005](005-later.md) | Later | Accepted |")
        self.assertEqual(self.citation_errors(), [])


class RealRepository(unittest.TestCase):
    def test_this_repositorys_adrs_pass(self):
        adr_dir = os.path.join(os.path.dirname(__file__), "..", "docs", "adr")
        self.assertEqual(ac.check_dir(adr_dir), [])

    def test_this_repositorys_living_documents_cite_adrs_in_force(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        self.assertEqual(ac.check_citations(root, os.path.join(root, "docs", "adr")), [])


if __name__ == "__main__":
    unittest.main()
