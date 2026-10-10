"""Tests for .github/labels.json, which labels.yml syncs to this repository's labels.
Run: python3 -m unittest discover -s scripts"""

import json
import os
import re
import unittest

LABELS = os.path.join(os.path.dirname(__file__), "..", ".github", "labels.json")
# ADR-003: the state labels, with the same names as Stella Rain's.
ADR_003 = {"cmd:status-now", "cmd:verify-not-verified", "cmd:verify-needs-linux"}


class Labels(unittest.TestCase):
    def setUp(self):
        with open(LABELS, encoding="utf-8") as f:
            self.labels = json.load(f)

    def test_exactly_the_adr_003_labels(self):
        names = [label["name"] for label in self.labels]
        self.assertEqual(len(names), len(set(names)), "duplicate label")
        self.assertEqual(set(names), ADR_003)

    def test_each_label_has_a_colour_and_a_description(self):
        for label in self.labels:
            with self.subTest(label=label.get("name")):
                self.assertEqual(set(label), {"name", "color", "description"})
                self.assertRegex(label["color"], re.compile(r"^[0-9A-F]{6}$"))
                self.assertTrue(label["description"].strip())
                self.assertLessEqual(len(label["description"]), 100)  # GitHub's limit


if __name__ == "__main__":
    unittest.main()
