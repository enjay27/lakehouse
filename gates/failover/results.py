"""What a failover run's records say (ADR-047 §4, §7). Pure: no cluster, no clock.

A run's record, written by driver.py, holds the failure's time `t0`, the cluster as polled
each second (`observations`), the operator's log since the failure, every write the probe
attempted (`probe`) and the rows its table held once every instance was back (`rows`). Every
time is epoch milliseconds, read on the runner, the one clock of the kind cluster.

- **The write outage:** the longest gap between two good writes, the second completed after
  the failure: from the last good write before the writes fail to the first good one after.
  A write that hangs and then succeeds counts as well as one that fails. With no good write
  after the last failure, the run did not recover, and the gap to the last attempt is a lower
  bound.
- **A confirmed write is lost** when its commit returned to the probe and its row is missing.
- **Commit latency:** p50 and p99, nearest rank, over the 60 s before the failure.
"""

import datetime as dt
import json
import math
import statistics

MODES = ("pooled", "per_write")
HEALTHY = "Cluster in healthy state"
FAILING_OVER = "Failing over"
LATENCY_WINDOW_MS = 60_000


def parse_records(text):
    """The probe's write records from its log; any other line is skipped."""
    records = []
    for line in text.splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict) and "mode" in record and "seq" in record:
            records.append(record)
    return records


def _of(records, mode):
    return [r for r in records if r["mode"] == mode]


def outage(records, mode, t0):
    """{outage_ms, recovered, last_good, first_good} for one mode."""
    mine = _of(records, mode)
    good = sorted(r["done"] for r in mine if r["ok"])
    longest, last_good, first_good = 0, None, None
    for before, after in zip(good, good[1:]):
        if after >= t0 and after - before > longest:
            longest, last_good, first_good = after - before, before, after
    failures = [r["done"] for r in mine if not r["ok"] and r["sent"] >= t0]
    recovered = bool(good) and good[-1] >= t0 and (not failures or good[-1] > max(failures))
    if not recovered:
        last_good = good[-1] if good else None
        end = max((r["done"] for r in mine), default=t0)
        longest = end - last_good if last_good is not None else None
        first_good = None
    return {"outage_ms": longest, "recovered": recovered, "last_good": last_good,
            "first_good": first_good}


def failed(records, mode, t0):
    return sum(1 for r in _of(records, mode) if not r["ok"] and r["sent"] >= t0)


def lost(records, mode, rows):
    """Sequence numbers whose commit returned and whose row is missing from `rows`, a set of
    (mode, seq)."""
    return sorted(r["seq"] for r in _of(records, mode) if r["ok"] and (mode, r["seq"]) not in rows)


def percentile(values, p):
    """Nearest rank; None for no values."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(1, math.ceil(p / 100 * len(ordered))) - 1]


def latency(records, mode, t0, window_ms=LATENCY_WINDOW_MS):
    samples = [r["done"] - r["sent"] for r in _of(records, mode)
               if r["ok"] and t0 - window_ms <= r["sent"] < t0]
    return {"p50_ms": percentile(samples, 50), "p99_ms": percentile(samples, 99),
            "samples": len(samples)}


def epoch_ms(value):
    """Epoch milliseconds from an RFC 3339 timestamp (CNPG's status, the operator's log) or
    epoch seconds; None for none."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return round(value * 1000)
    return round(dt.datetime.fromisoformat(value).timestamp() * 1000)


def events(observations, t0, old_primary):
    """When the cluster, polled each second, showed the failure detected, a standby promoted,
    the `-rw` Service naming it, and the cluster healthy again."""
    detected = promoted = route = healthy = None
    new_primary = old_primary
    for o in sorted(observations, key=lambda o: o["t"]):
        if o["t"] < t0:
            continue
        if detected is None and o["phase"] != HEALTHY:
            detected = o["t"]
        primary = o["current_primary"]
        if promoted is None and primary and primary != old_primary:
            new_primary = primary
            promoted = epoch_ms(o.get("current_primary_timestamp")) or o["t"]
        if route is None and promoted is not None and o["endpoints"] == [new_primary]:
            route = o["t"]
        if healthy is None and detected is not None and o["phase"] == HEALTHY:
            healthy = o["t"]
    return {"detected": detected, "promoted": promoted, "route_switched": route,
            "healthy": healthy, "new_primary": new_primary}


