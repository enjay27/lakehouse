#!/usr/bin/env python3
"""The hourly batch over Polaris's per-pod log files (ADR-014, ADR-024, ADR-036). Standard library
only. Ported from the archive (enjay27/lakehouse-k8s at 09a4773,
images/polaris-log-batch/polaris_log_batch.py), moved to UTC and to contract v1.

    Polaris pods   -- JSON, hourly .gz rolls -->  the claim polaris-logs:
                                                  polaris-<pod>.log[.<YYYY-MM-DD-HH>[.N].gz]
    this batch (CronJob "3 * * * *", UTC)   -->  processed-logs/YYYYMMDD-HH.jsonl
                                                  aggregated-logs/YYYYMMDD-HH.jsonl (written last)
                                                  malformed/YYYYMMDD-HH.jsonl
    the observability team's reader         -->  reads processed-logs/ and aggregated-logs/

SELECTION IS BY TIMESTAMP, NOT BY FILE. For hour H the batch reads every pod's rolls and its current
.log, and keeps the lines whose `timestamp` is in [H, H+1) UTC. A current file is read while Polaris
appends to it; only newline-terminated lines count.

ORPHANS. A current polaris-<pod>.log moves to done/ only when (1) the Kubernetes API no longer lists
its pod and (2) it is complete: its last line is in a published hour and it has been quiet for more
than 120 s. If the pod list cannot be read, no current file moves that run.

EXACTLY ONCE PER HOUR. The checkpoint is per hour. An hour is written to .tmp/, fsynced, renamed
into place, and only then checkpointed; a crash means the next run redoes that hour from the files
still in place, to the same lines with the same event_id (the SHA-1 of the raw line).

CONTRACT v1 (docs/contracts/polaris-logs.md): each processed line is Polaris's JSON object as
written plus `event_id` and `log_hour`; the aggregated file holds one summary row.
"""

import argparse
import datetime as dt
import errno
import fcntl
import gzip
import hashlib
import json
import os
import re
import ssl
import sys
import urllib.parse
import urllib.request
import zlib

UTC = dt.timezone.utc
HOUR = dt.timedelta(hours=1)
SCHEMA = "polaris-log-batch/1"  # the checkpoint's own format
CONTRACT_VERSION = 1  # docs/contracts/polaris-logs.md

QUIET_SECONDS = 120  # a file untouched this long is not being written right now
GRACE_SECONDS = 60  # hour H is publishable from H+1 plus this
RETENTION_SECONDS = 3 * 24 * 3600  # 3 days after publication (ADR-036)
MAX_HOURS_PER_RUN = 72  # catch-up bound; the rest follows next run
TAIL_BYTES = 256 * 1024  # enough to hold a stack-trace line when reading a file's last line

CURRENT_RE = re.compile(r"^polaris-(?P<pod>.+)\.log$")
ROLL_RE = re.compile(
    r"^polaris-(?P<pod>.+?)\.log\.(?P<hour>\d{4}-\d{2}-\d{2}-\d{2})(?:\.(?P<n>\d+))?\.gz$")
TS_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:?\d{2})?$")

DIRS = ("processed-logs", "aggregated-logs", "malformed", "done", ".tmp", ".state")


# ---------------------------------------------------------------------------- time


def parse_ts(value):
    """ISO-8601 (Polaris writes up to nanoseconds and an offset) -> an aware datetime in UTC, or
    None. A timestamp without an offset is read as UTC. Fractions beyond microseconds are
    truncated: selection only needs the hour, and the raw line, nanoseconds included, goes out.
    """
    if not isinstance(value, str):
        return None
    m = TS_RE.match(value.strip())
    if not m:
        return None
    y, mo, d, h, mi, s, frac, off = m.groups()
    micro = int((frac or "0")[:6].ljust(6, "0"))
    if off in (None, "", "Z"):
        tz = UTC
    else:
        sign = 1 if off[0] == "+" else -1
        hh, mm = int(off[1:3]), int(off[-2:])
        tz = dt.timezone(sign * dt.timedelta(hours=hh, minutes=mm))
    try:
        t = dt.datetime(int(y), int(mo), int(d), int(h), int(mi), int(s), micro, tzinfo=tz)
    except ValueError:
        return None
    return t.astimezone(UTC)


def floor_hour(t):
    return t.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def hour_key(h):
    return h.strftime("%Y%m%d-%H")


def hour_from_key(key):
    return dt.datetime.strptime(key, "%Y%m%d-%H").replace(tzinfo=UTC)


