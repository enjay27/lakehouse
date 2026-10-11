# The stand-ins answer as Polaris needs them to (ADR-031, ADR-026). Run by `gates/gate.py
# check-standins` in a short-lived curl pod in Polaris's namespace, fed on stdin; POSIX sh.
#
#   sh check_standins.sh S3_ENDPOINT BUCKET REGION OPA_URL [NAME WANT INPUT_JSON]...
#
# S3: a signed PUT (with Expect: 100-continue, as the S3 client sends it), a signed GET that
# returns the same bytes, an anonymous GET that is refused (403), and a signed DELETE, which
# removes the object again. The credentials come from AWS_ACCESS_KEY_ID and
# AWS_SECRET_ACCESS_KEY (the polaris-s3 Secret) and are never printed.
# OPA: each case POSTs {"input": INPUT_JSON} to OPA_URL, the policy package's URL, and expects
# the package document with `"allow": WANT`, the only field Polaris reads; a bare boolean
# result is a deny to Polaris and fails here (docs/contracts/polaris-opa-input.md).
# Prints one line per check, `ok` or `FAIL`; exits 1 when any check failed.
set -u
s3=$1 bucket=$2 region=$3 opa=$4
shift 4
fail=0
ok() { echo "ok   $1"; }
bad() { echo "FAIL $1"; fail=1; }

key="gate-check/$(date +%s)-$$"
body="stand-in round trip $key"
url="$s3/$bucket/$key"
sign() {
  curl -s --max-time 20 --aws-sigv4 "aws:amz:$region:s3" \
    --user "$AWS_ACCESS_KEY_ID:$AWS_SECRET_ACCESS_KEY" "$@"
}

code=$(sign -o /dev/null -w '%{http_code}' -H 'Expect: 100-continue' -X PUT \
  --data-binary "$body" "$url")
if [ "$code" = 200 ]; then ok "S3 signed PUT $bucket/$key: HTTP 200"
else bad "S3 signed PUT $bucket/$key: HTTP $code, expected 200"; fi

got=$(sign "$url")
if [ "$got" = "$body" ]; then ok "S3 signed GET returns the bytes put"
else bad "S3 signed GET returned something else"; fi

code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "$url")
if [ "$code" = 403 ]; then ok "S3 anonymous GET refused: HTTP 403"
else bad "S3 anonymous GET: HTTP $code, expected 403"; fi

code=$(sign -o /dev/null -w '%{http_code}' -X DELETE "$url")
if [ "$code" = 204 ]; then ok "S3 signed DELETE: HTTP 204"
else bad "S3 signed DELETE: HTTP $code, expected 204"; fi

while [ $# -ge 3 ]; do
  name=$1 want=$2 input=$3
  shift 3
  got=$(curl -s --max-time 10 -H 'Content-Type: application/json' \
    --data-binary "{\"input\": $input}" "$opa" | tr -d ' \n\t')
  case "$got" in
    "{\"result\":{"*"\"allow\":$want"[,}]*) ok "OPA $name: allow $want" ;;
    *) bad "OPA $name: expected allow $want, got $got" ;;
  esac
done
exit $fail
