"""Datahub's smoke check (ADR-071 §8, ADR-072 §4): the requests a lineage emitter and a user
make, from a short-lived pod on the pinned Python image (`gates/gate.py check-datahub`).
Standard library only.

    python3 check.py gms GMS_URL NAME SEARCH_SECONDS
    python3 check.py login FRONTEND_URL

The credential comes on stdin, one line, never on the command line: the system client's secret
(the Secret datahub-auth-secrets) for `gms`, the root user's password (datahub-root-user) for
`login`.

`gms`, as the system client `__datahub_system`, from a pod labelled as GMS's client:
1. GMS answers /health.
2. A dataset named NAME is written with its datasetProperties aspect (ingestProposal), then read
   back by its URN: datahub-pg.
3. A search for NAME finds its URN within SEARCH_SECONDS: the change log went through Kafka and
   was indexed in OpenSearch.
4. The dataset is hard-deleted, and reading it back finds nothing. Once written, it is deleted
   whatever failed after.

`login`, from a pod in the ingress controller's namespace: the frontend signs in `datahub` with
the password, and refuses the image's own `datahub:datahub`.

Prints one line per check, `ok` or `FAIL`, with the credential masked; exits 1 when any check
failed, 2 on wrong arguments.
"""

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

SYSTEM_CLIENT = "__datahub_system"
ROOT_USER = "datahub"
# The image's own user.props line, which datahub-root-user replaces (ADR-071 §6).
IMAGE_PASSWORD = "datahub"
PLATFORM = "urn:li:dataPlatform:hive"
RESTLI = {"X-RestLi-Protocol-Version": "2.0.0"}
POLL_SECONDS = 3


def urllib_http(method, url, headers, body=None):
    """(status, headers in lowercase, raw body): one request, no exception for an HTTP error."""
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers = {**headers, "Content-Type": "application/json"}
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
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
    def __init__(self, secret, out):
        self.secret, self.out, self.failed = secret, out, False

    def mask(self, text):
        return text.replace(self.secret, "<redacted>") if self.secret else text

    def check(self, passed, line, why=""):
        if passed:
            print(f"ok   {self.mask(line)}", file=self.out, flush=True)
        else:
            self.failed = True
            print(f"FAIL {self.mask(line)}{': ' + self.mask(why) if why else ''}", file=self.out,
                  flush=True)
        return passed


def dataset_urn(name):
    return f"urn:li:dataset:({PLATFORM},{name},PROD)"


def read_properties(http, gms, headers, urn):
    """(status, the datasetProperties aspect or None) for `urn`."""
    quoted = urllib.parse.quote(urn, safe="")
    status, _, raw = http("GET", f"{gms}/aspects/{quoted}?aspect=datasetProperties&version=0",
                          headers)
    aspect = _json(raw).get("aspect") or {}
    return status, aspect.get("com.linkedin.dataset.DatasetProperties")


def check_gms(http, report, gms, secret, name, search_seconds, sleep=time.sleep,
              clock=time.monotonic):
    headers = {**RESTLI, "Authorization": f"Basic {SYSTEM_CLIENT}:{secret}"}
    status, _, _ = http("GET", f"{gms}/health", {})
    if not report.check(status == 200, "GMS answers /health", f"HTTP {status}"):
        return
    urn = dataset_urn(name)
    status, _, _ = http("POST", f"{gms}/aspects?action=ingestProposal", headers, {"proposal": {
        "entityType": "dataset", "entityUrn": urn, "changeType": "UPSERT",
        "aspectName": "datasetProperties",
        "aspect": {"contentType": "application/json",
                   "value": json.dumps({"name": name, "description": "lakehouse gate check"})}}})
    if not report.check(status == 200, f"{urn} written as {SYSTEM_CLIENT}", f"HTTP {status}"):
        return
    try:
        status, aspect = read_properties(http, gms, headers, urn)
        report.check(status == 200 and (aspect or {}).get("name") == name,
                     f"{urn} read back by its URN", f"HTTP {status}, {aspect}")
        end, found, tries = clock() + search_seconds, False, 0
        while True:
            tries += 1
            status, _, raw = http("POST", f"{gms}/entities?action=search", headers,
                                  {"input": name, "entity": "dataset", "start": 0, "count": 10})
            found = status == 200 and urn in json.dumps(_json(raw).get("value") or {})
            if found or clock() >= end:
                break
            sleep(POLL_SECONDS)
        report.check(found, f"{urn} found by search after {tries} tries",
                     f"not within {search_seconds} s, last HTTP {status}")
    finally:
        status, _, _ = http("POST", f"{gms}/entities?action=delete", headers, {"urn": urn})
        report.check(status == 200, f"{urn} hard-deleted", f"HTTP {status}")
        status, aspect = read_properties(http, gms, headers, urn)
        report.check(aspect is None, f"{urn} reads back nothing after the delete",
                     f"HTTP {status}, {aspect}")


def check_login(http, report, frontend, password):
    status, headers, _ = http("POST", f"{frontend}/logIn", {},
                              {"username": ROOT_USER, "password": password})
    report.check(status == 200 and "set-cookie" in headers,
                 f"the frontend signs in {ROOT_USER} with datahub-root-user's password",
                 f"HTTP {status}")
    if password == IMAGE_PASSWORD:
        report.check(False, f"{ROOT_USER}:{IMAGE_PASSWORD} refused",
                     "datahub-root-user holds the image's own password")
        return
    status, _, _ = http("POST", f"{frontend}/logIn", {},
                        {"username": ROOT_USER, "password": IMAGE_PASSWORD})
    report.check(status == 401, f"the frontend refuses the image's {ROOT_USER}:{IMAGE_PASSWORD}",
                 f"HTTP {status}")


def run(argv, secret, http=urllib_http, out=sys.stdout, sleep=time.sleep, clock=time.monotonic):
    report = Report(secret, out)
    if argv[0] == "gms":
        _, gms, name, seconds = argv
        check_gms(http, report, gms, secret, name, int(seconds), sleep, clock)
    else:
        _, frontend = argv
        check_login(http, report, frontend, secret)
    return 1 if report.failed else 0


def main(argv, stdin=sys.stdin):
    args = argv[1:]
    if not (args[:1] == ["gms"] and len(args) == 4 or args[:1] == ["login"] and len(args) == 2):
        print(__doc__.strip().splitlines()[4].strip(), file=sys.stderr)
        return 2
    secret = stdin.readline().strip()
    if not secret:
        print("FAIL no credential on stdin", flush=True)
        return 1
    return run(args, secret)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
