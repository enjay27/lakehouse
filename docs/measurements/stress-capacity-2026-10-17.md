# One Polaris replica's capacity under generational ZGC (Tier D), 2026-10-17

Stress Tier D (ADR-053 §7, standing under ADR-054), run by hand from
`docs/runbooks/stress-capacity.md` for ADR-061 §4, as amended by ADR-062 and ADR-063 (#182).
Raw records beside this file: `stress-capacity-2026-10-17.json` (the driver's `summary.json`)
and `stress-capacity-2026-10-17-timeline.json` (the Prometheus stand-in's series, every 15 s).
The baseline is `stress-capacity-2026-10-16.md`: the same replica under the image's own
Parallel collector.

**In short:** one replica with 4 CPUs and 8 GiB ran generational ZGC at a 50% heap share. Its
knees rose over the Parallel run's: 45 against 40 for commits, 250 and 275 against 125 for the
two read profiles, and 75 against 25 for the mixed profile. The 2-hour hold answered all
511,102 of its requests, and its longest pause was 1 ms, against 404 ms. The sizing for
12,000 req/s falls from 480 replicas to 160.

| Profile | Knee (req/s) | Parallel run | Polaris cores at the knee | Polaris CPU ms per request | Its collector's | polaris-pg primary CPU ms per request |
|---|---|---|---|---|---|---|
| commits | 45, plus the hot set's 4 commits/s | 40 | 1.78 | 17.4 | 1.5 | 1.3 |
| queries-conditional | 250 | 125 | 1.67 | 6.7 | 0.4 | 0.5 |
| queries-full | 275 | 125 | 3.08 | 11.2 | 0.8 | 0.5 |
| mixed | 75 | 25 | 1.03 | 11.7 | 0.9 | 0.8 |

**Against ADR-061 §4's criteria** (as ADR-062 and ADR-063 amend them), ZGC at 50% stays:
- `verify` passes except for ordinary commits that ran out of 409 retries: 3 such commits,
  nothing else;
- the hold's longest pause is under 10 ms: 1 ms;
- the mixed knee is above 25 req/s: 75.

## What was measured

- **Commit:** `1918199` on `claude/polaris-jvm-gc` (PR #183), which the JSON names.
- **Polaris** `apache/polaris:1.6.0@sha256:9738b2052dea20aabf0cd42521424ff963fee41b0ee888fef9f512efb256602a`,
  OpenJDK 21.0.11, with `values-lakehouse.yaml`'s `JAVA_OPTS`. Its command line, read from the
  pod: `java -XX:MaxRAMPercentage=50.0 -XX:+UseZGC -XX:+ZGenerational -XX:+ExitOnOutOfMemoryError -cp . -jar /deployments/quarkus-run.jar`.
  One replica, requests equal to limits: 4 CPUs, 8 GiB; heap maximum 4,096 MiB.
- **Everything else as on 2026-10-16:**
  - polaris-pg: CNPG 1.30.1, three instances of
    `ghcr.io/cloudnative-pg/postgresql:16.15-202610050823-minimal-trixie@sha256:37a7225a2577af6e7e274b50b278f89dcdaf7c2cf005a66402872f6ed39d58e3`,
    `failoverQuorum` on, no resource limits; `polaris-pg-1` primary throughout;
  - the stand-ins at 2 CPUs each: SeaweedFS `4.47@sha256:ce9e796f…`, OPA `1.21.0@sha256:9e001051…`;
  - k6 `2.3.0@sha256:9c2dee7f…`, limited to 2 CPUs;
  - kind v0.33.0 with three nodes of `kindest/node:v1.36.4`;
  - the Prometheus stand-in `v3.15.0`, the Barman Cloud Plugin v0.15.1, cert-manager v1.21.2,
    and the log batch `de86f42f373e`, whose CronJob ran once an hour. The console's pod stayed
    in ImagePullBackOff, as the runbook's step 1 does not build its image.
- **No component was throttled** over 0.1% of its CFS periods at any knee.
- **The server:** the same one, 8 cores, 16 threads, 62 GiB of memory, with the same six idle
  containers beside kind. Its CPU sensor read at most 88.5 °C over the run.
- **The overlay** `gates/stress/capacity.json`, unchanged: steps of 120 s; commits from 5 req/s
  in steps of 5, the others from 25 in steps of 25; the hold at 80% of the mixed knee for
  7,200 s, sampled every 60 s; k6 with 200 to 2,000 VUs. The hot set: 10 tables, 4 writers
  each, 10 s between commits.

## The ramps

Each ramp started after Polaris had been Ready with no restart for 60 s. No step had a restart
of Polaris or polaris-pg. Every step up to its knee passed with no error, except 3 at the
commits knee; the highest p99 is the step's worst kind of request.

| Profile | Rate | Requests | Errors | Polaris cores | Highest p99 (ms) | Result |
|---|---|---|---|---|---|---|
| commits | 5 to 40 | 2,274 to 10,880 | 0 | 0.42 to 1.48 | 873 at most (commit_hot) | passed |
| commits | 45 | 12,304 | 3 (0.02%) | 1.78 | 881 (commit_hot) | passed |
| commits | 50 | 14,562 | 3,412 (23.43%) | 2.63 | 10,001 | failed: errors |
| queries-conditional | 25 to 250 | 3,001 to 30,001 | 0 | 0.32 to 1.67 | 16 at most | passed |
| queries-conditional | 275 | 24,862 | 17,926 (72.10%) | 2.21 | 10,001 | failed: errors |
| queries-full | 25 to 275 | 3,001 to 33,000 | 0 | 0.33 to 3.08 | 16 at most | passed |
| queries-full | 300 | 24,996 | 19,434 (77.75%) | 2.03 | 10,001 | failed: errors |
| mixed | 25 to 75 | 4,217 to 10,575 | 0 | 0.51 to 1.03 | 629 at most (commit_hot) | passed |
| mixed | 100 | 13,620 | 0 | 1.43 | 5,659 (conditional_load) | failed: p99 |

- **The read profiles are now limited by load.** queries-conditional's p99 stayed between 12
  and 16 ms from 25 to 250 req/s, where the Parallel run had 318 ms at 125. queries-full
  reached 3.08 of its 4 cores at its knee: of the four, it is the one that uses its CPU.
- **Commits are limited by Polaris's CPU, as before.** At 50 req/s commits timed out after 10 s.
  The Parallel run failed at 45 with 3.68 cores.
- **Mixed failed at 100 req/s on a stall in the step's last seconds, not on CPU.** Through most
  of that step, Polaris's mean latency was about 11.6 ms with 2 to 3 requests in flight. At
  11:00:16 UTC, 235 requests were in flight with a mean of 1,010 ms, and live threads rose
  from 34 to 226, at 1.43 cores.
  - polaris-pg had nothing waiting and no long transaction. The longest pause was 1 ms.
  - Heap used was 3.5 GB of 4.29 GB at that sample. ZGC makes a thread wait for memory when
    it cannot collect fast enough (an allocation stall), and no metric here records that.
    The stall is therefore likely but **not confirmed**. Attempt 1, with a 5.6 GiB heap, passed
    mixed at 100 and 125 (see *Attempts*).
- **Below each knee, the collector costs 0.4 to 1.6 ms of CPU per request.** It rises to 4.6 to
  6.4 ms only in a failing step, where requests pile up. The report gives every step with the
  collector's threads apart (ADR-061 §4).

## The hold

The mixed profile at 60 req/s for 7,200 s, from 11:01 to 13:01 UTC:

- **511,102 requests, 0 failed.** k6 dropped 2 arrivals, which it never sent. 21,659
  ordinary and 28,600 hot commits, every one answered 200 on its first or a retried attempt.
  Polaris used 0.90 of its 4 cores on average.

  | Kind | p50 ms | p95 ms | p99 ms |
  |---|---|---|---|
  | commit | 38.9 | 44.4 | 47.7 |
  | commit_hot | 42.2 | 61.6 | 67.2 |
  | conditional_load | 10.5 | 15.3 | 18.6 |
  | config | 1.6 | 2.4 | 2.8 |
  | head_table | 7.7 | 11.6 | 13.6 |
  | list_namespaces | 2.3 | 3.3 | 3.8 |
  | list_tables | 2.4 | 3.3 | 3.9 |
  | token | 1.2 | 1.8 | 2.1 |

  Against the Parallel run's hold at 20 req/s, every p99 is lower at three times the rate
  (commit_hot: 67.2 against 370.5 ms).
- **GC:** 5,799 pauses, the longest 1 ms. Micrometer records each pause in whole milliseconds,
  so most read 0 ms, and the 0.044 s total is a floor (ADR-062).
- **Heap after GC:** 204 MiB at the first sample, 236 MiB at the last, 534 MiB at most. No sign
  of growth over 2 hours. Heap used reached 3,842 MiB of 4,096 MiB.
- **Memory working sets:** Polaris 6.35 to 6.37 GB of its 8 GiB limit. polaris-pg's three
  instances were under 0.31 GB together, SeaweedFS 1.2 to 1.5 GB, OPA 16 MiB.
- **Worker queue:** at most 1, nothing rejected. 120 samples, none failed.

## Sizing for 12,000 req/s (estimates)

ADR-053 §7's arithmetic on the mixed knee: **160 Polaris replicas** (12,000 ÷ 75) and **9.3
cores** for polaris-pg's primary (12,000 × 0.78 ms). These are estimates for the target's
hardware, to be checked against #9's answers.

The replica count is still not the CPU's. At the mixed profile's 11.7 ms per request, 4 cores
would serve about 343 req/s, about 35 replicas for 12,000 req/s. At its knee the replica used
1.03 cores, and the step past it failed on the stall above.

## Correctness (ADR-054 §2, ADR-063)

```
ok   100 tables loaded, 87226 snapshots newer than the seed
Commits: 87009 answered 200, 1248 unknown, 87226 new snapshots found
commits at 45 req/s: 3 failed request(s), 0 of them 5xx
```

- `verify` fails the run on the 3 commits at 45 req/s, the commits knee. They are ordinary
  commits that ran out of their 4 retries on 409s: Polaris answered that step's commits with
  5,877 200s and 275 409s, and nothing else. The Parallel run had 6 such commits, at 35 req/s.
  ADR-063 leaves these out of the collector's criterion; ADR-054 §2 still counts them.
- Every commit answered 200 is in its table's snapshot list once. The 1,248 unknown were cut
  short when their step ended.
- polaris-pg: `polaris-pg-1` primary throughout, no readiness probe failure. No container
  restarted.

## Deviations

As on 2026-10-16: `previous_versions_max` 5, the seed's one reused manifest list per table, and
the mixed profile's shares as ADR-053's assumption (#142).

- **Metadata growth:** seeded at 1,127,432 to 1,134,486 B. After the run, ordinary tables
  were 1,334,744 to 1,377,513 B, and the hot set's 2,180,098 to 2,760,324 B.
- **The heap maximum the seed printed is right this time.** At `13329dc` the driver added ZGC's
  two pools together and read 11,472 MiB for 5,736. `1918199` counts them once.
- **Memory outside the heap follows the requests in flight** (ADR-062). k6's 2,000 VUs bound
  them in this run; real clients do not, beside #179.

## Attempts

Two runs on 2026-10-17.

- **Attempt 1**, at `13329dc`, ZGC at a 70% heap share (ADR-061). Knees before it was stopped:
  - commits 45 req/s;
  - queries-conditional 250;
  - queries-full 200, its step at 225 failing on p99;
  - mixed passed 25 to 125, and the run was stopped during 150.

  At 275 req/s of queries-conditional, 08:38:48 UTC, the memory cgroup killed Polaris: ZGC's
  heap, all committed, 5.84 GB of shared memory, plus 2.43 GB outside it, with 1,590 requests
  in flight. The first step after the restart (queries-full at 25) had 3 failed requests. Kade
  stopped the run, and ADR-062 retried ZGC at 50%. Its records stay on the server.
- **Attempt 2**, at `1918199`, ZGC at 50%: the run above.

## Open

- **Whether the stall at mixed 100 req/s is ZGC's allocation stall.** A run with ZGC's log on
  (`-Xlog:gc`) would show it; the heap share it implies is a tuning value.
- **The replica's mixed knee is still a third of what its CPU would allow.**
