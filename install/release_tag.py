"""Release tags (ADR-080, ADR-051): their names, the checks a candidate reads, and the notes.

A release is the tag `platform-YYYY.MM.DD`, `.N` appended (from 2) for a later release the
same day. A candidate of it is `platform-YYYY.MM.DD-rc.N` on the same `main` commit, with the
branch `rc/<rc tag>` for Promote to run from; a candidate tag is never a release, so a date with
only candidates is still free. The gates upgrade from the latest release the commit they test
contains (gates/gate.py).

Usage:
  python3 install/release_tag.py previous [TAG...]
      prints the latest release among TAGS, or nothing.
  python3 install/release_tag.py next YYYY-MM-DD [TAG...]
      prints the name of the next release on that date.
  python3 install/release_tag.py rc FINAL [TAG...]
      prints the next candidate tag of the release FINAL.
  python3 install/release_tag.py check-candidate BRANCH
      exits 1 unless BRANCH is candidate/<12 characters of HEAD>, HEAD is on origin/main and
      origin has no other candidate/* branch.
  python3 install/release_tag.py checks-passed RUN_ID
      reads a commit's check runs (the API's JSON) on stdin; exits 1 with the problems unless
      all finished, none failed and static succeeded, leaving out run RUN_ID's own jobs and
      the candidate workflows' jobs.
  python3 install/release_tag.py should-candidate
      prints true when HEAD carries no platform-* tag (final or candidate) and a file under
      charts/, images/ or install/ changed since the latest release (every file when there is
      none); else false, the reason on stderr. Those paths are what install/release.py
      installs (auto-candidate.yml, ADR-080).
  python3 install/release_tag.py ci-done NAME...
      reads a commit's workflow runs (the API's JSON) on stdin; prints red when a run of a
      workflow NAME finished other than success, skipped or neutral, else waiting when one is
      unfinished or none exists, else ready.
  python3 install/release_tag.py check-promote RC_TAG [TAG...]
      prints the final tag; exits 1 unless RC_TAG is the highest candidate of a release that
      does not exist yet and is above the latest release.
  python3 install/release_tag.py rc-branch BRANCH
      prints the rc tag of a branch rc/<rc tag>.
  python3 install/release_tag.py superseded RC_TAG [BRANCH...]
      prints the rc/ branches among BRANCHES of RC_TAG's release below it.
  python3 install/release_tag.py released FINAL [BRANCH...]
      prints the rc/ branches among BRANCHES of FINAL's release and every older one.
  python3 install/release_tag.py notes TAG COMMIT PREVIOUS|- BRANCH CHECK_RUNS_JSON LOG_FILE
      prints the candidate's release notes; LOG_FILE holds one commit per line since PREVIOUS.
      The log batch's image is named with DIGEST_PLACEHOLDER for its digest, which the
      candidate workflow's tag job replaces with the digest its image job pushed or found
      (ADR-042, ADR-080).
"""

import json
import os
import re
import subprocess
import sys

import release

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
# Stands for the log batch image's digest in the notes, until the tag job writes it (ADR-042).
DIGEST_PLACEHOLDER = "BATCH_IMAGE_DIGEST"
TAG_RE = re.compile(r"^platform-(\d{4})\.(\d{2})\.(\d{2})(?:\.(\d+))?$")
RC_RE = re.compile(r"^(platform-\d{4}\.\d{2}\.\d{2}(?:\.\d+)?)-rc\.(\d+)$")
RC_BRANCH_RE = re.compile(r"^rc/(platform-\d{4}\.\d{2}\.\d{2}(?:\.\d+)?-rc\.\d+)$")
CANDIDATE_BRANCH_RE = re.compile(r"^candidate/([0-9a-f]{12})$")
DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
# The checks the notes link, from the commit a candidate was made at (ADR-080): static from
# main's own run, the others the gates candidate.yml calls.
GATE_CHECKS = ("static", "dry-run / dry-run", "smoke upgrade / smoke", "smoke fresh / smoke")
# Check runs that are not the commit's CI: the jobs of candidate.yml, candidate-guard.yml and
# auto-candidate.yml (AC4b), from this run or an earlier candidate on the commit.
CANDIDATE_JOBS = frozenset([
    "check the candidate",
    "the log batch's image on GHCR",
    "notes of the candidate",
    "rc tag and prerelease",
    "delete the candidate branch",
    "candidate branch unchanged",
    "decide on a candidate",
    "push the candidate branch",
])
# The jobs candidate.yml calls from its own jobs (workflow `dry-run`, `smoke upgrade`, `smoke
# fresh`): a check run is "<job> / <called job>". Candidate reads their result itself.
CANDIDATE_GATE_PREFIXES = ("dry-run / ", "smoke upgrade / ", "smoke fresh / ")
# What a release ships (install/release.py installs it): a change under these decides a candidate.
SHIPPED_DIRS = ("charts/", "images/", "install/")
# The events whose runs of a workflow are main's CI: a push, a dispatch and, for Static after
# Auto-merge (auto-merge pushes with GITHUB_TOKEN, which starts no push run, ADR-055),
# a workflow_run.
CI_EVENTS = frozenset(["push", "workflow_dispatch", "workflow_run"])
FAILED = frozenset(["failure", "timed_out", "cancelled", "action_required", "stale",
                    "startup_failure"])


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


