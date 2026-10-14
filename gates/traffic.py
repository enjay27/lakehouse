"""check-traffic (ADR-034): matches the recorder's records against expectation files.

An expectation file (gates/expectations/*.json) is {"expectations": [...]}; each expectation has
a `name`, `must` ("exist": at least one record matches; "not_exist": none does) and `match`,
conditions that all hold for a matching record. A condition's key is a dotted path into the
record (`request_body.json.input.actor.principal`); its value is the exact value, or
{"regex": PATTERN} for a string field, or {"present": true|false}, which tells a field the
recorder redacted from one that was never sent.

The output names each expectation and how many records matched, never a body (ADR-002). A
failed must-not also names up to five of the records that matched, by the fields in
NAMED_FIELDS only: never a path, a body, a statement or an error message.

A must-not may carry `allow_during_restart`, conditions like `match`'s: while polaris-pg
restarts during the gate's upgrade (`restart`, a pair of ISO times), a record that matches
both is let through, counted and named as allowed. Outside that window, or with no restart,
it fails like any other (Kade, 2026-10-12).
"""

import datetime as dt

import json
import re

MUSTS = ("exist", "not_exist")
# What a failed must-not shows of each matching record: fields that hold no body, statement or
# message (ADR-002).
NAMED_FIELDS = ("time", "proto", "listener", "event", "method", "status", "user", "database",
                "error.severity", "error.sqlstate")
NAMED_MAX = 5
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


def named(record):
    """One record by its NAMED_FIELDS that it has, in that order."""
    return " ".join(f"{path}={value}" for path in NAMED_FIELDS
                    for value in [lookup(record, path)] if value is not _MISSING)


def when(text):
    return dt.datetime.fromisoformat(text.replace("Z", "+00:00"))


def _during(record, restart):
    if restart is None or not isinstance(record.get("time"), str):
        return False
    return when(restart[0]) <= when(record["time"]) <= when(restart[1])


def check(recorded, expectations, restart=None):
    """(problems, one output line per expectation, then the records a failed must-not
    matched or an allowance let through). `restart` is polaris-pg's restart window, or None."""
    problems, lines = [], []
    for e in expectations:
        if e["must"] not in MUSTS:
            raise ValueError(f"{e['name']}: must is {e['must']!r}, not one of {MUSTS}")
        allowance = e.get("allow_during_restart")
        if allowance is not None and e["must"] != "not_exist":
            raise ValueError(f"{e['name']}: only a must-not may carry allow_during_restart")
        found = [r for r in recorded if matches(r, e["match"])]
        allowed = [r for r in found
                   if allowance is not None and matches(r, allowance) and _during(r, restart)]
        found = [r for r in found if not any(r is a for a in allowed)]
        passed = len(found) > 0 if e["must"] == "exist" else not found
        line = f"{'ok  ' if passed else 'FAIL'} {e['name']}: {len(found)} matching records"
        if allowed:
            line += f", {len(allowed)} allowed during the restart"
        lines.append(line)
        lines += [f"     allowed: {named(r)}" for r in allowed[:NAMED_MAX]]
        if not passed:
            problems.append(line)
            lines += [f"     {named(r)}" for r in found[:NAMED_MAX]]
            if len(found) > NAMED_MAX:
                lines.append(f"     and {len(found) - NAMED_MAX} more")
    return problems, lines