def log_detected(text, t0):
    """The time of the operator's first log line from `t0` on that names a failover."""
    for line in text.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        t = epoch_ms(entry.get("ts"))
        if t is not None and t >= t0 and "failover" in str(entry.get("msg", "")).lower():
            return t
    return None


def _seconds(t, t0):
    return None if t is None else round((t - t0) / 1000, 1)


def summarize(run):
    """One run's numbers, in seconds from the failure."""
    t0, probe = run["t0"], run["probe"]
    rows = {(mode, seq) for mode, seq in run["rows"]}
    found = events(run["observations"], t0, run["old_primary"])
    modes = {}
    for mode in MODES:
        out = outage(probe, mode, t0)
        modes[mode] = {"outage_s": None if out["outage_ms"] is None
                       else round(out["outage_ms"] / 1000, 1),
                       "recovered": out["recovered"], "failed": failed(probe, mode, t0),
                       "lost": len(lost(probe, mode, rows)),
                       "latency": latency(probe, mode, t0)}
    return {"setup": run["setup"], "scenario": run["scenario"], "run": run["run"],
            "node_down_s": run.get("node_down_s"), "old_primary": run["old_primary"],
            "new_primary": found["new_primary"], "settings": run.get("settings", {}),
            "events_s": {"detected": _seconds(found["detected"], t0),
                         "detected_log": _seconds(log_detected(run["operator_log"], t0), t0),
                         "promoted": _seconds(found["promoted"], t0),
                         "route_switched": _seconds(found["route_switched"], t0),
                         "healthy": _seconds(found["healthy"], t0)},
            "modes": modes}


def aggregate(summaries):
    """{(setup, scenario, mode): row}: median and worst over the runs."""
    groups = {}
    for s in summaries:
        for mode, m in s["modes"].items():
            groups.setdefault((s["setup"], s["scenario"], mode), []).append(m)
    table = {}
    for key, runs in sorted(groups.items()):
        outages = [m["outage_s"] for m in runs if m["outage_s"] is not None]
        p99 = [m["latency"]["p99_ms"] for m in runs if m["latency"]["p99_ms"] is not None]
        p50 = [m["latency"]["p50_ms"] for m in runs if m["latency"]["p50_ms"] is not None]
        table[key] = {"runs": len(runs),
                      "outage_median_s": statistics.median(outages) if outages else None,
                      "outage_worst_s": max(outages) if outages else None,
                      "all_recovered": all(m["recovered"] for m in runs),
                      "failed_median": statistics.median(m["failed"] for m in runs),
                      "lost_total": sum(m["lost"] for m in runs),
                      "lost_worst": max(m["lost"] for m in runs),
                      "latency_p50_median_ms": statistics.median(p50) if p50 else None,
                      "latency_p99_worst_ms": max(p99) if p99 else None}
    return table


def markdown(table):
    lines = ["| Setup | Failure | Mode | Runs | Outage median (s) | Outage worst (s) | "
             "All recovered | Failed writes, median | Confirmed and lost, total | "
             "Commit p50 (ms) | Commit p99, worst (ms) |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for (setup, scenario, mode), row in table.items():
        lines.append(f"| {setup} | {scenario} | {mode} | {row['runs']} | "
                     f"{row['outage_median_s']} | {row['outage_worst_s']} | "
                     f"{'yes' if row['all_recovered'] else 'no'} | {row['failed_median']} | "
                     f"{row['lost_total']} | {row['latency_p50_median_ms']} | "
                     f"{row['latency_p99_worst_ms']} |")
    return "\n".join(lines) + "\n"