def roll_hour(text):
    return dt.datetime.strptime(text, "%Y-%m-%d-%H").replace(tzinfo=UTC)


def epoch(t):
    return t.timestamp()


# ---------------------------------------------------------------------------- files


class LogFile:
    """One polaris-<pod>.log or polaris-<pod>.log.<hour>[.N].gz at the top of the log directory."""

    __slots__ = ("name", "path", "kind", "pod", "roll_hour", "mtime", "size")

    def __init__(self, name, path, kind, pod, rhour, mtime, size):
        self.name, self.path, self.kind, self.pod = name, path, kind, pod
        self.roll_hour, self.mtime, self.size = rhour, mtime, size


def scan(log_dir, pod_prefix):
    """Top-level log files of pods whose name starts with `pod_prefix`. Subdirectories and other
    names are ignored."""
    out = []
    for name in sorted(os.listdir(log_dir)):
        path = os.path.join(log_dir, name)
        m = ROLL_RE.match(name)
        if m:
            kind, pod, rh = "roll", m.group("pod"), roll_hour(m.group("hour"))
        else:
            m = CURRENT_RE.match(name)
            if not m:
                continue
            kind, pod, rh = "current", m.group("pod"), None
        if not pod.startswith(pod_prefix):
            continue
        try:
            st = os.stat(path)
        except FileNotFoundError:  # rotated away between listdir and stat
            continue
        if not os.path.isfile(path):
            continue
        out.append(LogFile(name, path, kind, pod, rh, st.st_mtime, st.st_size))
    return out


class Deferred(Exception):
    """A roll could not be decoded but is fresh, probably still being compressed: retry next run."""


class Corrupt(Exception):
    """A roll could not be decoded and is old enough that it never will be."""


def read_file(f, now_epoch, quiet):
    """-> (complete_lines, unterminated_tail). Raises Deferred or Corrupt for an undecodable roll."""
    if f.kind == "roll":
        try:
            with gzip.open(f.path, "rb") as fh:
                data = fh.read()
        except (EOFError, OSError, zlib.error) as exc:
            if now_epoch - f.mtime < quiet:
                raise Deferred(f"{f.name}: {exc}") from exc
            raise Corrupt(f"{f.name}: {exc}") from exc
        lines = data.split(b"\n")
        if lines and lines[-1] == b"":
            lines.pop()
        return lines, None  # a closed roll has no tail being written
    with open(f.path, "rb") as fh:
        data = fh.read()
    parts = data.split(b"\n")
    tail = parts.pop()  # b"" when the file ends with a newline
    return parts, (tail or None)


def last_complete_line(path):
    """The last newline-terminated line of a (possibly large) current file, read from the end."""
    size = os.path.getsize(path)
    with open(path, "rb") as fh:
        fh.seek(max(0, size - TAIL_BYTES))
        chunk = fh.read()
    parts = chunk.split(b"\n")
    tail = parts.pop()
    if size > TAIL_BYTES and len(parts) <= 1:
        return None, tail or None  # no complete line inside the window: do not guess
    if size > TAIL_BYTES:
        parts = parts[1:]  # the first piece may be the end of a line cut by the seek
    return (parts[-1] if parts else None), (tail or None)


def parse_line(raw):
    """-> (record, UTC datetime), or (None, None) when the line is not a timestamped JSON object."""
    try:
        rec = json.loads(raw)
    except ValueError:
        return None, None
    if not isinstance(rec, dict):
        return None, None
    ts = parse_ts(rec.get("timestamp"))
    if ts is None:
        return None, None
    return rec, ts


def classify(f, lines):
    """Yield (line_no, raw, rec, ts, hour) for every line of one file.

    A malformed line has no timestamp of its own. It is booked to the hour of the nearest valid line
    before it in the same file (else the nearest after it, else the roll's hour, else the file's
    mtime hour), so it is reported exactly once, in one hour's malformed file.
    """
    parsed = [parse_line(raw) for raw in lines]
    fallback = f.roll_hour or floor_hour(dt.datetime.fromtimestamp(f.mtime, UTC))
    nxt = [None] * len(parsed)
    upcoming = None
    for i in range(len(parsed) - 1, -1, -1):
        if parsed[i][1] is not None:
            upcoming = floor_hour(parsed[i][1])
        nxt[i] = upcoming
    prev = None
    for i, (raw, (rec, ts)) in enumerate(zip(lines, parsed)):
        if ts is not None:
            prev = floor_hour(ts)
            yield i + 1, raw, rec, ts, prev
        else:
            yield i + 1, raw, None, None, (prev or nxt[i] or fallback)


