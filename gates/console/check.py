"""The console's sign-in check (ADR-028, ADR-026): the requests a maintainer's browser makes,
from a short-lived pod on the pinned Python image (`gates/gate.py check-console`). Standard
library only.

    python3 check.py ORIGIN API_URL REALM

ORIGIN is where maintainers open the console (POLARIS_CONSOLE_ORIGIN), API_URL where their
browser reaches Polaris (POLARIS_CONSOLE_API_URL). Root's credentials come from the
environment, never the command line: ROOT_CLIENT_ID and ROOT_CLIENT_SECRET (the Secret
polaris-root).

1. The console answers /health and its page, and its config.js points the browser at API_URL,
   its token URL and REALM.
2. Root creates the principal gate_maintainer, which the gate's policy lets read only (a
   principal already there gets new credentials). Maintainers sign in with their own
   principals, never root (ADR-026).
3. As the browser, with `Origin: ORIGIN`: the CORS preflight of the token request, the token
   for gate_maintainer, the preflight of the management API, the list of catalogs, and a
   catalog create refused with a 403 by the policy. Each answer carries
   `Access-Control-Allow-Origin: ORIGIN`.
4. A preflight from another origin is not allowed.

Prints one line per check, `ok` or `FAIL`, with every credential masked; exits 1 when any
check failed.
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

MAINTAINER = "gate_maintainer"
OTHER_ORIGIN = "http://not-the-console.invalid"
CREDENTIAL_VARS = ("ROOT_CLIENT_ID", "ROOT_CLIENT_SECRET")


def urllib_http(method, url, headers, body=None, form=None):
    """(status, headers in lowercase, raw body): one request, no exception for an HTTP error."""
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers = {**headers, "Content-Type": "application/x-www-form-urlencoded"}
    elif body is not None:
        data = json.dumps(body).encode()
        headers = {**headers, "Content-Type": "application/json"}
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            return response.status, _lower(response.headers), response.read()
    except urllib.error.HTTPError as e:
        return e.code, _lower(e.headers), e.read()


def _lower(headers):
    return {k.lower(): v for k, v in headers.items()}


def _json(raw):
    try:
        return json.loads(raw) if raw else {}
    except ValueError:
        return {}


class Report:
    def __init__(self, env, out):
        self.secrets = [env[v] for v in CREDENTIAL_VARS if env.get(v)]
        self.out, self.failed = out, False

    def secret(self, value):
        if value:
            self.secrets.append(value)

    def mask(self, text):
        for value in self.secrets:
            text = text.replace(value, "<redacted>")
        return text

    def ok(self, line):
        print(f"ok   {self.mask(line)}", file=self.out, flush=True)

    def bad(self, line):
        self.failed = True
        print(f"FAIL {self.mask(line)}", file=self.out, flush=True)

    def check(self, passed, line, why=""):
        if passed:
            self.ok(line)
        else:
            self.bad(f"{line}{': ' + why if why else ''}")
        return passed


def check_console(http, report, origin, api, realm):
    status, _, raw = http("GET", f"{origin}/health", {})
    report.check(status == 200 and raw.strip() == b"healthy", "console answers /health",
                 f"HTTP {status}")
    status, headers, _ = http("GET", f"{origin}/", {})
    report.check(status == 200 and headers.get("content-type", "").startswith("text/html"),
                 "console serves its page", f"HTTP {status} {headers.get('content-type')}")
    status, _, raw = http("GET", f"{origin}/config.js", {})
    text = raw.decode(errors="replace")
    wanted = [f"VITE_POLARIS_API_URL: '{api}'", f"VITE_POLARIS_REALM: '{realm}'",
              f"VITE_OAUTH_TOKEN_URL: '{api}/api/catalog/v1/oauth/tokens'"]
    missing = [w for w in wanted if w not in text]
    report.check(status == 200 and not missing,
                 f"config.js points the browser at {api}, realm {realm}",
                 f"HTTP {status}, missing {missing}")


def root_headers(http, api, realm, env):
    status, _, raw = http("POST", f"{api}/api/catalog/v1/oauth/tokens", {"Polaris-Realm": realm},
                          form={"grant_type": "client_credentials",
                                "client_id": env["ROOT_CLIENT_ID"],
                                "client_secret": env["ROOT_CLIENT_SECRET"],
                                "scope": "PRINCIPAL_ROLE:ALL"})
    token = _json(raw).get("access_token") if status == 200 else None
    return {"Authorization": f"Bearer {token}", "Polaris-Realm": realm} if token else None


def maintainer_credentials(http, report, api, headers):
    """(clientId, clientSecret) for gate_maintainer, new or rotated, or None."""
    principals = f"{api}/api/management/v1/principals"
    status, _, raw = http("POST", principals, headers, {"principal": {"name": MAINTAINER}})
    expected = 201
    if status == 409:
        status, _, raw = http("POST", f"{principals}/{MAINTAINER}/rotate", headers)
        expected = 200
    creds = _json(raw).get("credentials") or {}
    if status != expected or not creds.get("clientId") or not creds.get("clientSecret"):
        report.bad(f"root creating principal {MAINTAINER}: HTTP {status}, expected {expected}")
        return None
    report.secret(creds["clientSecret"])
    report.ok(f"principal {MAINTAINER} ready: HTTP {status}")
    return creds["clientId"], creds["clientSecret"]


def allowed(headers, origin):
    return headers.get("access-control-allow-origin") == origin


def preflight(http, report, origin, url, method, what):
    status, headers, _ = http("OPTIONS", url, {
        "Origin": origin, "Access-Control-Request-Method": method,
        "Access-Control-Request-Headers": "authorization,content-type,polaris-realm"})
    return report.check(200 <= status < 300 and allowed(headers, origin),
                        f"preflight of {what} allows {origin}",
                        f"HTTP {status}, Access-Control-Allow-Origin "
                        f"{headers.get('access-control-allow-origin')}")


def sign_in(http, report, origin, api, realm, client):
    """The browser's requests as gate_maintainer. Returns nothing; each is reported."""
    tokens = f"{api}/api/catalog/v1/oauth/tokens"
    catalogs = f"{api}/api/management/v1/catalogs"
    browser = {"Origin": origin, "Polaris-Realm": realm}
    preflight(http, report, origin, tokens, "POST", "the token request")
    status, headers, raw = http("POST", tokens, browser, form={
        "grant_type": "client_credentials", "client_id": client[0], "client_secret": client[1],
        "scope": "PRINCIPAL_ROLE:ALL"})
    token = _json(raw).get("access_token") if status == 200 else None
    report.secret(token)
    if not report.check(token and allowed(headers, origin),
                        f"{MAINTAINER} got a token from the browser's origin",
                        f"HTTP {status}, Access-Control-Allow-Origin "
                        f"{headers.get('access-control-allow-origin')}"):
        return
    browser["Authorization"] = f"Bearer {token}"
    preflight(http, report, origin, catalogs, "GET", "the management API")
    status, headers, raw = http("GET", catalogs, browser)
    listed = _json(raw).get("catalogs")
    report.check(status == 200 and isinstance(listed, list) and allowed(headers, origin),
                 f"{MAINTAINER} listed {len(listed or [])} catalogs from the browser's origin",
                 f"HTTP {status}")
    status, headers, _ = http("POST", catalogs, browser, {"catalog": {
        "type": "INTERNAL", "name": "gate_maintainer_may_not",
        "properties": {"default-base-location": "s3://none/none"},
        "storageConfigInfo": {"storageType": "S3", "allowedLocations": ["s3://none/none"],
                              "stsUnavailable": True}}})
    report.check(status == 403 and allowed(headers, origin),
                 f"{MAINTAINER} creating a catalog: refused (403)", f"HTTP {status}")


def run(argv, env, http=urllib_http, out=sys.stdout):
    origin, api, realm = argv
    report = Report(env, out)
    check_console(http, report, origin, api, realm)
    headers = root_headers(http, api, realm, env)
    if not headers:
        report.bad(f"root got no token in realm {realm}")
        return 1
    report.secret(headers["Authorization"].split(" ", 1)[1])
    client = maintainer_credentials(http, report, api, headers)
    if client:
        sign_in(http, report, origin, api, realm, client)
    status, other, _ = http("OPTIONS", f"{api}/api/catalog/v1/oauth/tokens", {
        "Origin": OTHER_ORIGIN, "Access-Control-Request-Method": "POST"})
    report.check(not allowed(other, OTHER_ORIGIN),
                 f"another origin ({OTHER_ORIGIN}) is not allowed: HTTP {status}",
                 "Polaris allows it")
    return 1 if report.failed else 0


def main(argv):
    if len(argv) != 4:
        print(__doc__.strip().splitlines()[4].strip(), file=sys.stderr)
        return 2
    return run(argv[1:], os.environ)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
