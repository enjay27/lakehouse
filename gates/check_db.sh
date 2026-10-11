# Polaris's database answers through the recorder (ADR-034), with and without TLS. Run by
# `gates/gate.py check-standins` in a short-lived pod on the PostgreSQL image of
# charts/polaris-postgres, in Polaris's namespace, fed on stdin; POSIX sh.
#
#   sh check_db.sh HOST DATABASE
#
# Logs in as Polaris (PGUSER and PGPASSWORD from the polaris-db Secret, never printed), once
# with sslmode=require (the recorder accepts TLS and opens TLS to PostgreSQL) and once with
# sslmode=disable, and in each session writes and reads one row of a temporary table, so the
# recorder sees a login, statements and a result. Prints `ok` or `FAIL` per mode; exits 1
# when any failed.
set -u
host=$1 db=$2
fail=0
for mode in require disable; do
  got=$(psql "host=$host port=5432 dbname=$db sslmode=$mode application_name=gate-check-$mode" \
    -v ON_ERROR_STOP=1 -qAt \
    -c "CREATE TEMP TABLE gate_check (n integer)" \
    -c "INSERT INTO gate_check VALUES (1)" \
    -c "SELECT count(*) FROM gate_check" 2>&1)
  if [ "$got" = 1 ]; then echo "ok   PostgreSQL through the recorder, sslmode=$mode"
  else echo "FAIL PostgreSQL through the recorder, sslmode=$mode: $got"; fail=1; fi
done
exit $fail