def processed_doc(rec, raw, key):
    """Contract v1: Polaris's object as written, plus event_id and log_hour."""
    doc = dict(rec)
    doc["event_id"] = hashlib.sha1(raw).hexdigest()
    doc["log_hour"] = key
    return doc


def dumps(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


# ---------------------------------------------------------------------------- disk


def ensure_dirs(log_dir):
    for d in DIRS:
        os.makedirs(os.path.join(log_dir, d), exist_ok=True)


def fsync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(log_dir, dest, data):
    """Write to .tmp/, fsync, rename into place (same filesystem), fsync the directory."""
    tmp = os.path.join(log_dir, ".tmp", f"{os.path.basename(dest)}.{os.getpid()}")
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, dest)
    fsync_dir(os.path.dirname(dest))


def move_unique(src, dest_dir, name):
    """Move without ever overwriting: a taken name gets .1, .2, ..."""
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, name)
    n = 0
    while os.path.exists(dest):
        n += 1
        dest = os.path.join(dest_dir, f"{name}.{n}")
    os.rename(src, dest)
    return dest


def load_checkpoint(log_dir):
    path = os.path.join(log_dir, ".state", "checkpoint.json")
    if not os.path.exists(path):
        return {"schema": SCHEMA, "last_published": None, "hours": {}}
    with open(path) as fh:
        return json.load(fh)


def save_checkpoint(log_dir, cp):
    atomic_write(log_dir, os.path.join(log_dir, ".state", "checkpoint.json"),
                 json.dumps(cp, indent=1, sort_keys=True).encode())


# ---------------------------------------------------------------------------- pods


def k8s_pod_lister(selector):
    """Pod names in the batch's own namespace, with its ServiceAccount token (a Role that may only
    get and list pods, ADR-036)."""

    def lister():
        sa = "/var/run/secrets/kubernetes.io/serviceaccount"
        host = os.environ["KUBERNETES_SERVICE_HOST"]
        port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
        if ":" in host:
            host = f"[{host}]"
        with open(f"{sa}/namespace") as fh:
            ns = fh.read().strip()
        with open(f"{sa}/token") as fh:
            token = fh.read().strip()
        url = (f"https://{host}:{port}/api/v1/namespaces/{ns}/pods"
               f"?labelSelector={urllib.parse.quote(selector)}")
        ctx = ssl.create_default_context(cafile=f"{sa}/ca.crt")
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, context=ctx, timeout=10) as resp:
            data = json.load(resp)
        return {item["metadata"]["name"] for item in data.get("items", [])}

    return lister


# ---------------------------------------------------------------------------- one hour


def process_hour(cfg, files, h, now_epoch, pods, pod_error):
    """Everything for hour h, in memory. Nothing is written here."""
    key = hour_key(h)
    h0 = epoch(h)
    processed, malformed, sources, corrupt, pods_seen = [], [], [], [], {}

    for f in files:
        if f.mtime < h0:  # last written before the hour: it holds no line of it
            continue
        try:
            lines, _tail = read_file(f, now_epoch, cfg.quiet)
        except Corrupt as exc:
            corrupt.append({"file": f.name, "error": str(exc)})
            continue
        n_here = 0
        for line_no, raw, rec, _ts, hour in classify(f, lines):
            if hour != h:
                continue
            n_here += 1
            if rec is None:
                malformed.append({"reason": "not a timestamped JSON object", "file": f.name,
                                  "line_no": line_no, "raw": raw.decode("utf-8", "replace")})
                continue
            processed.append(processed_doc(rec, raw, key))
            pods_seen[f.pod] = pods_seen.get(f.pod, 0) + 1
        if n_here:
            sources.append({"file": f.name, "lines": n_here})

    # orphans and idle files: every current file, whatever its mtime, judged by its last line
    orphans, idle = [], []
    for f in files:
        if f.kind != "current":
            continue
        last, tail = last_complete_line(f.path)
        _, last_ts = parse_line(last) if last else (None, None)
        if last_ts is None or floor_hour(last_ts) > h:
            continue  # still in a later hour, or unreadable: leave it alone
        quiet = now_epoch - f.mtime > cfg.quiet
        entry = {"file": f.name, "pod": f.pod, "last_line_hour": hour_key(floor_hour(last_ts))}
        if pods is not None and f.pod not in pods and quiet:
            entry["unterminated_tail"] = tail is not None
            orphans.append((f, entry))
            if tail is not None:
                malformed.append({
                    "reason": "unterminated last line of an orphaned file (pod killed mid-write)",
                    "file": f.name, "line_no": None, "raw": tail.decode("utf-8", "replace")})
        elif pods is not None and f.pod in pods and floor_hour(last_ts) < h:
            idle.append(entry)

    summary = {
        "contract_version": CONTRACT_VERSION,
        "report_type": "summary",
        "hour": key,
        "window_start": h.isoformat(),
        "window_end": (h + HOUR).isoformat(),
        "lines_in": len(processed) + len(malformed),
        "processed": len(processed),
        "malformed": len(malformed),
        "pods": sorted(pods_seen),
        "by_pod": dict(sorted(pods_seen.items())),
        "sources": sources,
        "corrupt_files": corrupt,
        "orphans_moved": None if pods is None else [e for _, e in orphans],
        "idle_log_files": None if pods is None else idle,
        "pod_list_error": pod_error,
    }
    return {"key": key, "processed": processed, "malformed": malformed, "summary": summary,
            "orphans": orphans, "corrupt": corrupt}


