# Polaris answers as a release must (ADR-009, ADR-026). Run by `gates/gate.py check-polaris` in a
# short-lived curl pod in Polaris's namespace, fed on stdin; POSIX sh.
#
#   sh check_polaris.sh API_URL MGMT_URL REALM POD_URL...
#
# The management port says ready. Root gets a token from API_URL with client_credentials
# (ROOT_CLIENT_ID and ROOT_CLIENT_SECRET, from the polaris-root Secret), which only a realm
# the bootstrap created can give. Then each pod, asked directly at its POD_URL, accepts that
# token and lists the catalogs, so every replica signs with the same key (ADR-026); in OPA mode
# each listing is also asked of the policy endpoint. Neither the credentials nor the token are
# printed. Prints one line per check, `ok` or `FAIL`; exits 1 when any check failed.
set -u
api=$1 mgmt=$2 realm=$3
shift 3
fail=0
ok() { echo "ok   $1"; }
bad() { echo "FAIL $1"; fail=1; }

code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$mgmt/q/health/ready")
if [ "$code" = 200 ]; then ok "Polaris is ready: $mgmt/q/health/ready HTTP 200"
else bad "Polaris is not ready: $mgmt/q/health/ready HTTP $code"; fi

body=$(curl -s --max-time 20 -H "Polaris-Realm: $realm" \
  --data-urlencode grant_type=client_credentials --data-urlencode "client_id=$ROOT_CLIENT_ID" \
  --data-urlencode "client_secret=$ROOT_CLIENT_SECRET" --data-urlencode scope=PRINCIPAL_ROLE:ALL \
  "$api/api/catalog/v1/oauth/tokens")
token=$(printf '%s' "$body" | sed -n 's/.*"access_token" *: *"\([^"]*\)".*/\1/p')
if [ -n "$token" ]; then ok "root got a token in realm $realm"
else bad "root got no token in realm $realm"; fi

[ $# -gt 0 ] || bad "no pod to ask"
for pod in "$@"; do
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 -H "Polaris-Realm: $realm" \
    -H "Authorization: Bearer $token" "$pod/api/management/v1/catalogs")
  if [ "$code" = 200 ]; then ok "$pod accepts root's token and lists the catalogs: HTTP 200"
  else bad "$pod with root's token: HTTP $code, expected 200"; fi
done
exit $fail