def _candidates(final, tags):
    return [int(m.group(2)) for m in map(RC_RE.match, tags) if m and m.group(1) == final]


def rc_tag(final, tags):
    """The next candidate tag of the release `final`: `<final>-rc.N`."""
    if not _key(final):
        raise ValueError(f"{final or '(none)'} is not a release tag")
    return f"{final}-rc.{max([0] + _candidates(final, tags)) + 1}"


def check_candidate(branch, head, on_main, others):
    """Refuses unless the branch names HEAD, HEAD is on origin/main and no other candidate
    branch is left over."""
    m = CANDIDATE_BRANCH_RE.match(branch)
    if not m:
        raise ValueError(f"{branch} is not candidate/<12-character sha>")
    if not head.startswith(m.group(1)):
        raise ValueError(f"{branch} names {m.group(1)}, not HEAD {head}")
    if not on_main:
        raise ValueError(f"{head} is not on origin/main")
    if others:
        raise ValueError(f"{', '.join(others)} is left over; Kade deletes it first")


def checks_passed(runs, run_id):
    """Refuses unless every check run on the candidate commit finished and none failed, and
    `static` succeeded. Left out: this run's own jobs, the candidate workflows' jobs from any
    run (CANDIDATE_JOBS) and the gates Candidate calls (CANDIDATE_GATE_PREFIXES)."""
    own = f"/actions/runs/{run_id}/"
    others = [r for r in runs
              if own not in str(r.get("details_url") or "")
              and r["name"] not in CANDIDATE_JOBS
              and not r["name"].startswith(CANDIDATE_GATE_PREFIXES)]
    if not others:
        raise ValueError("no check run on this commit besides this run")
    bad = [f"{r['name']}: {r['conclusion'] if r.get('status') == 'completed' else r.get('status')}"
           for r in others
           if r.get("status") != "completed" or r.get("conclusion") in FAILED]
    if not any(r["name"] == "static" and r.get("conclusion") == "success" for r in others):
        bad.append("static: not reported")
    if bad:
        raise ValueError(f"checks not passed: {'; '.join(bad)}")


def should_candidate(head_tags, final, changed):
    """(True, "") when HEAD has no platform-* tag and a shipped file changed since the release
    `final` (`changed` is every file at HEAD when `final` is None); else (False, reason)."""
    tagged = [t for t in head_tags if _key(t) or RC_RE.match(t)]
    if tagged:
        return False, f"HEAD already carries {', '.join(tagged)}"
    if any(f.startswith(SHIPPED_DIRS) for f in changed):
        return True, ""
    since = f"since {final}" if final else "(no release yet)"
    return False, f"no file under {', '.join(SHIPPED_DIRS)} changed {since}"


def ci_done(runs, names):
    """The state of main's CI from a commit's workflow runs: only runs of the workflows
    `names` by CI_EVENTS count. Others (Auto-merge, Main status) run after them and may
    still be going when the last one finishes."""
    ours = [r for r in runs if r.get("name") in names and r.get("event") in CI_EVENTS]
    if any(r.get("status") == "completed" and r.get("conclusion") in FAILED for r in ours):
        return "red"
    if not ours or any(r.get("status") != "completed" for r in ours):
        return "waiting"
    return "ready"


def check_promote(rc, tags):
    """The final tag of candidate `rc`, refused unless `rc` is the highest candidate of a
    release that does not exist yet and is above the latest release."""
    m = RC_RE.match(rc or "")
    if not m:
        raise ValueError(f"{rc or '(none)'} is not a candidate tag platform-YYYY.MM.DD-rc.N")
    final, n = m.group(1), int(m.group(2))
    highest = max(_candidates(final, tags) + [n])
    if highest > n:
        raise ValueError(f"{final}-rc.{highest} is newer than {rc}")
    if final in tags:
        raise ValueError(f"{final} is already released")
    latest = previous(tags)
    if latest and _key(final) <= _key(latest):
        raise ValueError(f"{final} is not above the latest release {latest}")
    return final


