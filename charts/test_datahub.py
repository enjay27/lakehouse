"""Tests for charts/datahub (ADR-071, ADR-072, ADR-025, ADR-016): upstream's datahub chart 1.1.7,
its release archive with the one line ADR-072 §1 patches, proven against the archive itself,
fetched by the URL and sha256 pinned in gates/pins.json.

Run: python3 -m unittest discover -s charts
Needs helm and kubeconform on PATH (`python3 gates/gate.py tools BIN_DIR helm kubeconform`) and
PyYAML (`pip install --require-hashes -r charts/requirements.txt`). Missing tools fail the run.
"""

import difflib
import io
import os
import sys
import tarfile
import unittest

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "gates"))
import gate  # noqa: E402

CHART = os.path.join(HERE, "datahub")
# Our files beside upstream's.
OURS = {"values-lakehouse.yaml"}
# ADR-072 §1: the label datahub.acryl.io/datahub-version takes the tag up to its first `@`, in
# both copies of datahub-gms's helpers, line 39.
HELPERS = ("charts/datahub-gms/templates/_helpers.tpl",
           "subcharts/datahub-gms/templates/_helpers.tpl")
PATCHED_LINE_NUMBER = 39
UPSTREAM_LINE = "{{- .Values.image.tag | default .Values.global.datahub.version -}}"
PATCHED_LINE = ('{{- .Values.image.tag | default .Values.global.datahub.version '
                '| splitList "@" | first -}}')

ARCHIVE = None


def setUpModule():
    global ARCHIVE
    pin = gate.load_pins()["datahub_chart"]
    data = gate.fetch(pin["url"], pin["sha256"])
    ARCHIVE = {}
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        for member in archive.getmembers():
            if member.isfile():
                top, _, path = member.name.partition("/")
                if top != "datahub":
                    raise AssertionError(f"{member.name}: not under datahub/")
                ARCHIVE[path] = archive.extractfile(member).read()


def chart_files():
    """{path relative to charts/datahub: bytes} for every file but ours."""
    files = {}
    for directory, _, names in os.walk(CHART):
        for name in names:
            path = os.path.relpath(os.path.join(directory, name), CHART).replace(os.sep, "/")
            if path not in OURS:
                with open(os.path.join(directory, name), "rb") as f:
                    files[path] = f.read()
    return files


def changed_lines(old, new):
    """[(line number in old, old line, new line)] for each line replaced one for one; any other
    change (lines added or removed) is returned as (None, old lines, new lines)."""
    a, b = old.decode("utf-8").split("\n"), new.decode("utf-8").split("\n")
    changes = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b).get_opcodes():
        if tag == "equal":
            continue
        if tag == "replace" and i2 - i1 == j2 - j1:
            changes += [(i1 + k + 1, a[i1 + k], b[j1 + k]) for k in range(i2 - i1)]
        else:
            changes.append((None, a[i1:i2], b[j1:j2]))
    return changes


class Upstream(unittest.TestCase):
    def test_the_chart_is_the_pinned_archives_1_1_7_with_datahub_v1_7_0(self):
        with open(os.path.join(CHART, "Chart.yaml"), encoding="utf-8") as f:
            chart = yaml.safe_load(f)
        self.assertEqual((chart["name"], chart["version"], chart["appVersion"]),
                         ("datahub", gate.load_pins()["datahub_chart"]["version"], "v1.7.0"))

    def test_the_chart_has_every_file_of_the_archive_and_no_other(self):
        ours = chart_files()
        self.assertEqual(sorted(set(ARCHIVE) - set(ours)), [], "missing from charts/datahub")
        self.assertEqual(sorted(set(ours) - set(ARCHIVE)), [], "not in the archive")

    def test_every_file_but_the_helpers_is_the_archives_byte_for_byte(self):
        ours = chart_files()
        changed = sorted(p for p in ARCHIVE if p in ours and p not in HELPERS
                         and ours[p] != ARCHIVE[p])
        self.assertEqual(changed, [])

    def test_each_helper_differs_from_the_archives_in_the_patched_line_alone(self):
        ours = chart_files()
        for path in HELPERS:
            with self.subTest(file=path):
                self.assertEqual(changed_lines(ARCHIVE[path], ours[path]),
                                 [(PATCHED_LINE_NUMBER, UPSTREAM_LINE, PATCHED_LINE)])

    def test_changed_lines_finds_a_replaced_line_and_an_added_one(self):
        self.assertEqual(changed_lines(b"a\nb\nc\n", b"a\nB\nc\n"), [(2, "b", "B")])
        self.assertEqual(changed_lines(b"a\nc\n", b"a\nb\nc\n"), [(None, [], ["b"])])
        self.assertEqual(changed_lines(b"a\n", b"a\n"), [])


if __name__ == "__main__":
    unittest.main()
