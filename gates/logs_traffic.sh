# Traffic the log batch's check follows to the hand-off (ADR-036). Run by `gates/gate.py
# check-logs` in a short-lived curl pod in Polaris's namespace, fed on stdin; POSIX sh.
#
#   sh logs_traffic.sh API_URL REALM COUNT PREFIX CATALOG S3_ENDPOINT REGION BUCKET \
#     POD_NAME POD_URL [POD_NAME POD_URL]...
#
# Root gets a token from API_URL with client_credentials (ROOT_CLIENT_ID and
# ROOT_CLIENT_SECRET, from the polaris-root Secret). Through API_URL it makes CATALOG on the
# S3 endpoint, as gates/iceberg/smoke.py makes its own (PREFIX-setup-catalog: 201, or 409 when
# a run before made it), and its namespace `logs` (200 or 409). Then each pod, asked directly
# at its POD_URL, lists the catalogs COUNT times with root's token (X-Request-ID
# PREFIX-POD_NAME-read-N, which must answer 200) and COUNT times without one
# (PREFIX-POD_NAME-denied-N, 401); then, once, makes a table without a schema (-invalid-1,
# 400: refused before anything is written, where a name made twice would leave the gate's
# recorder a failed INSERT), loads a table that does not exist (-missing-1, 404), and makes a
# table (-commit-1, 200); tables are named for their request ID, dashes as underscores. Polaris writes each ID to the request's lines; the
# audit policy counts, keeps or drops each as gates/gate.py logs_request_ids says
# (docs/contracts/polaris-logs.md). Neither the credentials nor the token are printed. Prints
# one line per request, `ok` or `FAIL`; exits 1 when any failed.
set -u
api=$1 realm=$2 count=$3 prefix=$4 catalog=$5 s3=$6 region=$7 bucket=$8
shift 8
fail=0

# ask ID WHO EXPECTED METHOD URL [BODY]: one request with root's token, its status checked
# against EXPECTED, a `|`-separated list. POSIX sh has no `local`: its variables are ask_*.
ask() {
  ask_id=$1 ask_who=$2 ask_want=$3 ask_method=$4 ask_url=$5
  if [ $# -ge 6 ]; then
    ask_code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 60 -X "$ask_method" \
      -H "Polaris-Realm: $realm" -H "Authorization: Bearer $token" -H "X-Request-ID: $ask_id" \
      -H 'Content-Type: application/json' --data "$6" "$ask_url")
  else
    ask_code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 60 -X "$ask_method" \
      -H "Polaris-Realm: $realm" -H "Authorization: Bearer $token" -H "X-Request-ID: $ask_id" \
      "$ask_url")
  fi
  case "|$ask_want|" in
    *"|$ask_code|"*) echo "ok   $ask_id: ${ask_who}answered HTTP $ask_code" ;;
    *) echo "FAIL $ask_id: ${ask_who}answered HTTP $ask_code, expected $ask_want"; fail=1 ;;
  esac
}

body=$(curl -s --max-time 20 -H "Polaris-Realm: $realm" \
  --data-urlencode grant_type=client_credentials --data-urlencode "client_id=$ROOT_CLIENT_ID" \
  --data-urlencode "client_secret=$ROOT_CLIENT_SECRET" --data-urlencode scope=PRINCIPAL_ROLE:ALL \
  "$api/api/catalog/v1/oauth/tokens")
token=$(printf '%s' "$body" | sed -n 's/.*"access_token" *: *"\([^"]*\)".*/\1/p')
if [ -z "$token" ]; then echo "FAIL root got no token in realm $realm"; exit 1; fi

location="s3://$bucket/$catalog"
ask "$prefix-setup-catalog" "" "201|409" POST "$api/api/management/v1/catalogs" \
  "{\"catalog\":{\"type\":\"INTERNAL\",\"name\":\"$catalog\",\
\"properties\":{\"default-base-location\":\"$location\"},\
\"storageConfigInfo\":{\"storageType\":\"S3\",\"allowedLocations\":[\"$location\"],\
\"endpoint\":\"$s3\",\"region\":\"$region\",\"pathStyleAccess\":true,\"stsUnavailable\":true}}}"
space="api/catalog/v1/$catalog/namespaces"
ask "$prefix-setup-namespace" "" "200|409" POST "$api/$space" '{"namespace":["logs"]}'
[ $fail = 0 ] || exit 1

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
  id="$prefix-$name-invalid-1"
  ask "$id" "$name " 400 POST "$url/$space/logs/tables" \
    "{\"name\":\"$(printf '%s' "$id" | tr -- - _)\"}"
  ask "$prefix-$name-missing-1" "$name " 404 GET "$url/$space/logs/tables/absent"
  id="$prefix-$name-commit-1"
  ask "$id" "$name " 200 POST "$url/$space/logs/tables" "{\"name\":\"$(printf '%s' "$id" | \
tr -- - _)\",\"schema\":{\"type\":\"struct\",\"schema-id\":0,\"fields\":[{\"id\":1,\
\"name\":\"id\",\"required\":false,\"type\":\"long\"}]}}"
done
exit $fail
