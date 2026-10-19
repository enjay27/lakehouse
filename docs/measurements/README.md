# Measurements

Numbers this repository has measured, each with its evidence (ADR-003). The first is
`stress-capacity-2026-10-16.md`, one Polaris replica's capacity (Tier D, #174).

## The rule

- **One file per measurement**, added in the PR that took it, with the run links and its raw
  records (JSON) beside it.
- **The versions measured** are named in the file: each component's version or image, and on
  the gate the stand-ins it ran against (ADR-031), on the plain upstream schema (ADR-015).
- **A server is described by cores, threads and memory only**: no hostnames, addresses or
  other names of non-local systems (ADR-002).
- **A measurement is a record, not a living document:** it is not edited to follow later
  ADRs, and a new run is a new file.

## Decided so far

| File | What | Decided by |
|---|---|---|
| `failover-<date>.md` | Failover on the gate, setup by failure, once C3a reproduces the target | ADR-047 §7 |
| `stress-capacity-<date>.md` | One Polaris's capacity on a Linux test server (Tier D) | ADR-053 §7 |
| A stress run's summary | A run on the gate worth keeping as a baseline | ADR-054 §3 |
| `smoke-time-<date>.md` | Smoke's phases, for a tuning value of upstream's charts | ADR-071 §2 |