def rc_of_branch(branch):
    """The rc tag of a branch `rc/<rc tag>`, which Candidate makes at the tag's commit for
    Promote to run from (the GitHub app lists branches, not tags)."""
    m = RC_BRANCH_RE.match(branch or "")
    if not m:
        raise ValueError(f"{branch or '(none)'} is not rc/<rc tag>")
    return m.group(1)


def _rc_branches(branches):
    out = []
    for b in branches:
        m = RC_BRANCH_RE.match(b)
        if m:
            tag = RC_RE.match(m.group(1))
            out.append((b, tag.group(1), int(tag.group(2))))
    return out


def superseded(rc, branches):
    """The rc/ branches of `rc`'s release with a lower candidate number."""
    m = RC_RE.match(rc or "")
    if not m:
        raise ValueError(f"{rc or '(none)'} is not a candidate tag")
    return [b for b, final, n in _rc_branches(branches) if final == m.group(1) and n < int(m.group(2))]


def released(final, branches):
    """The rc/ branches of release `final` and every older one."""
    if not _key(final):
        raise ValueError(f"{final or '(none)'} is not a release tag")
    return [b for b, f, _ in _rc_branches(branches) if _key(f) <= _key(final)]


def notes(tag, commit, previous, branch, checks, log, pins, supported, batch_image):
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
        f"Candidate of {RC_RE.match(tag).group(1) if RC_RE.match(tag) else tag} at commit "
        f"`{commit}` on `main`, from `{branch}`.",
        "",
        "## Gates on the commit (ADR-080)",
        "",
    ]
    lines += [f"- [{name}]({by_name[name]['html_url']})" for name in GATE_CHECKS
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


def _git(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True, check=False)


def _check_candidate_here(branch):
    head = _git("rev-parse", "HEAD").stdout.strip()
    on_main = _git("merge-base", "--is-ancestor", head, "origin/main").returncode == 0
    listed = _git("ls-remote", "--heads", "origin", "candidate/*").stdout.splitlines()
    others = [line.split("refs/heads/", 1)[1] for line in listed if "refs/heads/" in line]
    check_candidate(branch, head, on_main, [b for b in others if b != branch])


def _should_candidate_here():
    head_tags = _git("tag", "--points-at", "HEAD").stdout.split()
    final = previous(_git("tag", "--list", "platform-*").stdout.split())
    if final:
        changed = _git("diff", "--name-only", final, "HEAD").stdout.splitlines()
    else:
        changed = _git("ls-files").stdout.splitlines()
    return should_candidate(head_tags, final, changed)


def main(argv):
    try:
        return _main(argv)
    except ValueError as e:
        print(e)
        return 1


def _main(argv):
    cmd, rest = (argv[1] if len(argv) > 1 else ""), argv[2:]
    if cmd == "previous":
        tag = previous(rest)
        if tag:
            print(tag)
        return 0
    if cmd == "next" and rest:
        print(next_tag(rest[0], rest[1:]))
        return 0
    if cmd == "rc" and rest:
        print(rc_tag(rest[0], rest[1:]))
        return 0
    if cmd == "check-candidate" and len(rest) == 1:
        _check_candidate_here(rest[0])
        return 0
    if cmd == "checks-passed" and len(rest) == 1:
        checks_passed(json.load(sys.stdin).get("check_runs") or [], rest[0])
        return 0
    if cmd == "should-candidate" and not rest:
        ok, why = _should_candidate_here()
        if why:
            print(why, file=sys.stderr)
        print("true" if ok else "false")
        return 0
    if cmd == "ci-done" and rest:
        print(ci_done(json.load(sys.stdin).get("workflow_runs") or [], rest))
        return 0
    if cmd == "check-promote" and rest:
        print(check_promote(rest[0], rest[1:]))
        return 0
    if cmd == "rc-branch" and len(rest) == 1:
        print(rc_of_branch(rest[0]))
        return 0
    if cmd == "superseded" and rest:
        print(" ".join(superseded(rest[0], rest[1:])))
        return 0
    if cmd == "released" and rest:
        print(" ".join(released(rest[0], rest[1:])))
        return 0
    if cmd == "notes" and len(rest) == 6:
        tag, commit, prev, branch, checks_path, log_path = rest
        with open(log_path, encoding="utf-8") as f:
            log = f.read().splitlines()
        print(notes(tag, commit, None if prev == "-" else prev, branch, _load(checks_path), log,
                    _load(os.path.join(ROOT, "gates", "pins.json")),
                    _load(os.path.join(HERE, "supported.json")), release.batch_image()),
              end="")
        return 0
    print(__doc__.strip().splitlines()[0], file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
