# Traffic the log batch's check follows to the hand-off (ADR-036). Run by `gates/gate.py
# check-logs` in a short-lived curl pod in Polaris's namespace, fed on stdin; POSIX sh.
#
#   sh logs_traffic.sh API_URL REALM COUNT PREFIX POD_NAME POD_URL [POD_NAME POD_URL]...
#
# Root gets a token from API_URL with client_credentials (ROOT_CLIENT_ID and
# ROOT_CLIENT_SECRET, from the polaris-root Secret). Then each pod, asked directly at its
# POD_URL, lists the catalogs COUNT times with root's token (X-Request-ID
# PREFIX-POD_NAME-read-N, which must answer 200) and COUNT times without one
# (PREFIX-POD_NAME-denied-N, which must answer 401). Polaris writes each ID to its access line;
# the audit policy counts the reads and keeps the refusals (docs/contracts/polaris-logs.md).
# Neither the credentials nor the token are printed. Prints one line per request, `ok` or
# `FAIL`; exits 1 when any failed.
set -u
api=$1 realm=$2 count=$3 prefix=$4
shift 4
fail=0

body=$(curl -s --max-time 20 -H "Polaris-Realm: $realm" \
  --data-urlencode grant_type=client_credentials --data-urlencode "client_id=$ROOT_CLIENT_ID" \
  --data-urlencode "client_secret=$ROOT_CLIENT_SECRET" --data-urlencode scope=PRINCIPAL_ROLE:ALL \
  "$api/api/catalog/v1/oauth/tokens")
token=$(printf '%s' "$body" | sed -n 's/.*"access_token" *: *"\([^"]*\)".*/\1/p')
if [ -z "$token" ]; then echo "FAIL root got no token in realm $realm"; exit 1; fi

[ $# -ge 2 ] || { echo "FAIL no pod to ask"; exit 1; }
while [ $# -ge 2 ]; do
  name=$1 url=$2
  shift 2
  n=1
  while [ "$n" -le "$count" ]; do
    id="$prefix-$name-read-$n"
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 -H "Polaris-Realm: $realm" \
      -H "Authorization: Bearer $token" -H "X-Request-ID: $id" \
      "$url/api/management/v1/catalogs")
    if [ "$code" = 200 ]; then echo "ok   $id: $name answered HTTP 200"
    else echo "FAIL $id: $name answered HTTP $code, expected 200"; fail=1; fi
    id="$prefix-$name-denied-$n"
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 -H "Polaris-Realm: $realm" \
      -H "X-Request-ID: $id" "$url/api/management/v1/catalogs")
    if [ "$code" = 401 ]; then echo "ok   $id: $name refused it without a token, HTTP 401"
    else echo "FAIL $id: $name answered HTTP $code without a token, expected 401"; fail=1; fi
    n=$((n + 1))
  done
done
exit $fail
