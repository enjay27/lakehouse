# Restore polaris-pg from its backups into a new Cluster

**Status: not yet proven on a Linux test cluster (#138).** ADR-043 §4 asks for that proof
before this runbook is used on a target cluster.

This runbook restores polaris-pg into a new Cluster, `polaris-pg-restore`, beside it in
Polaris's namespace. The new Cluster is built from the latest base backup and every WAL
segment archived since, through the Barman Cloud Plugin (ADR-043). Then it checks that the
copy holds Polaris's realm and catalogs. polaris-pg itself is left as it is.

The steps are the smoke gate's `check-restore` (`gates/gate.py`), which runs on every smoke.
They differ only in the restored Cluster's size, which here is polaris-pg's own.

**Not covered:**
- Moving Polaris onto the restored Cluster.
- A restore to a point in time.
- A restore after polaris-pg's primary is lost (see step 2).

## Before you start

- A shell on a Linux machine, in a clone of this repository, with `.env` filled in for the
  target cluster (ADR-002). Load it, then check the context:

  ```sh
  set -a; . ./.env; set +a
  test "$(kubectl config current-context)" = "$KUBE_CONTEXT" && echo "context ok"
  ```

  Every command below names `--context "$KUBE_CONTEXT"` and `-n "$NS_CATALOG_POLARIS"`
  (ADR-005).
- **Steps 3 and 6 create and delete a Cluster: Kade approves each one when it runs.**
- `polaris-pg-restore` needs as much storage as polaris-pg, on the same StorageClass.

```sh
k() { kubectl --context "$KUBE_CONTEXT" -n "$NS_CATALOG_POLARIS" "$@"; }
```

## 1. Find a completed backup

The ObjectStore `polaris-backup` holds polaris-pg's base backups and WAL. The daily
`ScheduledBackup` `polaris-pg-daily` writes into it.

```sh
k get objectstore polaris-backup
k get backups --sort-by=.metadata.creationTimestamp \
  -o custom-columns=NAME:.metadata.name,PHASE:.status.phase,STOPPED:.status.stoppedAt
```

**Stop here if no backup reads `completed`.** To take a backup now:

```sh
k apply -f - <<EOF
apiVersion: postgresql.cnpg.io/v1
kind: Backup
metadata:
  name: polaris-pg-manual-$(date -u +%Y%m%d%H%M)
spec:
  cluster:
    name: polaris-pg
  method: plugin
  pluginConfiguration:
    name: barman-cloud.cloudnative-pg.io
EOF
```

Wait until it reads `completed`.

## 2. Switch WAL on the primary and wait until it is archived

CNPG takes backups from a standby by default. Such a backup is consistent only past the
WAL segment that was being written when it ended. If that segment is not archived yet, the
restore cannot reach a consistent state. It retries until the primary's `archive_timeout`
passes (#86: the gate's `check-restore` took 255 s without this step, 85 s with it).

```sh
primary=$(k get cluster/polaris-pg -o jsonpath='{.status.currentPrimary}')
psql_on_primary() { k exec "$primary" -c postgres -- psql -XAtq -c "$1"; }
segment=$(psql_on_primary "SELECT pg_walfile_name(pg_switch_wal())")
echo "switched: $segment"
# WAL file names sort by timeline, then position.
until [ "$(psql_on_primary "SELECT coalesce(last_archived_wal, '') >= '$segment'
                            FROM pg_stat_archiver")" = t ]; do
  sleep 3
done
echo "archived: $segment"
```

The gate allows 120 s for this. If it takes longer, check the plugin's sidecar logs in the
primary's pod before going on.

**If the primary is gone:** this step cannot run, and the restore may not reach a
consistent state from the latest backup. This runbook does not cover that case; stop and
raise it.

## 3. Create polaris-pg-restore (Kade approves)

Use polaris-pg's own image and storage size:

```sh
image=$(k get cluster/polaris-pg -o jsonpath='{.spec.imageName}')
size=$(k get cluster/polaris-pg -o jsonpath='{.spec.storage.size}')
```

Then create the Cluster:

```sh
k apply -f - <<EOF
apiVersion: postgresql.cnpg.io/v1
kind: Cluster
metadata:
  name: polaris-pg-restore
spec:
  instances: 1
  imageName: "$image"
  storage:
    size: "$size"
  bootstrap:
    recovery:
      source: polaris-pg
      database: polaris
      owner: polaris
      secret:
        name: polaris-db
  externalClusters:
    - name: polaris-pg
      plugin:
        name: barman-cloud.cloudnative-pg.io
        parameters:
          barmanObjectName: polaris-backup
          serverName: polaris-pg
EOF
```

- **No recovery target:** every archived WAL segment is replayed.
- **No `plugins` section:** the restore never archives into polaris-pg's backups. Do not
  add one that names `polaris-backup`.
- **The Polaris user keeps its password** from the `polaris-db` Secret.
- **No network policy to add:** ADR-048's policies already let Polaris's clients reach
  every CNPG instance in the namespace.

## 4. Wait until it is Ready and stays healthy

```sh
k wait --for=condition=Ready cluster/polaris-pg-restore --timeout=30m
for i in 1 2 3; do
  k get cluster/polaris-pg-restore \
    -o jsonpath='{.status.phase}{" / "}{.status.readyInstances}{"\n"}'
  sleep 10
done
```

All three reads must say `Cluster in healthy state / 1`. CNPG restarts the restored instance
once after the recovery, so Ready alone is not enough (smoke 38245393066).

If it does not get Ready, look at the full-recovery Job's pods:

```sh
k get pods -l cnpg.io/cluster=polaris-pg-restore,cnpg.io/jobRole=full-recovery
```

## 5. Check that it holds what polaris-pg holds

This reads the realm's root principal and its catalogs. It runs in a short-lived pod labelled
as a polaris-pg client, logged in as Polaris from `polaris-db`, so no password is printed.

```sh
# The pod's container takes the Polaris user from polaris-db; kubectl merges it by name.
overrides() {
  cat <<EOF
{"spec": {"containers": [{"name": "$1", "image": "$image", "stdin": true, "stdinOnce": true,
  "env": [
    {"name": "PGUSER", "valueFrom": {"secretKeyRef": {"name": "polaris-db", "key": "username"}}},
    {"name": "PGPASSWORD", "valueFrom": {"secretKeyRef": {"name": "polaris-db", "key": "password"}}}
  ]}]}}
EOF
}
# The realm's principals (type 2) and catalogs (type 4) in the Cluster behind service $1.
catalogs() {
  name="check-${1%-rw}"
  k run "$name" --image="$image" --restart=Never --rm -i --quiet \
    --labels=lakehouse/polaris-pg-client=true \
    --override-type=strategic --overrides="$(overrides "$name")" \
    --command -- psql "host=$1 dbname=polaris sslmode=require" -XAtq -f - <<'SQL'
SELECT type_code, name FROM POLARIS_SCHEMA.ENTITIES
WHERE realm_id = 'POLARIS' AND type_code IN (2, 4) ORDER BY 1, 2;
SQL
}
catalogs polaris-pg-restore-rw > restored.txt
catalogs polaris-pg-rw > live.txt
cat restored.txt
diff live.txt restored.txt && echo "same principals and catalogs"
```

- `restored.txt` must list the principal `root` (type 2) and every catalog (type 4).
- `diff` shows what changed on polaris-pg after the last archived WAL. It is empty when
  nothing was written since step 2.

## 6. Afterwards (Kade approves)

Moving Polaris onto `polaris-pg-restore` is not covered here (see the top).

When the copy is no longer needed, delete it. Its PVC goes with it, and polaris-pg and its
backups are not touched:

```sh
k delete cluster polaris-pg-restore --wait --timeout=5m
rm -f restored.txt live.txt
```

## Record

Add a comment to the issue the restore was for, giving:
- the backup used and the WAL segment from step 2;
- how long each of steps 1 to 4 took;
- the outcome of step 5.

Write it in neutral terms: no hostnames, endpoints or names (ADR-002).