def publish(cfg, result, now_epoch):
    """Malformed and processed first, the aggregated file last: once it exists, the hour is
    complete (ADR-036)."""
    d, key = cfg.log_dir, result["key"]
    if result["malformed"]:
        atomic_write(d, os.path.join(d, "malformed", f"{key}.jsonl"),
                     "".join(dumps(m) + "\n" for m in result["malformed"]).encode())
    atomic_write(d, os.path.join(d, "processed-logs", f"{key}.jsonl"),
                 "".join(dumps(p) + "\n" for p in result["processed"]).encode())
    summary = dict(result["summary"])
    summary["published_at"] = dt.datetime.fromtimestamp(now_epoch, UTC).isoformat()
    atomic_write(d, os.path.join(d, "aggregated-logs", f"{key}.jsonl"),
                 (dumps(summary) + "\n").encode())
    return summary


def housekeep_after(cfg, files, h, result):
    """After hour h is checkpointed: rolls named for hours up to h and orphans go to done/, corrupt
    rolls to malformed/files/. Never a current file of a listed pod."""
    d = cfg.log_dir
    moved = []
    corrupt_names = {c["file"] for c in result["corrupt"]}
    for f in files:
        if not os.path.exists(f.path):
            continue
        if f.name in corrupt_names:
            moved.append(move_unique(f.path, os.path.join(d, "malformed", "files"), f.name))
        elif f.kind == "roll" and f.roll_hour <= h:
            day = f.roll_hour.strftime("%Y%m%d")
            moved.append(move_unique(f.path, os.path.join(d, "done", day), f.name))
    for f, entry in result["orphans"]:
        if not os.path.exists(f.path):
            continue
        lh = hour_from_key(entry["last_line_hour"])
        name = f"{f.name}.{lh.strftime('%Y-%m-%d-%H')}.orphan"
        moved.append(move_unique(f.path, os.path.join(d, "done", lh.strftime("%Y%m%d")), name))
    return moved


def retention(cfg, now_epoch):
    d, cutoff, removed = cfg.log_dir, now_epoch - cfg.retention, 0
    for sub in ("processed-logs", "aggregated-logs", "malformed", "done"):
        root = os.path.join(d, sub)
        for dirpath, _dirnames, filenames in os.walk(root, topdown=False):
            for name in filenames:
                p = os.path.join(dirpath, name)
                if os.stat(p).st_mtime < cutoff:
                    os.remove(p)
                    removed += 1
            if dirpath != root and not os.listdir(dirpath):
                os.rmdir(dirpath)
    return removed


# ---------------------------------------------------------------------------- run


class Config:
    def __init__(self, log_dir, pod_prefix="polaris-", pod_lister=None, quiet=QUIET_SECONDS,
                 grace=GRACE_SECONDS, retention=RETENTION_SECONDS, max_hours=MAX_HOURS_PER_RUN,
                 dry_run=False):
        self.log_dir, self.pod_prefix, self.pod_lister = log_dir, pod_prefix, pod_lister
        self.quiet, self.grace, self.retention = quiet, grace, retention
        self.max_hours, self.dry_run = max_hours, dry_run


def first_hour(files, now_epoch, quiet):
    """The earliest hour of any valid line on disk, for a run with no checkpoint yet. Roll names are
    not enough: a roll of 11 can open with a 10:59:59.9 line written after the rotation."""
    earliest = None
    for f in files:
        try:
            lines, _ = read_file(f, now_epoch, quiet)
        except (Deferred, Corrupt):
            continue
        for raw in lines:
            _, ts = parse_line(raw)
            if ts is not None and (earliest is None or ts < earliest):
                earliest = ts
    return floor_hour(earliest) if earliest else None


