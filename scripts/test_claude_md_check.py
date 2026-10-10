"""Tests for claude_md_check.py. Run: python3 -m unittest discover -s scripts

Ported from stella-rain/.github scripts/test_claude_md_check.py at 144d317
(MIT License, Copyright (c) 2026 Kade).
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import claude_md_check as cc  # noqa: E402

RULE_OK = '---\npaths:\n  - "src/**"\n---\n\n# Rule\n'


class RepoCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, rel, text):
        full = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8", newline="") as f:
            f.write(text)

    def errors(self, **limits):
        return cc.check_repo(self.root, **limits)


class ClaudeMd(RepoCase):
    def test_missing_claude_md_is_an_error(self):
        self.assertEqual(self.errors(), ["CLAUDE.md: missing"])

    def test_at_the_limit_passes(self):
        self.write("CLAUDE.md", "x\n" * 100)
        self.assertEqual(self.errors(), [])

    def test_one_past_the_limit_fails(self):
        self.write("CLAUDE.md", "x\n" * 101)
        self.assertEqual(self.errors(), ["CLAUDE.md: 101 lines, limit 100"])

    def test_missing_final_newline_still_counts_the_last_line(self):
        self.write("CLAUDE.md", "x\n" * 100 + "x")
        self.assertEqual(self.errors(), ["CLAUDE.md: 101 lines, limit 100"])

    def test_crlf_counts_like_lf(self):
        self.write("CLAUDE.md", "x\r\n" * 100)
        self.assertEqual(self.errors(), [])

    def test_limit_is_configurable(self):
        self.write("CLAUDE.md", "x\n" * 11)
        self.assertEqual(self.errors(claude_md_max=10), ["CLAUDE.md: 11 lines, limit 10"])


class Rules(RepoCase):
    def setUp(self):
        super().setUp()
        self.write("CLAUDE.md", "# ok\n")

    def test_rule_with_paths_list_passes(self):
        self.write(".claude/rules/a.md", RULE_OK)
        self.assertEqual(self.errors(), [])

    def test_rule_with_inline_paths_passes(self):
        self.write(".claude/rules/a.md", '---\npaths: ["src/**"]\n---\n# Rule\n')
        self.assertEqual(self.errors(), [])

    def test_rule_without_front_matter_fails(self):
        self.write(".claude/rules/a.md", "# Rule\n")
        self.assertEqual(self.errors(), [".claude/rules/a.md: no `paths:` in front matter"])

    def test_rule_with_paths_only_in_body_fails(self):
        self.write(".claude/rules/a.md", '---\ndescription: x\n---\npaths:\n  - "src/**"\n')
        self.assertEqual(self.errors(), [".claude/rules/a.md: no `paths:` in front matter"])

    def test_rule_with_empty_paths_fails(self):
        self.write(".claude/rules/a.md", "---\npaths:\n---\n# Rule\n")
        self.assertEqual(self.errors(), [".claude/rules/a.md: `paths:` is empty"])

    def test_unclosed_front_matter_fails(self):
        self.write(".claude/rules/a.md", '---\npaths:\n  - "src/**"\n# Rule\n')
        self.assertEqual(self.errors(), [".claude/rules/a.md: no `paths:` in front matter"])

    def test_rule_too_long_fails(self):
        self.write(".claude/rules/a.md", RULE_OK + "x\n" * 80)
        self.assertEqual(self.errors(), [".claude/rules/a.md: 86 lines, limit 80"])

    def test_rules_in_subfolders_are_checked(self):
        self.write(".claude/rules/sub/b.md", "# Rule\n")
        self.assertEqual(self.errors(), [".claude/rules/sub/b.md: no `paths:` in front matter"])

    def test_non_markdown_files_are_ignored(self):
        self.write(".claude/rules/notes.txt", "x\n" * 500)
        self.assertEqual(self.errors(), [])

    def test_errors_are_sorted_and_all_reported(self):
        self.write("CLAUDE.md", "x\n" * 101)
        self.write(".claude/rules/b.md", "# Rule\n")
        self.write(".claude/rules/a.md", "# Rule\n")
        self.assertEqual(
            self.errors(),
            [
                "CLAUDE.md: 101 lines, limit 100",
                ".claude/rules/a.md: no `paths:` in front matter",
                ".claude/rules/b.md: no `paths:` in front matter",
            ],
        )


class Main(RepoCase):
    def test_exit_codes(self):
        self.assertEqual(cc.main([self.root]), 1)
        self.write("CLAUDE.md", "# ok\n")
        self.assertEqual(cc.main([self.root]), 0)


if __name__ == "__main__":
    unittest.main()
