# The restored copy of polaris-pg holds the catalog (ADR-043 §4). Run by `gates/gate.py
# check-restore` in a short-lived pod on the PostgreSQL image of charts/polaris-postgres, in
# Polaris's namespace, fed on stdin; POSIX sh.
#
#   sh check_restore.sh HOST DATABASE REALM CATALOG
#
# Logs in as Polaris (PGUSER and PGPASSWORD from the polaris-db Secret, never printed) to the
# restored Cluster's read-write service, straight, not through the recorder, and counts in
# Polaris's entities table the realm's root principal (type 2, PolarisEntityType.PRINCIPAL)
# and the catalog the smoke tests created (type 4, CATALOG). Prints `ok` or `FAIL` for each;
# exits 1 when any failed.
set -u
host=$1 db=$2 realm=$3 catalog=$4
fail=0
check() {
  got=$(psql "host=$host port=5432 dbname=$db sslmode=require application_name=gate-check-restore" \
    -v ON_ERROR_STOP=1 -qAt -c "$2" 2>&1)
  if [ "$got" = 1 ]; then echo "ok   restored: $1"
  else echo "FAIL restored: $1: $got"; fail=1; fi
}
entities="SELECT count(*) FROM POLARIS_SCHEMA.ENTITIES WHERE realm_id = '$realm'"
check "the realm's root principal" "$entities AND type_code = 2 AND name = 'root'"
check "the catalog $catalog" "$entities AND type_code = 4 AND name = '$catalog'"
exit $fail