def run(cfg, now=None, out=None):
    """One CronJob run. Returns a dict describing what happened, also printed as JSON lines."""
    out = out or sys.stdout
    now = now or dt.datetime.now(UTC)
    now_epoch = epoch(now)
    ensure_dirs(cfg.log_dir)
    lock = open(os.path.join(cfg.log_dir, ".state", "lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        lock.close()
        if exc.errno in (errno.EAGAIN, errno.EACCES):
            print(dumps({"event": "skipped", "reason": "another run holds the lock"}), file=out)
            return {"status": "locked", "published": []}
        raise
    try:
        return _run_locked(cfg, now, now_epoch, out)
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def _run_locked(cfg, now, now_epoch, out):
    cp = load_checkpoint(cfg.log_dir)
    files = scan(cfg.log_dir, cfg.pod_prefix)
    last_ready = floor_hour(now - dt.timedelta(seconds=cfg.grace)) - HOUR
    if cp["last_published"]:
        h = hour_from_key(cp["last_published"]) + HOUR
    else:
        h = first_hour(files, now_epoch, cfg.quiet)
    if h is None:
        print(dumps({"event": "idle", "reason": "no log files and no checkpoint"}), file=out)
        return {"status": "idle", "published": []}

    pods, pod_error = None, None
    if cfg.pod_lister is not None:
        try:
            pods = set(cfg.pod_lister())
        except Exception as exc:  # noqa: BLE001 -- any failure means: move no current file
            pod_error = f"{type(exc).__name__}: {exc}"
    else:
        pod_error = "pod listing disabled"

    published, status, n = [], "ok", 0
    while h <= last_ready and n < cfg.max_hours:
        try:
            result = process_hour(cfg, files, h, now_epoch, pods, pod_error)
        except Deferred as exc:
            status = "deferred"
            print(dumps({"event": "deferred", "hour": hour_key(h), "reason": str(exc)}), file=out)
            break
        if cfg.dry_run:
            print(dumps({"event": "dry-run", **result["summary"]}), file=out)
        else:
            summary = publish(cfg, result, now_epoch)
            cp["last_published"] = result["key"]
            cp["hours"][result["key"]] = {"published_at": summary["published_at"],
                                          "processed": summary["processed"],
                                          "malformed": summary["malformed"],
                                          "sources": summary["sources"]}
            save_checkpoint(cfg.log_dir, cp)
            moved = housekeep_after(cfg, files, h, result)
            files = scan(cfg.log_dir, cfg.pod_prefix)
            print(dumps({"event": "published", **summary, "moved": len(moved)}), file=out)
        published.append(result["key"])
        h += HOUR
        n += 1

    if not cfg.dry_run:
        cutoff = now_epoch - cfg.retention
        cp["hours"] = {k: v for k, v in cp["hours"].items()
                       if epoch(dt.datetime.fromisoformat(v["published_at"])) >= cutoff}
        save_checkpoint(cfg.log_dir, cp)
        removed = retention(cfg, now_epoch)
        if removed:
            print(dumps({"event": "retention", "removed": removed}), file=out)
    return {"status": status, "published": published, "pod_list_error": pod_error}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--log-dir", default=os.environ.get("LOG_DIR", "/deployments/logs"))
    ap.add_argument("--pod-prefix", default=os.environ.get("POD_PREFIX", "polaris-"))
    ap.add_argument("--selector", default=os.environ.get("POD_SELECTOR",
                                                         "app.kubernetes.io/name=polaris"))
    ap.add_argument("--no-pod-list", action="store_true",
                    help="skip the API: publish, but move no current file")
    ap.add_argument("--now", help="ISO time to act as now (the gate, replays)")
    ap.add_argument("--dry-run", action="store_true", help="compute and print, write nothing")
    args = ap.parse_args(argv)
    now = None
    if args.now:
        now = parse_ts(args.now)
        if now is None:
            ap.error(f"--now {args.now!r} is not an ISO-8601 time")
    cfg = Config(args.log_dir, pod_prefix=args.pod_prefix,
                 pod_lister=None if args.no_pod_list else k8s_pod_lister(args.selector),
                 dry_run=args.dry_run)
    result = run(cfg, now=now)
    return 0 if result["status"] in ("ok", "idle", "locked", "deferred") else 1


if __name__ == "__main__":
    sys.exit(main())
