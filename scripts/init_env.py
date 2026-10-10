"""init-env (ADR-006): writes `.env` with credentials generated for this clone, and creates the
Kubernetes Secrets from it.

    python3 scripts/init_env.py                    # write .env only
    python3 scripts/init_env.py --apply-secrets    # also create or update the Secrets

Writing `.env`: starts from `.env.template` when `.env` does not exist, adds every variable the
template has and `.env` lacks, and fills every empty credential this repository creates
(GENERATED) with a new random value: 32 letters and digits, or 16 lowercase letters and digits
for user names and client IDs. Credentials of other teams' services are never generated
(ADR-023); the script lists the ones still empty, to be typed in by hand. A value already in
`.env` is never overwritten, so rerunning is safe. `.env` is written with mode 0600.

Applying the Secrets: stops unless `KUBE_CONTEXT` and every value a Secret needs are set. Each
Secret is applied with its Namespace by `kubectl --context $KUBE_CONTEXT apply --server-side
-n <namespace> -f -` (ADR-004, ADR-007); values travel on stdin, never on the command line,
and server-side apply keeps them out of a last-applied annotation.

Output names variables and Secrets, never a value. Run from the repository root.
"""

import argparse
import base64
import json
import os
import re
import secrets
import string
import subprocess
import sys
from collections import namedtuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from repo_check import ASSIGNMENT_RE, CREDENTIAL_RE  # noqa: E402

IDENTITY_RE = re.compile(r"(USER|CLIENT_ID)$")
SECRET_ALPHABET = string.ascii_letters + string.digits
IDENTITY_ALPHABET = string.ascii_lowercase + string.digits
ADDED_COMMENT = "# Added from .env.template by scripts/init_env.py"

# Credentials this repository creates, so init-env generates them (ADR-006, ADR-026).
GENERATED = {"POLARIS_DB_PASSWORD", "POLARIS_ROOT_CLIENT_ID", "POLARIS_ROOT_CLIENT_SECRET",
             "POLARIS_TOKEN_SIGNING_KEY", "DATAHUB_DB_PASSWORD"}

# A Secret: its name, the .env variable naming its namespace (ADR-007), its type, the keys
# filled from .env variables, and keys with fixed values.
Secret = namedtuple("Secret", "name namespace_var type from_env fixed")
SECRETS = [
    Secret("polaris-db", "NS_CATALOG_POLARIS", "kubernetes.io/basic-auth",
           {"password": "POLARIS_DB_PASSWORD"}, {"username": "polaris"}),
    Secret("polaris-root", "NS_CATALOG_POLARIS", "Opaque",
           {"clientId": "POLARIS_ROOT_CLIENT_ID", "clientSecret": "POLARIS_ROOT_CLIENT_SECRET"},
           {}),
    Secret("polaris-token-signing", "NS_CATALOG_POLARIS", "Opaque",
           {"signingKey": "POLARIS_TOKEN_SIGNING_KEY"}, {}),
    Secret("polaris-s3", "NS_CATALOG_POLARIS", "Opaque",
           {"accessKeyId": "POLARIS_S3_ACCESS_KEY_ID",
            "secretAccessKey": "POLARIS_S3_SECRET_ACCESS_KEY"}, {}),
    Secret("datahub-db", "NS_CATALOG_DATAHUB", "kubernetes.io/basic-auth",
           {"password": "DATAHUB_DB_PASSWORD"}, {"username": "datahub"}),
]


def generate(name):
    """A new random value for the credential variable `name`."""
    if IDENTITY_RE.search(name):
        return "".join(secrets.choice(IDENTITY_ALPHABET) for _ in range(16))
    return "".join(secrets.choice(SECRET_ALPHABET) for _ in range(32))


