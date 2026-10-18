"""Tests for .github/labels.json, which labels.yml syncs to this repository's labels.
Run: python3 -m unittest discover -s scripts"""

import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import project_bridge  # noqa: E402

LABELS = os.path.join(os.path.dirname(__file__), "..", ".github", "labels.json")
# ADR-073 §3: one label per command the bridge knows, and no other; labels are requests.
COMMANDS = {project_bridge.PREFIX + suffix for suffix in project_bridge.COMMANDS}


class Labels(unittest.TestCase):
    def setUp(self):
        with open(LABELS, encoding="utf-8") as f:
            self.labels = json.load(f)

    def test_exactly_the_bridges_commands(self):
        names = [label["name"] for label in self.labels]
        self.assertEqual(len(names), len(set(names)), "duplicate label")
        self.assertEqual(set(names), COMMANDS)
        self.assertEqual(len(names), 11)

    def test_each_description_names_the_field_it_sets(self):
        for label in self.labels:
            field, option = project_bridge.parse_command(label["name"])
            with self.subTest(label=label["name"]):
                self.assertIn(f"{field}: {option}", label["description"])
                self.assertIn("ADR-073", label["description"])

    def test_each_label_has_a_colour_and_a_description(self):
        for label in self.labels:
            with self.subTest(label=label.get("name")):
                self.assertEqual(set(label), {"name", "color", "description"})
                self.assertRegex(label["color"], re.compile(r"^[0-9A-F]{6}$"))
                self.assertTrue(label["description"].strip())
                self.assertLessEqual(len(label["description"]), 100)  # GitHub's limit


if __name__ == "__main__":
    unittest.main()
