"""Checks a repository's Claude configuration stays within its limits.

- `CLAUDE.md` exists and has at most 100 lines (it is loaded in every session).
- Every `.claude/rules/**/*.md` has at most 80 lines and a non-empty `paths:` in its front
  matter, so it loads only when the files it concerns are touched.

Usage: python3 claude_md_check.py [REPO_ROOT] [--claude-md-max N] [--rules-max N]
Exit status 1 lists every problem, one per line.
Ported from stella-rain/.github scripts/claude_md_check.py at 144d317
(MIT License, Copyright (c) 2026 Kade).
"""

import argparse
import os
import sys

CLAUDE_MD_MAX = 100
RULES_MAX = 80


def read_lines(path):
    with open(path, encoding="utf-8") as f:
        return f.read().splitlines()


def front_matter(lines):
    """Lines between an opening `---` on line 1 and the next `---`, or None."""
    if not lines or lines[0].strip() != "---":
        return None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return lines[1:i]
    return None


def paths_problem(lines):
    """None when the front matter has a non-empty `paths:`, else the problem."""
    fm = front_matter(lines)
    if fm is None:
        return "no `paths:` in front matter"
    for i, line in enumerate(fm):
        if not line.startswith("paths:"):
            continue
        inline = line[len("paths:"):].strip()
        if inline and inline not in ("[]", '""', "''"):
            return None
        for item in fm[i + 1:]:
            if not item.startswith((" ", "\t")):
                break
            if item.strip().startswith("-") and item.strip()[1:].strip():
                return None
        return "`paths:` is empty"
    return "no `paths:` in front matter"


def check_repo(root, claude_md_max=CLAUDE_MD_MAX, rules_max=RULES_MAX):
    errors = []
    claude_md = os.path.join(root, "CLAUDE.md")
    if not os.path.isfile(claude_md):
        errors.append("CLAUDE.md: missing")
    else:
        n = len(read_lines(claude_md))
        if n > claude_md_max:
            errors.append(f"CLAUDE.md: {n} lines, limit {claude_md_max}")

    rules_dir = os.path.join(root, ".claude", "rules")
    rule_files = []
    for dirpath, _dirs, files in os.walk(rules_dir):
        rule_files += [os.path.join(dirpath, f) for f in files if f.endswith(".md")]
    for path in sorted(rule_files):
        rel = os.path.relpath(path, root).replace(os.sep, "/")
        lines = read_lines(path)
        if len(lines) > rules_max:
            errors.append(f"{rel}: {len(lines)} lines, limit {rules_max}")
        problem = paths_problem(lines)
        if problem:
            errors.append(f"{rel}: {problem}")
    return errors


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("root", nargs="?", default=".")
    parser.add_argument("--claude-md-max", type=int, default=CLAUDE_MD_MAX)
    parser.add_argument("--rules-max", type=int, default=RULES_MAX)
    args = parser.parse_args(argv)
    errors = check_repo(args.root, args.claude_md_max, args.rules_max)
    for e in errors:
        print(e)
    if not errors:
        print("Claude configuration within limits")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
