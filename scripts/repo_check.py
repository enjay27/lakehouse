"""Checks the files git tracks for what a public repository must never hold (ADR-002, ADR-006).

- No environment file but `.env.template`; in it, every credential variable is empty.
- No key or keystore file (`*.pem`, `*.key`, `*.p12`, `*.pfx`, `*.jks`, `*.keystore`, `id_rsa`
  and the like).
- No private key block, AWS access key ID or GitHub token in any text file.
- No CRLF line endings, and `.gitattributes` keeps `* text=auto eol=lf`.

Hostnames and employer names are not pattern-checked: a list of them would publish them.
Findings name the file, line and kind, never the matched value.

Usage: python3 scripts/repo_check.py [REPO_ROOT]   (default: the current directory)
Exit status 1 lists every problem, one per line.
"""

import os
import re
import subprocess
import sys

TEMPLATE = ".env.template"
KEY_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".jks", ".keystore")
KEY_NAMES = {"id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"}
CREDENTIAL_RE = re.compile(r"(USER|PASSWORD|SECRET|TOKEN|KEY|KEY_ID|CLIENT_ID)$")
ASSIGNMENT_RE = re.compile(r"^([A-Z_][A-Z0-9_]*)=(.*)$")
SECRET_PATTERNS = [
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("AWS access key ID", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("GitHub token",
     re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})\b")),
]
EOL_LINE = "* text=auto eol=lf"


def tracked_files(root):
    """Paths git tracks under root that exist in the working tree."""
    out = subprocess.run(["git", "-C", root, "ls-files", "-z"], check=True,
                         capture_output=True).stdout.decode("utf-8")
    return [p for p in out.split("\0") if p and os.path.isfile(os.path.join(root, p))]


def is_env_file(name):
    return name != TEMPLATE and (name == ".env" or name.startswith(".env."))


def is_key_file(name):
    return name in KEY_NAMES or name.endswith(KEY_SUFFIXES)


def template_problems(rel, text):
    errors = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        m = ASSIGNMENT_RE.match(line.strip())
        if not m or not CREDENTIAL_RE.search(m.group(1)):
            continue
        if m.group(2).strip().strip("\"'"):
            errors.append(f"{rel}:{lineno}: {m.group(1)} has a value; "
                          "credentials stay empty (ADR-006)")
    return errors


def content_problems(rel, text):
    errors = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for kind, pattern in SECRET_PATTERNS:
            if pattern.search(line):
                errors.append(f"{rel}:{lineno}: {kind} (ADR-006)")
    return errors


def check_repo(root, files):
    errors = []
    for rel in sorted(files):
        name = rel.rsplit("/", 1)[-1]
        if is_env_file(name):
            errors.append(f"{rel}: environment file is committed (ADR-002)")
        if is_key_file(name):
            errors.append(f"{rel}: key or keystore file is committed (ADR-006)")
        with open(os.path.join(root, rel), "rb") as f:
            data = f.read()
        if b"\0" in data[:8000]:
            continue  # binary
        text = data.decode("utf-8", errors="replace")
        if rel == TEMPLATE:
            errors += template_problems(rel, text)
        errors += content_problems(rel, text)
        if b"\r\n" in data:
            errors.append(f"{rel}: CRLF line endings (.gitattributes)")

    attributes = os.path.join(root, ".gitattributes")
    lines = []
    if ".gitattributes" in files and os.path.isfile(attributes):
        with open(attributes, encoding="utf-8") as f:
            lines = [line.strip() for line in f]
    if EOL_LINE not in lines:
        errors.append(f".gitattributes: no `{EOL_LINE}` line")
    return errors


def main(argv):
    root = argv[1] if len(argv) > 1 else "."
    errors = check_repo(root, tracked_files(root))
    for e in errors:
        print(e)
    if not errors:
        print("Repository checks pass")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
