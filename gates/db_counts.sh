# Polaris's database counted before and after an upgrade (ADR-051 §2, §3). Run by
# `gates/gate.py seed` and `gates/gate.py check-migration` in a short-lived pod on the PostgreSQL
# image of charts/polaris-postgres, in Polaris's namespace, fed on stdin; POSIX sh.
#
#   sh db_counts.sh HOST DATABASE
#
# Logs in as Polaris (PGUSER and PGPASSWORD from the polaris-db Secret, never printed) to
# polaris-pg's read-write service, straight, not through the recorder, and prints one line per
# table of polaris_schema, `table NAME ROWS`, then `version N` from its version table. Exits
# non-zero when psql fails.
set -eu
host=$1 db=$2
psql "host=$host port=5432 dbname=$db sslmode=require application_name=gate-db-counts" \
  -v ON_ERROR_STOP=1 -qAt -F ' ' <<'SQL'
SELECT 'table', table_name,
       (xpath('/row/c/text()', query_to_xml(format('SELECT count(*) AS c FROM %I.%I',
                                                   table_schema, table_name),
                                            false, true, '')))[1]::text::bigint
  FROM information_schema.tables
 WHERE table_schema = 'polaris_schema' AND table_type = 'BASE TABLE'
 ORDER BY table_name;
SELECT 'version', version_value FROM polaris_schema.version WHERE version_key = 'version';
SQL
