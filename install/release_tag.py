"""Release tags (ADR-033, ADR-051): their names, the gate a release reads, and the notes.

A release is the tag `platform-YYYY.MM.DD`, `.N` appended (from 2) for a later release the
same day. The release workflow tags an `rc` commit only when every check of the release gate
succeeded on the head of the PR merged as that commit; the gates upgrade from the latest
release the commit they test contains (gates/gate.py).

Usage:
  python3 install/release_tag.py previous [TAG...]
      prints the latest release among TAGS, or nothing.
  python3 install/release_tag.py next YYYY-MM-DD [TAG...]
      prints the name of the next release on that date.
  python3 install/release_tag.py verify CHECK_RUNS_JSON
      CHECK_RUNS_JSON is the head commit's check runs (a list of {name, status, conclusion});
      exits 0 when every release-gate check succeeded, else 1 with the problems.
  python3 install/release_tag.py notes TAG COMMIT PREVIOUS|- PR_URL CHECK_RUNS_JSON LOG_FILE
      prints the release notes; LOG_FILE holds one commit per line since PREVIOUS. The log
      batch's image is named with DIGEST_PLACEHOLDER for its digest, which the release
      workflow's tag job replaces with the digest its image job pushed or found (ADR-042).
"""

import json
import os
import re
import sys

import release

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
# Stands for the log batch image's digest in the notes, until the tag job writes it (ADR-042).
DIGEST_PLACEHOLDER = "BATCH_IMAGE_DIGEST"
TAG_RE = re.compile(r"^platform-(\d{4})\.(\d{2})\.(\d{2})(?:\.(\d+))?$")
DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
# The check names the `rc` ruleset requires (ADR-033): release-gate.yml's jobs and static.yml.
REQUIRED_CHECKS = ("the head is main", "static", "dry-run / dry-run",
                   "smoke upgrade / smoke", "smoke fresh / smoke")


def _key(tag):
    m = TAG_RE.match(tag)
    if not m:
        return None
    year, month, day, n = m.groups()
    return int(year), int(month), int(day), int(n) if n else 1


def previous(tags):
    keyed = [(_key(t), t) for t in tags if _key(t)]
    return max(keyed)[1] if keyed else None


def next_tag(date, tags):
    m = DATE_RE.match(date)
    if not m:
        raise ValueError(f"date {date!r} is not YYYY-MM-DD")
    base = "platform-{}.{}.{}".format(*m.groups())
    day = tuple(int(x) for x in m.groups())
    taken = [_key(t)[3] for t in tags if _key(t) and _key(t)[:3] == day]
    return f"{base}.{max(taken) + 1}" if taken else base


def verify(check_runs):
    """Problems with the release-gate checks; a name passes if any of its runs succeeded."""
    by_name = {}
    for run in check_runs:
        by_name.setdefault(run["name"], []).append(run)
    problems = []
    for name in REQUIRED_CHECKS:
        runs = by_name.get(name, [])
        if not runs:
            problems.append(f"{name}: not reported")
        elif not any(r.get("conclusion") == "success" for r in runs):
            last = runs[-1]
            problems.append(f"{name}: {last.get('conclusion') or last.get('status')}")
    return problems


def notes(tag, commit, previous, pr, checks, log, pins, supported, batch_image):
    image = pins["node_image"]["image"]
    kubernetes = image.split(":", 1)[1].split("@", 1)[0]
    chart = pins["cnpg_chart"]
    by_name = {}
    for run in checks:
        if run.get("conclusion") == "success":
            by_name.setdefault(run["name"], run)
    lines = [
        f"# {tag}",
        "",
        f"Commit `{commit}` on `rc`, from {pr}.",
        "",
        "## Release gate (ADR-033)",
        "",
    ]
    lines += [f"- [{name}]({by_name[name]['html_url']})" for name in REQUIRED_CHECKS
              if name in by_name]
    lines += [
        "",
        "## Versions",
        "",
        f"- The log batch's image: `{batch_image}@{DIGEST_PLACEHOLDER}` (ADR-042).",
        f"- Gate: kind on Kubernetes {kubernetes}, node image `{image}`.",
        f"- CloudNativePG on the gate: chart {chart['version']}, operator "
        f"{chart['operator_version']}. Operator versions this release supports: "
        + ", ".join(supported["cnpg_operator"]) + " (ADR-029).",
        "",
        "## Changes",
        "",
    ]
    if previous:
        lines.append(f"Commits since {previous}:")
        lines.append("")
        lines += [f"- {line}" for line in log if line.strip()]
    else:
        lines.append("First release: nothing to upgrade from.")
    return "\n".join(lines) + "\n"


def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def main(argv):
    if len(argv) >= 2 and argv[1] == "previous":
        tag = previous(argv[2:])
        if tag:
            print(tag)
        return 0
    if len(argv) >= 3 and argv[1] == "next":
        print(next_tag(argv[2], argv[3:]))
        return 0
    if len(argv) == 3 and argv[1] == "verify":
        problems = verify(_load(argv[2]))
        for problem in problems:
            print(problem)
        return 1 if problems else 0
    if len(argv) == 8 and argv[1] == "notes":
        tag, commit, prev, pr, checks_path, log_path = argv[2:8]
        with open(log_path, encoding="utf-8") as f:
            log = f.read().splitlines()
        print(notes(tag, commit, None if prev == "-" else prev, pr, _load(checks_path), log,
                    _load(os.path.join(ROOT, "gates", "pins.json")),
                    _load(os.path.join(HERE, "supported.json")), release.batch_image()),
              end="")
        return 0
    print(__doc__.strip().splitlines()[0], file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
