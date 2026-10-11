# Polaris 1.6.0's log files across an hourly roll

Real files written by Polaris 1.6.0 with the file appender this repository sets
(`charts/polaris/values-lakehouse.yaml`, ADR-036), captured once on 2026-10-11 and replayed by
`gates/test_check_logs.py` (`Roll`) through the real batch and `gate.py`'s `logs_problems` on
every push (#54). Rolled files never change, so one capture proves the roll until Polaris is
upgraded; an upgrade captures them again (ADR-009).

| File | What |
|---|---|
| `polaris-<pod>.log.2026-10-11-17.gz` | Each pod's roll: its lines up to the hour boundary |
| `polaris-<pod>.log` | Each pod's current file after the roll |
| `manifest.json` | Images, clock, boundary, request ID prefixes, and each file's SHA-256, mtime and lines |

What happened, per pod: `gates/logs_traffic.sh` (prefix `fx-pre`), the boundary, the same again
(`fx-post`), which rolled the file, then on the first pod one table made while S3 was down
(`…-fail-1`, HTTP 500, an `ERROR` line carrying `exception`).

**The clock.** To roll within minutes, the JVM's zone was `GMT+00:37` (`-Duser.timezone`), so
its hour turned at 17:23 UTC, and the roll is named for that local hour. JSON timestamps stay
UTC (`QUARKUS_LOG_FILE_JSON_ZONE_ID=UTC`), as `TZ=UTC` makes them in a release. A roll named
for local hour L then holds lines of UTC hours L-1 and L, so the batch, which picks files by
mtime and lines by timestamp, publishes every line before the roll moves to `done/`. The
release itself runs with `TZ=UTC`, where rolls are named for the UTC hour.

**Not like the release:** two containers on one machine, each with its own in-memory catalog
(so each made `gate_logs`), Docker's own addresses, no OPA, and S3 from the gate's pinned
SeaweedFS stand-in.

## Capturing again

Bash, Docker, curl, from the repository root. `POLARIS` is the image in
`charts/polaris/values-lakehouse.yaml`, `SEAWEED` the one in `gates/pins.json`. Every key and
secret is generated for the capture and never written down.

```bash
OUT=$(mktemp -d); mkdir -m 777 "$OUT/logs" "$OUT/sw"
PODS="polaris-5f7c9d8b6-aaaaa polaris-5f7c9d8b6-bbbbb"
hex() { head -c "$1" /dev/urandom | od -An -tx1 | tr -d ' \n'; }
AK=$(hex 12) SK=$(hex 24) ROOT=$(hex 16)
printf '{"identities":[{"name":"polaris","credentials":[{"accessKey":"%s","secretKey":"%s"}],"actions":["Admin","Read","Write","List","Tagging"]}]}' \
  "$AK" "$SK" > "$OUT/sw/s3.json"
docker network create lk
docker run -d --name sw --network lk -v "$OUT/sw:/etc/seaweedfs:ro" "$SEAWEED" mini -dir=/data \
  -ip.bind=0.0.0.0 -s3.port=8333 -bucket=warehouse -master.telemetry=false -webdav=false \
  -admin.ui=false -s3.port.iceberg=0 -s3.port.lance=0 -s3.config=/etc/seaweedfs/s3.json
B=$(( (10#$(date -u +%M) + 3) % 60 )); OFF=$(printf 'GMT+00:%02d' $(( (60 - B) % 60 )))
port=8190
for pod in $PODS; do port=$((port + 1))
  docker run -d --name $pod --hostname $pod --network lk -p $port:8181 \
    -v "$OUT/logs:/deployments/logs" -e TZ=UTC -e "JAVA_OPTS_APPEND=-Duser.timezone=$OFF" \
    -e QUARKUS_LOG_FILE_JSON_ZONE_ID=UTC -e AWS_ACCESS_KEY_ID=$AK -e AWS_SECRET_ACCESS_KEY=$SK \
    -e AWS_REGION=us-east-1 -e POLARIS_BOOTSTRAP_CREDENTIALS=POLARIS,root,$ROOT \
    -e QUARKUS_HTTP_ACCESS_LOG_ENABLED=true -e QUARKUS_LOG_FILE_ENABLED=true \
    -e 'QUARKUS_LOG_FILE_PATH=/deployments/logs/polaris-${HOSTNAME}.log' \
    -e QUARKUS_LOG_FILE_JSON_ENABLED=true -e QUARKUS_LOG_FILE_ROTATION_FILE_SUFFIX=.yyyy-MM-dd-HH.gz \
    -e QUARKUS_LOG_FILE_ROTATION_ROTATE_ON_BOOT=false -e QUARKUS_LOG_FILE_ROTATION_MAX_FILE_SIZE=2G \
    -e QUARKUS_LOG_FILE_ROTATION_MAX_BACKUP_INDEX=50 -e polaris.readiness.ignore-severe-issues=true \
    "$POLARIS"
done
traffic() { port=8190; for pod in $PODS; do port=$((port + 1))
  ROOT_CLIENT_ID=root ROOT_CLIENT_SECRET=$ROOT sh -s -- "http://localhost:$port" POLARIS 2 \
    "fx-$1" gate_logs http://sw:8333 us-east-1 warehouse $pod "http://localhost:$port" \
    < gates/logs_traffic.sh; done; }
# once both answer 401 on /api/management/v1/catalogs:
traffic pre
until [ $(( 10#$(date -u +%M) )) -eq $B ] && [ $(( 10#$(date -u +%S) )) -ge 5 ]; do sleep 2; done
traffic post
docker stop sw   # then, as root on port 8191, make one table (X-Request-ID fx-post-<pod>-fail-1)
docker stop $PODS
```

Then copy `$OUT/logs/*` here, write `manifest.json` (the boundary, the hour, each file's
SHA-256, mtime and lines), check that no generated key or secret is in the files, and run
`python3 -m unittest discover -s gates`.
