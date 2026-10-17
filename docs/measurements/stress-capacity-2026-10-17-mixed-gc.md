# ZGC's allocation stalls in one Polaris replica's mixed ramp (Tier D, mixed only), 2026-10-17

A shorter Tier D run for #186: the mixed profile's ramp only, twice, with ZGC's own log on.
It asks whether the stall that ended `stress-capacity-2026-10-17.md`'s mixed ramp at 100 req/s
was ZGC's allocation stall: a thread waiting for memory because the collector could not free
it fast enough. It does not replace that file's knees or sizing: it runs no other profile and
no hold.

Run by hand from `docs/runbooks/stress-capacity.md` (ADR-053 §7, standing under ADR-054),
steps 1 to 5 and 7 to 8. The raw records stay on the Linux test server, as in
`stress-capacity-2026-10-17.md`'s *Deviations*: the driver's output, among it `summary.json`
(SHA-256 `a4fc1175…`), `verify.json` (`34e2a0e0…`) and the Prometheus stand-in's
`timeline.json` (`5b135b0b…`); ZGC's log lines `gc-raw.log` (`13d09ab7…`); and the run's log
(`f200ecdc…`). Every number below comes from them.

**In short:** yes, ZGC stalls allocation at a 4 GiB heap, 3,093 times over the two ramps. The
stalls come at the start of a step, never later in it:
- **Every stall fell in the first 2 to 6 s of its step,** except in ramp 1's failing step,
  where they went on for 32 s. Of the 14 collections ZGC started on an allocation stall,
  11 began with the heap full (4,096 of 4,096 MiB).
- **They line up with the latency:** Polaris's mean latency rose to 38 to 509 ms in the
  first 45 s of a step, then held at 10.8 to 17.2 ms with at most 8 requests in flight.
- **Both ramps passed 100, 125 and 150 req/s** and failed at 175 on p99, so the knee was 150
  twice, against 75 in `stress-capacity-2026-10-17.md`. Its stall at 100 came at the end of
  its step, which this run never showed, and it ran with no GC log: that one stays
  unexplained.

## What was measured

- **Commit:** `72ad91d` on `main`, which `summary.json` names. Polaris's chart values as on
  2026-10-17's run, from `values-lakehouse.yaml`.
- **Polaris** `apache/polaris:1.6.0@sha256:9738b2052dea20aabf0cd42521424ff963fee41b0ee888fef9f512efb256602a`,
  OpenJDK 21.0.11. Its command line, read from the pod:
  `java -XX:MaxRAMPercentage=50.0 -XX:+UseZGC -XX:+ZGenerational -XX:+ExitOnOutOfMemoryError -Xlog:gc:stdout:utctime,uptime,level,tags -cp . -jar /deployments/quarkus-run.jar`.
  One replica, requests equal to limits: 4 CPUs, 8 GiB; heap maximum 4,096 MiB.
- **Everything else as on 2026-10-17's run,** at the same images:
  - polaris-pg: CNPG 1.30.1, three instances of
    `ghcr.io/cloudnative-pg/postgresql:16.15-202610050823-minimal-trixie@sha256:37a7225a2577af6e7e274b50b278f89dcdaf7c2cf005a66402872f6ed39d58e3`;
    `polaris-pg-1` primary throughout;
  - SeaweedFS `4.47@sha256:ce9e796f…` and OPA `1.21.0@sha256:9e001051…` at 2 CPUs each;
  - k6 `2.3.0@sha256:9c2dee7f…`, limited to 2 CPUs;
  - three nodes of `kindest/node:v1.36.4`, the Prometheus stand-in `v3.15.0`, the Barman
    Cloud Plugin v0.15.1, cert-manager v1.21.2, and the log batch `de86f42f373e`.
- **No component was throttled** in any step: k6, OPA and SeaweedFS at 0.0% of their CFS
  periods.
- **The server:** the same one, 8 cores, 16 threads, 62 GiB of memory, with the same six idle
  containers beside kind. Its sensors read at most 83.1 °C.
- **The overlay** `gates/stress/capacity.json`, unchanged: steps of 120 s from 25 req/s in
  steps of 25; k6 with 200 to 2,000 VUs.
