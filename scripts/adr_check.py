"""Checks the ADRs in docs/adr against the ADR-000 rules that a script can check.

- File names are `NNN-kebab-case.md`; numbers are unique and sequential from 000.
- The first line is `# ADR-NNN: Title` with the file's number.
- The header (above the first `## `) has a known Status, a `YYYY-MM-DD` Date, and a Related
  line whose ADRs exist (Related is optional); `Superseded by ADR-NNN` names an existing ADR;
  `Amended by ADR-NNN` names an existing ADR whose Related line lists the amended one.
- README.md has one row per ADR with the same title and status, and no row for a missing file.
- Living documents cite no superseded ADR (ADR-000 rule 9): the index outside its rows, the
  root README.md, CLAUDE.md, .claude/ and docs/plans/. A line that also names the ADR now in
  force passes.

Whether a decision needs a Mermaid diagram is a judgement; the `adr` skill asks for it.

Usage: python3 scripts/adr_check.py [ADR_DIR]   (default: docs/adr; the repository root is
two levels above it)
Exit status 1 lists every problem, one per line.
Ported from stella-rain/app scripts/adr_check.py at ebd1081.
"""

import os
import re
import sys

FILE_RE = re.compile(r"^(\d{3})-[a-z0-9]+(?:-[a-z0-9]+)*\.md$")
STATUSES = {"Accepted", "Accepted (details proposed)", "Proposed", "Deferred", "Rejected"}
SUPERSEDED_RE = re.compile(r"^Superseded by ADR-(\d{3})$")
AMENDED_RE = re.compile(r"^Amended by ADR-(\d{3})$")
CITE_RE = re.compile(r"ADR-(\d{3})\b")
LIVING_FILES = ["README.md", "CLAUDE.md"]
LIVING_DIRS = [".claude", os.path.join("docs", "plans")]
DATE_RE = re.compile(r"^- \*\*Date:\*\* \d{4}-\d{2}-\d{2}$")
ROW_RE = re.compile(r"^\|\s*\[(\d{3})\]\(([^)]+)\)\s*\|\s*(.*?)\s*\|\s*(.*?)\s*\|\s*$")


def read_lines(path):
    with open(path, encoding="utf-8") as f:
        return f.read().splitlines()


def header(lines):
    """Lines above the first `## ` heading."""
    out = []
    for line in lines:
        if line.startswith("## "):
            break
        out.append(line)
    return out


def field(lines, name):
    prefix = f"- **{name}:** "
    for line in lines:
        if line.startswith(prefix):
            return line[len(prefix):].strip()
    return None


