# Smoke's wall time and Datahub's tuning values, 2026-10-19

Smoke runs on `gate/smoke/exp-*` branches for #229 (from #225), each one change on `main` at
`cd3804d`, to find what sets Smoke's wall time. ADR-071 §2 keeps upstream's probes and resources
as tuning values that a measurement changes in the values; this is that measurement, for
GMS's and the frontend's readiness probes and for the CPU limit of the system-update and
restore-indices Jobs. The per-run numbers are in `smoke-time-2026-10-19.json`; each run's
timelines are in its Actions log and in its comment on #229.

**In short:** upstream's 500m CPU limit on the system-update and restore-indices Jobs, and its
readiness probes at 60 s then every 30 s, set most of the `datahub-recovery` job's time. With
the probes at 20 s then every 5 s, and the Jobs' limit at 2 CPUs, the run's wall time fell from
29.9 min to 17.4 min, and the recovery job (15.0 min) no longer sets it: the `smoke` job does.

| Branch (one change each, from `cd3804d`) | Run | `smoke` job | `datahub-recovery` job | Run wall |
|---|---|---|---|---|
| `exp/baseline` (none) | 39192555349 | 20.7 min | 29.7 min | 29.9 min |
| `exp/probes` | 39192654730 | 19.5 min | 24.8 min | 24.9 min |
| `exp/probes`, second run | 39207112253 | 19.9 min | 22.5 min | 22.7 min |
| `exp/probes-cpu` (probes and the CPU limit) | 39207205151 | 17.4 min | 15.0 min | 17.4 min |

Phases of `check-datahub-recovery` (ADR-077 §3's timeline), in seconds:

| Phase | baseline | probes | probes-cpu |
|---|---|---|---|
| OpenSearch's loss: system-update Job | 255 | 243, 247 | 98 |
| OpenSearch's loss: `helm --wait` until GMS rolled | 141 | 44, 46 | 41 |
| OpenSearch's loss: non-blocking Job | 80 | 81, 88 | 27 |
| restore-indices Job | 89 | 82, 85 | 32 |
| Kafka's loss: system-update Job | 80 | 76, 81 | 26 |
| Kafka's loss: `helm --wait` until GMS rolled | 132 | 37, 37 | 37 |
| Kafka's loss: non-blocking Job | 71 | 69, 72 | 22 |
| GMS deleted and ready again | 138 | 37, 31 | 37 |
| `check-datahub-recovery` in all | 1079 | 777, 777 | 421 |

In the `smoke` job's install, this commit's system-update Job took 167 s and its non-blocking
Job 101 s with the probes alone (`exp/combo-serial`, run 39203762337), and 70 s and 34 s with the
CPU limit too.

## What was measured

- **Runner:** GitHub's hosted `ubuntu-24.04`, 4 vCPUs and 16 GiB, one per job; kind with three
  nodes on it (ADR-017, ADR-029).
- **Datahub:** upstream's chart 1.1.7 (ADR-071, ADR-072), images `v1.7.0.1` by the digests in
  `charts/datahub/values-lakehouse.yaml`; OpenSearch `2.19.6`, Kafka `4.3.1`, datahub-pg on CNPG
  1.30.1 with PostgreSQL 16.15, as `main` at `cd3804d` pins them.
- **The stand-ins** of ADR-031, as the gate runs them.
- **Changed in the values only:** readiness `initialDelaySeconds` 20, `periodSeconds` 5,
  `failureThreshold` 56 (GMS) and 32 (the frontend), keeping upstream's 300 s and 180 s windows;
  `limits.cpu` "2" on `datahubSystemUpdate` and `datahubUpgrade.restoreIndices`, requests 300m
  as upstream's. Liveness probes and every other resource stay upstream's.

## Spread

Second runs of two candidates (#229): a run's wall time moved by 0.2 and 2.2 min, a job's by up
to 2.3 min, `check-datahub-recovery` by at most 17 s, and no phase by 30 s or more. A difference
under about 2 min between two single runs is not read as a result.

The system-update Job after OpenSearch's loss took 243 s and 247 s with the probes alone, against
151 s and 163 s in other runs: repeated, so a property of that set-up rather than noise. The CPU
limit removes it (98 s); its cause was not found.

## Deviations

- One run of `exp/probes-cpu`. The gate runs of the PR that adopts it are its second.
- Other cuts measured on the same runs (one recovery job per store, no second GMS restart, the
  install's chains in parallel, JVM flags on the Jobs) are in #229 and are not adopted: each
  needs an ADR or stays within the spread once the CPU limit is in.