- **The tables were fresh:** the run seeded them (1,127,209 to 1,134,423 B) and ran the mixed
  ramp next. On 2026-10-17 three other profiles ran before mixed and grew them first (*The
  two runs differ*).

## The ramps

Each ramp started after Polaris had been Ready with no restart for 60 s. No step had an error
or a restart. The highest p99 is the step's worst kind of request. A stall's length is how
long one thread waited.

| Ramp | Rate | Requests | Highest p99 (ms) | Polaris CPU ms per request | Allocation stalls | Their total, s | Longest, ms | Result |
|---|---|---|---|---|---|---|---|---|
| 1 | 25 | 4,213 | 544 (commit_hot) | 17.4 | not logged | | | passed |
| 1 | 50 | 7,341 | 782 (commit_hot) | 12.1 | not logged | | | passed |
| 1 | 75 | 10,527 | 772 (commit_hot) | 11.1 | 41 | 11.1 | 349 | passed |
| 1 | 100 | 13,704 | 607 (commit_hot) | 10.4 | 51 | 14.7 | 330 | passed |
| 1 | 125 | 16,865 | 1,020 (commit_hot) | 10.6 | 84 | 28.2 | 398 | passed |
| 1 | 150 | 19,995 | 1,021 (commit_hot) | 10.4 | 218 | 43.8 | 498 | passed |
| 1 | 175 | 23,169 | 1,732 (commit_hot) | 12.2 | 2,197 | 375.6 | 680 | failed: p99 |
| 2 | 25 | 4,228 | 384 (commit_hot) | 13.3 | 0 | 0 | 0 | passed |
| 2 | 50 | 7,401 | 715 (commit_hot) | 11.4 | 54 | 14.3 | 328 | passed |
| 2 | 75 | 10,559 | 442 (commit_hot) | 10.8 | 26 | 3.6 | 188 | passed |
| 2 | 100 | 13,667 | 800 (commit_hot) | 10.6 | 82 | 15.5 | 275 | passed |
| 2 | 125 | 16,802 | 727 (commit_hot) | 9.5 | 98 | 18.6 | 244 | passed |
| 2 | 150 | 19,967 | 820 (commit_hot) | 11.4 | 92 | 21.3 | 293 | passed |
| 2 | 175 | 23,096 | 965 (commit_hot) | 11.3 | 150 | 31.6 | 293 | failed: p99 |

- **Ramp 1 failed at 175 widely:** conditional_load 1,377 ms, head_table 1,182 ms and four
  other kinds over their 500 ms caps. **Ramp 2 failed narrowly:** conditional_load 531 ms,
  the only kind over its cap.
- **At the knee, 150 req/s, Polaris used 1.56 and 1.71 of its 4 cores** (10.4 and 11.4 CPU
  ms per request), and its collector 0.8 to 0.9 ms of that.

## ZGC's log

- **3,093 allocation stalls and 393 relocation stalls** from 14:49:43 UTC to the run's end.
  2,885 of the allocation stalls were Polaris's request threads (`executor-thread`). 157
  were Vert.x event-loop threads, each of which serves many connections at once. The rest
  were 51 stalls on 12 other threads.
- **609 collections:** 552 minor and 29 major on ZGC's allocation-rate rule; 9 major
  proactive; 5 minor on high usage; and 14 minor on an allocation stall. A collection's
  time below is its whole concurrent cycle, not a pause.
  - **The 552 minor collections on the allocation-rate rule** took the heap from a median of
    3,352 MiB (quartiles 3,180 to 3,478) to 1,075 MiB (876 to 1,280). The median cycle took
    0.21 s.
  - **Of the 14 on an allocation stall,** 11 began with the heap full, 4,096 MiB, and three
    at 2,806 to 3,822 MiB. They left 1,818 to 4,096 MiB and took 0.30 to 1.94 s. The one at
    14:59:48 UTC freed nothing (4,096 to 4,096 MiB); the one at 15:00:08 ended higher than it
    began (3,822 to 4,096 MiB).
- **When in the step:** in ramp 2 the stalls fell 1.9 to 5.8 s after each step's start. In
  ramp 1 they fell 1.7 to 4.5 s after it, up to 150 req/s. At 175 they went on from 2.2 to
  32.2 s, 1,101 of them between 25 and 30 s.