def check_dir(adr_dir):
    errors = []
    names = sorted(n for n in os.listdir(adr_dir) if n.endswith(".md") and n != "README.md")
    adrs = {}  # number -> list of file names
    for name in names:
        m = FILE_RE.match(name)
        if not m:
            errors.append(f"{name}: file name must be NNN-kebab-case.md")
            continue
        adrs.setdefault(m.group(1), []).append(name)

    for number, files in sorted(adrs.items()):
        if len(files) > 1:
            errors.append(f"ADR-{number}: number used by {len(files)} files")
    if adrs:
        for n in range(int(max(adrs)) + 1):
            if f"{n:03d}" not in adrs:
                errors.append(f"ADR-{n:03d}: missing (numbers must be sequential from 000)")

    meta = {}  # file name -> (title, status)
    related_of = {}  # number -> numbers on its Related line
    amended = []  # (file name, amended number, amending number)
    for number, files in sorted(adrs.items()):
        for name in files:
            lines = read_lines(os.path.join(adr_dir, name))
            head = header(lines)
            title_re = re.compile(rf"^# ADR-{number}: (.+)$")
            m = title_re.match(lines[0]) if lines else None
            title = m.group(1).strip() if m else None
            if not m:
                errors.append(f"{name}: first line must be '# ADR-{number}: Title'")

            status = field(head, "Status")
            if status is None:
                errors.append(f"{name}: no '- **Status:**' line")
            else:
                sup = SUPERSEDED_RE.match(status)
                amd = AMENDED_RE.match(status)
                if sup:
                    if sup.group(1) not in adrs:
                        errors.append(
                            f"{name}: superseded by ADR-{sup.group(1)}, which does not exist")
                elif amd:
                    if amd.group(1) not in adrs:
                        errors.append(
                            f"{name}: amended by ADR-{amd.group(1)}, which does not exist")
                    else:
                        amended.append((name, number, amd.group(1)))
                elif status not in STATUSES:
                    errors.append(f"{name}: unknown status '{status}'")

            if not any(DATE_RE.match(line) for line in head):
                errors.append(f"{name}: no '- **Date:** YYYY-MM-DD' line")

            related = field(head, "Related")
            refs = re.findall(r"ADR-(\d{3})", related) if related else []
            for ref in refs:
                if ref not in adrs:
                    errors.append(f"{name}: related ADR-{ref} does not exist")
            related_of.setdefault(number, set()).update(refs)
            meta[name] = (title, status)

    for name, number, by in amended:
        if number not in related_of.get(by, set()):
            errors.append(
                f"{name}: amended by ADR-{by}, whose Related line does not list ADR-{number}")

    index_path = os.path.join(adr_dir, "README.md")
    rows = {}
    if os.path.exists(index_path):
        for line in read_lines(index_path):
            m = ROW_RE.match(line)
            if m:
                rows[m.group(2)] = (m.group(3), m.group(4))
    else:
        errors.append("README.md: missing")

    for name, (title, status) in meta.items():
        if name not in rows:
            errors.append(f"{name}: no row in README.md")
            continue
        row_title, row_status = rows[name]
        if title is not None and row_title != title:
            errors.append(f"{name}: README.md title '{row_title}' differs from '{title}'")
        if status is not None and row_status != status:
            errors.append(f"{name}: README.md says '{row_status}', the file says '{status}'")
    for target in rows:
        if not os.path.exists(os.path.join(adr_dir, target)):
            errors.append(f"README.md: row links {target}, which does not exist")
    return errors


def in_force(adr_dir):
    """Superseded ADR number -> the ADR now in force, following chains of supersessions."""
    superseded = {}
    for name in os.listdir(adr_dir):
        m = FILE_RE.match(name)
        if not m:
            continue
        sup = SUPERSEDED_RE.match(field(header(read_lines(os.path.join(adr_dir, name))),
                                        "Status") or "")
        if sup:
            superseded[m.group(1)] = sup.group(1)
    out = {}
    for number in superseded:
        current, seen = number, {number}
        while current in superseded and superseded[current] not in seen:
            current = superseded[current]
            seen.add(current)
        out[number] = current
    return out


def living_documents(root, adr_dir):
    """(path, skip index rows) for each living document under root, sorted."""
    docs = [(os.path.join(adr_dir, "README.md"), True)]
    docs += [(os.path.join(root, f), False) for f in LIVING_FILES]
    for d in LIVING_DIRS:
        for dirpath, _, files in os.walk(os.path.join(root, d)):
            docs += [(os.path.join(dirpath, f), False) for f in files if f.endswith(".md")]
    return sorted((p, skip) for p, skip in docs if os.path.isfile(p))


def check_citations(root, adr_dir):
    """Living documents cite the ADR in force, not one it superseded."""
    successor = in_force(adr_dir)
    errors = []
    for path, skip_rows in living_documents(root, adr_dir):
        rel = os.path.relpath(path, root).replace(os.sep, "/")
        for lineno, line in enumerate(read_lines(path), start=1):
            if skip_rows and ROW_RE.match(line):
                continue
            cited = CITE_RE.findall(line)
            for ref in dict.fromkeys(cited):
                if ref in successor and successor[ref] not in cited:
                    errors.append(f"{rel}:{lineno}: cites superseded ADR-{ref}; "
                                  f"cite ADR-{successor[ref]}")
    return errors


def main(argv):
    adr_dir = argv[1] if len(argv) > 1 else os.path.join("docs", "adr")
    root = os.path.dirname(os.path.dirname(os.path.abspath(adr_dir)))
    errors = check_dir(adr_dir) + check_citations(root, adr_dir)
    for e in errors:
        print(e)
    if not errors:
        print("ADRs follow ADR-000")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
