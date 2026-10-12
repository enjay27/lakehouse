"""check-traffic (ADR-034): matches the recorder's records against expectation files.

An expectation file (gates/expectations/*.json) is {"expectations": [...]}; each expectation has
a `name`, `must` ("exist": at least one record matches; "not_exist": none does) and `match`,
conditions that all hold for a matching record. A condition's key is a dotted path into the
record (`request_body.json.input.actor.principal`); its value is the exact value, or
{"regex": PATTERN} for a string field, or {"present": true|false}, which tells a field the
recorder redacted from one that was never sent.

The output names each expectation and how many records matched, never a body (ADR-002).
"""

import json
import re

MUSTS = ("exist", "not_exist")
_MISSING = object()


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)["expectations"]


def records(text):
    """The JSON object lines of the recorder's output; anything else is skipped."""
    out = []
    for line in text.splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            out.append(record)
    return out


def lookup(record, path):
    value = record
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return _MISSING
        value = value[part]
    return value


def _holds(value, want):
    if isinstance(want, dict) and set(want) == {"present"}:
        return (value is not _MISSING) == want["present"]
    if value is _MISSING:
        return False
    if isinstance(want, dict) and set(want) == {"regex"}:
        return isinstance(value, str) and re.search(want["regex"], value) is not None
    return type(value) is type(want) and value == want


def matches(record, match):
    return all(_holds(lookup(record, path), want) for path, want in match.items())


def check(recorded, expectations):
    """(problems, one output line per expectation)."""
    problems, lines = [], []
    for e in expectations:
        if e["must"] not in MUSTS:
            raise ValueError(f"{e['name']}: must is {e['must']!r}, not one of {MUSTS}")
        count = sum(1 for r in recorded if matches(r, e["match"]))
        passed = count > 0 if e["must"] == "exist" else count == 0
        line = f"{'ok  ' if passed else 'FAIL'} {e['name']}: {count} matching records"
        lines.append(line)
        if not passed:
            problems.append(line)
    return problems, lines