- **The timeline agrees.** Its mean latency is over 30 s. Within each step's first 45 s,
  it rose to:
  - ramp 1: 77, 87, 57, 59, 82, 97 and 509 ms, from 25 to 175 req/s;
  - ramp 2: 72, 70, 38, 52, 62, 91 and 153 ms.
  - Live threads rose to 70 to 131, and to 226 in ramp 1's step at 175.
  - In the rest of every step, the median was 10.8 to 17.2 ms, with at most 8 requests in
    flight. At 25 req/s in ramp 2 the latency rose to 72 ms with no stall at all: a step's
    start costs something besides the collector.
- **The 2026-10-17 run shows the same spike at the start of every mixed step.** Its mean
  latency read 52 to 90 ms, live threads 72 to 92, then about 12 ms.

## The two runs differ

| | 2026-10-17 | This run |
|---|---|---|
| Profiles before mixed | commits, queries-conditional, queries-full | none |
| Tables at the mixed ramp's start | grown by the commits of the profiles before it (after the whole run: ordinary 1.33 to 1.38 MB, the hot set's 2.18 to 2.76 MB) | fresh: 1.13 MB |
| Mixed knee | 75, the step at 100 failing on a stall at its end | 150 twice, the step at 175 failing on stalls at its start |
| ZGC's log | off | on |

- **A full load of a larger table allocates more.** That fits the lower knee, but this run
  did not measure it: one difference at a time would be a mixed ramp on grown tables.
- **The run on 2026-10-17 failed on a stall at the end of its step** (11:00:16 UTC: 235 in
  flight, mean 1,010 ms, threads 34 to 226). This run never showed one, and that run had no
  GC log. Whether it was an allocation stall stays open.

## Correctness (ADR-054 §2)

```
ok   100 tables loaded, 15065 snapshots newer than the seed
Commits: 15065 answered 200, 0 unknown, 15065 new snapshots found
No correctness fault.
```

- **No container restarted.** polaris-pg: `polaris-pg-1` primary throughout, one readiness
  probe failure on it.
- **Metadata growth:** after the run, ordinary tables were 1,157,824 to 1,177,555 B, and the
  hot set's 1,366,442 to 1,403,034 B.

## Deviations

- **`-Xlog:gc` in Polaris's `JAVA_OPTS`,** added in step 3 to this run only. The rest of the
  command line is `values-lakehouse.yaml`'s. The chart does not change.
- **Only the mixed profile, twice, and no hold.** The driver writes a ramp's files under the
  profile's name, so ramp 1's were renamed `mixed-r1` before ramp 2. `verify` read both
  ramps' commits.
- **ZGC's log for the first 2.5 steps is lost.** The run streamed Polaris's output with
  `kubectl logs -f`, which failed on the server with `too many open files`. At 125 req/s,
  Polaris wrote about 7 MB of output a minute, and the kubelet rotates its 10 MiB log file about every
  90 s, so the earliest lines were gone by the time this was seen. From 14:49:43 UTC on, a
  loop read every log file of Polaris's container on its node every 30 s, the rotated ones
  too, and kept the `[gc]` lines. Ramp 1's steps at 25 and 50 req/s are therefore not in the
  log, as the table shows.
- **The raw records stay out of the repository,** as on 2026-10-17.

## Corrections

- **2026-10-18:** the figures for ZGC's collections, in *In short* and *ZGC's log*, were first
  written from three of the 14 collections on an allocation stall, and from the two most
  frequent minor collections. They now cover all of them, from the same `gc-raw.log`.

## Open

- **Whether the knee is set by the start of a step or by the rate:** a step starts at its
  full rate at once, and every stall falls in its first seconds. Clients that ramp up more
  gradually could see another knee. The p99 a step is judged by includes those seconds.
- **Whether the lower knee on 2026-10-17 came from its grown tables.**
- **What the heap share does to the stalls:** the 70% attempt on 2026-10-17 passed mixed at
  100 and 125 req/s on grown tables and was stopped during 150. Its memory limit had killed
  Polaris once before, in queries-conditional (ADR-062). The heap share is a tuning value
  (ADR-062 §2).