def unquote(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def parse(text):
    """NAME -> value (unquoted) for each assignment line."""
    env = {}
    for line in text.splitlines():
        m = ASSIGNMENT_RE.match(line.strip())
        if m:
            env[m.group(1)] = unquote(m.group(2))
    return env


def merge(template_text, env_text, gen=generate):
    """The new .env text, the credential names filled, and the names added from the template.

    env_text is None when .env does not exist yet.
    """
    filled = []
    lines = []
    for line in (template_text if env_text is None else env_text).splitlines():
        m = ASSIGNMENT_RE.match(line.strip())
        if m and m.group(1) in GENERATED and not unquote(m.group(2)):
            line = f"{m.group(1)}={gen(m.group(1))}"
            filled.append(m.group(1))
        lines.append(line)

    present = parse("\n".join(lines))
    added = []
    for name, value in parse(template_text).items():
        if name in present:
            continue
        if not added:
            lines += ["", ADDED_COMMENT]
        if name in GENERATED and not value:
            value = gen(name)
            filled.append(name)
        lines.append(f"{name}={value}")
        added.append(name)
    return "\n".join(lines) + "\n", filled, added


def missing_by_hand(env):
    """Empty credentials that are not generated: other teams' services, typed by hand."""
    return [name for name, value in env.items()
            if CREDENTIAL_RE.search(name) and name not in GENERATED and not value]


def write_env(path, text):
    """Writes text to path, readable and writable by its owner only."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.chmod(path, 0o600)


def secret_problems(env):
    """What stops the Secrets from being applied; names variables, never values."""
    if not env.get("KUBE_CONTEXT"):
        return ["KUBE_CONTEXT is empty: set your Linux test cluster's context in .env (ADR-004)"]
    problems = []
    for s in SECRETS:
        for var in [s.namespace_var] + list(s.from_env.values()):
            if not env.get(var):
                problems.append(f"{s.name}: {var} is empty")
    return problems


def b64(value):
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def manifests(env):
    """(namespace, a v1 List of the Namespace and the Secret) for each Secret, in order."""
    out = []
    for s in SECRETS:
        ns = env[s.namespace_var]
        data = {key: b64(env[var]) for key, var in s.from_env.items()}
        data.update({key: b64(value) for key, value in s.fixed.items()})
        out.append((ns, {
            "apiVersion": "v1",
            "kind": "List",
            "items": [
                {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": ns}},
                {"apiVersion": "v1", "kind": "Secret", "type": s.type,
                 "metadata": {"name": s.name, "namespace": ns},
                 "data": dict(sorted(data.items()))},
            ],
        }))
    return out


def apply_secrets(env, run=subprocess.run):
    """Applies every Secret, or none: any problem stops before the first kubectl call."""
    problems = secret_problems(env)
    if problems:
        sys.exit("init-env: not applying Secrets:\n" + "\n".join(f"- {p}" for p in problems))
    for (ns, manifest), s in zip(manifests(env), SECRETS):
        cmd = ["kubectl", "--context", env["KUBE_CONTEXT"], "apply", "--server-side",
               "--field-manager=init-env", "-n", ns, "-f", "-"]
        run(cmd, input=json.dumps(manifest).encode("utf-8"), check=True)
        print(f"applied Secret {s.name} in namespace {ns}")


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--env", default=".env")
    parser.add_argument("--template", default=".env.template")
    parser.add_argument("--apply-secrets", action="store_true",
                        help="also create or update the Kubernetes Secrets (needs a cluster)")
    args = parser.parse_args(argv)

    with open(args.template, encoding="utf-8") as f:
        template_text = f.read()
    env_text = None
    if os.path.exists(args.env):
        with open(args.env, encoding="utf-8") as f:
            env_text = f.read()
    text, filled, added = merge(template_text, env_text)
    if text != env_text:
        write_env(args.env, text)
    print(f"{args.env}: filled {len(filled)} credentials"
          + (f" ({', '.join(filled)})" if filled else "")
          + f", added {len(added)} variables from {args.template}")
    by_hand = missing_by_hand(parse(text))
    if by_hand:
        print(f"{args.env}: type into .env by hand: {', '.join(by_hand)} (ADR-023)")

    if args.apply_secrets:
        apply_secrets(parse(text))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
