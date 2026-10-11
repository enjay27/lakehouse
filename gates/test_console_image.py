"""Tests for the Polaris console's image (ADR-028): images/polaris-console and
`gate.py console-image`. Run: python3 -m unittest discover -s gates"""

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import gate  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
IMAGE_DIR = os.path.join(os.path.dirname(HERE), "images", "polaris-console")
SOURCE = {"repository": "https://github.com/apache/polaris-tools.git",
          "commit": "3edb60fcb591e5f0f9f2fbf79ff0f457baae500e", "path": "console"}


def read(name):
    with open(os.path.join(IMAGE_DIR, name), encoding="utf-8") as f:
        return f.read()


class Committed(unittest.TestCase):
    def test_the_source_is_one_commit_of_polaris_tools(self):
        source = gate.console_source()
        self.assertEqual(source["repository"], "https://github.com/apache/polaris-tools.git")
        self.assertRegex(source["commit"], r"^[0-9a-f]{40}$")
        self.assertEqual(source["path"], "console")

    def test_every_base_image_is_pinned_by_digest(self):
        froms = re.findall(r"(?m)^FROM\s+(\S+)", read("Dockerfile"))
        self.assertEqual(len(froms), 2)
        for image in froms:
            with self.subTest(image=image):
                self.assertRegex(image, r"^registry\.access\.redhat\.com/ubi9/[a-z0-9-]+:"
                                        r"[0-9-]+@sha256:[0-9a-f]{64}$")

    def test_dependencies_come_from_the_lockfile_never_resolved_at_build(self):
        steps = re.findall(r"(?m)^(COPY|RUN)\s+(.*)$", read("Dockerfile"))
        runs = [args for kind, args in steps if kind == "RUN"]
        self.assertIn(("COPY", "package.json package-lock.json ./"), steps)
        self.assertTrue(any(r.startswith("npm ci ") and "--ignore-scripts" in r for r in runs))
        self.assertFalse(any("npm install" in r or "npm i " in r for r in runs))

    def test_the_lockfile_pins_every_package_by_sha512_from_the_npm_registry(self):
        lock = json.loads(read("package-lock.json"))
        self.assertEqual(lock["lockfileVersion"], 3)
        packages = {k: v for k, v in lock["packages"].items() if k}
        self.assertGreater(len(packages), 100)
        for name, package in packages.items():
            with self.subTest(package=name):
                self.assertTrue(package["integrity"].startswith("sha512-"))
                self.assertTrue(package["resolved"].startswith("https://registry.npmjs.org/"))


class Commands(unittest.TestCase):
    def test_the_image_is_named_for_its_commit(self):
        self.assertEqual(gate.console_image(SOURCE), "lakehouse/polaris-console:3edb60fcb591")

    def test_fetches_the_commit_builds_with_our_dockerfile_then_loads_it_into_kind(self):
        commands = gate.console_image_commands(SOURCE, "/ctx")
        self.assertEqual(commands[0], ["git", "init", "--quiet", "/ctx"])
        self.assertEqual(commands[1], ["git", "-C", "/ctx", "fetch", "--quiet", "--depth", "1",
                                       SOURCE["repository"], SOURCE["commit"]])
        self.assertEqual(commands[2], ["git", "-C", "/ctx", "-c", "advice.detachedHead=false",
                                       "checkout", "--quiet", "FETCH_HEAD"])
        self.assertEqual(commands[3], [
            "docker", "build", "--file", os.path.join(IMAGE_DIR, "Dockerfile"),
            "--tag", "lakehouse/polaris-console:3edb60fcb591", "/ctx/console"])
        self.assertEqual(commands[4], ["kind", "load", "docker-image",
                                       "lakehouse/polaris-console:3edb60fcb591",
                                       "--name", gate.CLUSTER])

    def test_a_checkout_of_another_commit_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            git = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", d]
            subprocess.run(["git", "init", "--quiet", d], check=True)
            subprocess.run(git + ["commit", "--quiet", "--allow-empty", "-m", "x"], check=True)
            head = subprocess.run(git + ["rev-parse", "HEAD"], capture_output=True, text=True,
                                  check=True).stdout.strip()
            gate.check_commit(d, head)
            with self.assertRaises(ValueError):
                gate.check_commit(d, "0" * 40)
