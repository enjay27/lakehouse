# One Polaris replica's capacity on a Linux test server (Tier D), 2026-10-16

Stress Tier D (ADR-053 §7, standing under ADR-054), run by hand from
`docs/runbooks/stress-capacity.md` (#174). Raw records beside this file:
`stress-capacity-2026-10-16.json` (the driver's `summary.json`) and
`stress-capacity-2026-10-16-timeline.json` (the Prometheus stand-in's series, every 15 s).

**In short:** one replica with 4 CPUs and 8 GiB reached these knees. The read and mixed knees
were set by p99 latency while Polaris used under half its CPU. Its JVM is the upstream image's,
with the Parallel collector, and full collections of up to 0.4 s stalled every request in flight.

| Profile | Knee (req/s) | Polaris cores at the knee | Polaris CPU ms per request | polaris-pg primary CPU ms per request |
|---|---|---|---|---|
| commits | 40, plus the hot set's 4 commits/s | 1.74 | 19.1 | 1.4 |
| queries-conditional | 125 | 1.29 | 10.3 | 0.6 |
| queries-full | 125 | 1.76 | 14.0 | 0.7 |
| mixed | 25 | 0.59 | 16.8 | 1.1 |

The 2-hour hold at 20 req/s of the mixed profile, 80% of its knee, answered every request with
no error, and Polaris's heap after collection ended lower than it began. No restart of Polaris
or polaris-pg in the run.

## What was measured

- **Commits:** the ramps at `7cac326`; the hold, `verify`, `report` and `timeline` at `75ae56b`
  (the JSON names that one), which changes only `scenario.js`'s
  token handling and `config.json`'s `token_refresh_s` (see *Attempts*). Both are on
  `claude/tier-d-capacity-e74222`.
- **Polaris** `apache/polaris:1.6.0@sha256:9738b2052dea20aabf0cd42521424ff963fee41b0ee888fef9f512efb256602a`,
  OpenJDK 21.0.11, the image's own JVM options:
  `-XX:MaxRAMPercentage=80.0 -XX:+UseParallelGC -XX:MinHeapFreeRatio=10 -XX:MaxHeapFreeRatio=20 -XX:GCTimeRatio=4 -XX:AdaptiveSizePolicyWeight=90`.
  One replica, requests equal to limits: 4 CPUs, 8 GiB; heap maximum 6,551.5 MiB.
- **polaris-pg:** CNPG 1.30.1, three instances of
  `ghcr.io/cloudnative-pg/postgresql:16.15-202610050823-minimal-trixie@sha256:37a7225a2577af6e7e274b50b278f89dcdaf7c2cf005a66402872f6ed39d58e3`,
  `failoverQuorum` on (#178), no resource limits. `polaris-pg-1` was primary throughout.
- **Stand-ins** (ADR-031), reached directly, 2 CPUs each from the overlay:
  `chrislusf/seaweedfs:4.47@sha256:ce9e796f1fe6f06968f4c04bdaf8f678dad9c8acdfef3d244133d71bfa6bf882`, `openpolicyagent/opa:1.21.0@sha256:9e0010512cf405e66bfd7b89394f514e56fe0464822df0527e780344ab151450`.
  Neither was throttled at any knee (at most 0.09% of CFS periods, OPA's, at 125 and 150 req/s
  of queries-conditional).
- **k6** `grafana/k6:2.3.0@sha256:9c2dee7f8ed74d317e4027c06a10f169b625638189de8d4555d0b3486a5aeb34`, limited to 2 CPUs, never throttled at a knee.
- **The cluster:** kind v0.33.0, three nodes of
  `kindest/node:v1.36.4@sha256:099e049362a1526b2db71494e1947aae99bd16290d7c895f2b7ea312e3cbfaed`, Docker 29.3.1, Linux 6.8. The Prometheus stand-in
  `prom/prometheus:v3.15.0@sha256:efd719c99d83b060d9daefdcf00360461adf279f45ef5391f8d111892118753e`,
  scraping Polaris's and polaris-pg's metrics (ADR-046). Also installed:
  `ghcr.io/cloudnative-pg/plugin-barman-cloud:v0.15.1`, cert-manager v1.21.2, and the log
  batch `ghcr.io/enjay27/lakehouse/polaris-log-batch:de86f42f373e`, whose CronJob ran once an
  hour. The console's pod never started: the runbook's step 1, like `stress.yml`, does not build its image
  (`lakehouse/polaris-console:3edb60fcb591`), and it stayed in ImagePullBackOff. The seed and `verify` ran on
  `python:3.12.14-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f`.
- **The server:** 8 cores, 16 threads, 62 GiB of memory. Its BIOS had been updated to the
  vendor's 2025-01-22 release after an unexplained power-off on the first attempt (see the
  runbook). It also ran six idle containers (a database, a cache, an object store with its
  console, a search engine with its dashboards), together under 1% of one core.
- **The overlay** `gates/stress/capacity.json` at those commits: steps of 120 s; commits from 5
  req/s in steps of 5, the three others from 25 in steps of 25, up to 30 steps each; the hold
  at 80% of the mixed knee for 7,200 s, sampled every 60 s; the stand-ins at 2 CPUs; k6 with
  200 to 2,000 VUs. `config.json`'s hot set: 10 tables, 4 writers each, 10 s between commits.

## The ramps

Each ramp started after Polaris had been Ready with no restart for 60 s. No step had a restart
of Polaris or polaris-pg, and none was throttled over 5%. Every knee is therefore Polaris's
(ADR-053 §4). Each step's numbers are in the JSON; the highest p99 is the step's worst kind.

| Profile | Rate | Requests | Errors | Polaris cores | Highest p99 (ms) | Result |
|---|---|---|---|---|---|---|
| commits | 5 | 2,298 | 0 | 0.53 | 983 | passed |
| commits | 10 | 3,486 | 0 | 0.66 | 520 | passed |
| commits | 15 | 4,692 | 0 | 0.90 | 818 | passed |
| commits | 20 | 5,906 | 0 | 1.11 | 674 | passed |
| commits | 25 | 7,230 | 0 | 1.44 | 1,557 | passed |
| commits | 30 | 8,430 | 0 | 1.55 | 1,188 | passed |
| commits | 35 | 10,200 | 6 (0.06%) | 1.74 | 2,712 | passed |
| commits | 40 | 10,942 | 0 | 1.74 | 1,087 | passed |
| commits | 45 | 22,424 | 557 (2.48%) | 3.68 | 2,991 | failed: errors |
| queries-conditional | 25 to 100 | 3,001 to 12,001 | 0 | 0.29 to 1.29 | 32 at most | passed |
| queries-conditional | 125 | 15,000 | 0 | 1.29 | 318 | passed |
| queries-conditional | 150 | 18,001 | 0 | 1.66 | 690 | failed: p99 |
| queries-full | 25 to 75 | 3,001 to 9,001 | 0 | 0.64 to 1.20 | 45 at most | passed |
| queries-full | 100 | 12,001 | 0 | 1.59 | 431 | passed |
| queries-full | 125 | 15,001 | 0 | 1.76 | 15 | passed |
| queries-full | 150 | 18,001 | 0 | 1.92 | 978 | failed: p99 |
| mixed | 25 | 4,239 | 0 | 0.59 | 647 (commit_hot) | passed |
| mixed | 50 | 7,503 | 0 | 1.23 | 3,966 | failed: p99 |

- **Commits** are limited by Polaris's CPU: at 45 req/s it jumped to 3.68 of its 4 cores, 557
  ordinary commits ran out of their 4 retries on 409s, and k6 dropped 4.4% of the arrivals.
  The errors are those 409s; no 5xx in any step. Commits' p99 is not judged against its 5 s
  cap before then.
- **Reads and the mixed profile are limited by pauses, not CPU.** Polaris's full collections
  (PS MarkSweep, cause "Ergonomics") reached 375 to 396 ms; each stalls every request in
  flight, and live threads rose from about 28 to 130 or more. A step passes or fails on
  whether such a pause lands in it: queries-full passed 125 req/s with a p99 of 15 ms after
  100 req/s had 431 ms. The knee is the last step that passed (ADR-053 §4), so these knees
  are the pauses' as much as the load's.
- **CPU per request is not flat.** queries-full cost 25.7 ms of Polaris CPU per request at
  25 req/s and 12.8 ms at 150, against 8.4 to 9.6 ms on attempt 4, whose ramp ran after only
  two steps of commits. Neither the table growth below nor the hot set explains a factor of
  two; it is left open.

## The hold

The mixed profile at 20 req/s for 7,200 s, from 21:26 to 23:27 UTC, at `75ae56b`, on attempt 6's
cluster after its ramps:

- **209,149 requests, 0 failed**, none dropped. With the hot set's writers that is 29 req/s
  at Polaris, 0.76 of its 4 cores on average; 7,150 ordinary and 28,454 hot commits, every one
  answered 200 on its first or a retried attempt.

  | Kind | p50 ms | p95 ms | p99 ms |
  |---|---|---|---|
  | commit | 39.1 | 56.8 | 114.2 |
  | commit_hot | 57.1 | 137.7 | 370.5 |
  | conditional_load | 15.4 | 28.7 | 80.7 |
  | config | 2.3 | 4.1 | 12.1 |
  | head_table | 12.4 | 24.8 | 81.4 |
  | list_namespaces | 3.4 | 5.5 | 11.5 |
  | list_tables | 3.4 | 5.9 | 14.2 |
  | token | 1.7 | 3 | 7.5 |

- **Heap after GC:** 223.7 MiB at the first sample, 108.0 MiB at the last, never higher than the
  first; heap used at most 4,348.9 MiB of 6,553.5 MiB. No sign of growth over 2 hours.
- **GC:** 169,247 pauses, 616 s in all (8.6% of the hold), the longest 404 ms. That longest
  pause is the same size as the ones that set the read knees.
- **Worker queue:** at most 0, nothing rejected. 120 samples, none failed.
- **Memory working sets:** Polaris 4.6 to 5.3 GiB of its 8 GiB limit, polaris-pg's three
  instances under 0.33 GiB together, SeaweedFS 0.9 to 1.3 GiB, OPA 16 MiB.

The timeline file covers 18:09 to 23:27 UTC: the ramps, attempt 6's first hold with its 401s
from 20:02, and this hold.

## Sizing for 12,000 req/s (estimates)

ADR-053 §7's arithmetic on the mixed knee: **480 Polaris replicas** (12,000 ÷ 25) and **13.4
cores** for polaris-pg's primary (12,000 × 1.1 ms). These are estimates for the target's
hardware, to be checked against #9's answers.

The replica count is the pauses', not the CPU's: at its knee the replica used 0.59 of its 4
cores. At the mixed profile's 16.8 ms per request, 4 cores would serve about 238 req/s, about
51 replicas for 12,000 req/s, if nothing but CPU limited it. The gap is the case for tuning
Polaris's collector before the target's sizing is taken from this file.

## Correctness (ADR-054 §2)

`verify` fails the run:

```
ok   100 tables loaded, 85319 snapshots newer than the seed
Commits: 85319 answered 200, 0 unknown, 85319 new snapshots found
commits at 35 req/s: 6 failed request(s), 0 of them 5xx
```

- Every commit answered 200 is in its table's snapshot list once; none was cut short (0
  unknown).
- polaris-pg: `polaris-pg-1` primary throughout; 5 readiness probe failures since the
  seed (`polaris-pg-1` 2, `polaris-pg-3` 3), recorded without judging (#180). No container
  restarted.
- The 6 failures at 35 req/s of commits are ordinary commits that ran out of their retries on
  409s, below the knee of 40, so ADR-054 §2 counts them.

## Deviations

- **`previous_versions_max` 5**, and the seed's one reused manifest list per table, as the
  runbook says: the metadata stays near 1 MB instead of growing with real manifests.
- **Metadata growth:** the scenario's commits only add snapshots. Seeded at 1,127,267 to
  1,134,409 B; after the run, ordinary tables 1,270,026 to 1,306,823 B and the hot set's
  2,557,010 to 3,229,293 B. The hot set nearly tripled over the run, the hold's commits among
  them.
- **The mixed profile's shares** are ADR-053's assumption until the target's metrics give them
  (#142).
- **The hot set** pauses 10 s between commits, not 2 s (see *Attempts*), so it adds about 4
  commits/s to every profile that commits.

## Attempts

Six runs on 2026-10-16. Attempts 1 to 3 are in #174: the server powered off under load until
its BIOS update, and an overloaded Polaris was restarted by its liveness probe (#179). From
attempt 4 on, each ramp waits for a settled Polaris.

- **Attempt 4**, at `67af211`: queries-conditional failed its first step, 500 req/s. At 350
  req/s of queries-full the liveness probe restarted Polaris, the driver's read of its metrics
  after the step was refused, and the driver stopped. `fc62ca6` fails that step instead, adds
  each step's error rate to the report, and starts every ramp low.
- **Attempt 5**, at `9d2fdf2` (with main's #180): the hot set's 40 writers sent about 19
  commits/s whatever the rate, and mixed failed its first step at 25 req/s, so no hold.
  `7cac326` sets the hot set's pause to 10 s.
- **Attempt 6**, at `7cac326`: the ramps above. Its first hold used the one token k6 fetched
  at its start; Polaris's tokens last an hour, and from minute 61 every request was a 401
  (85,035 of 191,053). Its first hour was steady: heap after GC 120 to 240 MiB, mean latency
  19 to 29 ms, GC 3 to 12% of the time, no 5xx. `75ae56b` refreshes each client's token every
  30 minutes; the hold ran again on the same cluster with the ConfigMap reloaded.
