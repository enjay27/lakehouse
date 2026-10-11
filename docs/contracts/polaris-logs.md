# The Polaris log files: contract v1

What the log batch hands to the observability team's Fluent Bit (ADR-024): where the files
are, what each line holds, and when an hour is complete. The decisions are ADR-014, ADR-024
and ADR-036; this file is the format they call a contract. A change to it is a new version,
announced to the observability team before it ships (ADR-024).

Lines measured on 2026-10-11 from Polaris 1.6.0 (the image pinned in
`charts/polaris/values-lakehouse.yaml`) with the file appender this repository sets. Every
Polaris upgrade re-checks this file (ADR-009).

The machine-readable form is two JSON Schemas (2020-12) next to this file:
`polaris-logs-processed.schema.json` and `polaris-logs-summary.schema.json`. The smoke gate
checks every line the stand-in Fluent Bit reads against them (`gates/contract.py`).

## Where

The claim `polaris-logs` (`ReadWriteMany`) in `NS_CATALOG_POLARIS`. The observability team's
reader mounts it in that namespace and reads two directories:

| Path | What |
|---|---|
| `processed-logs/YYYYMMDD-HH.jsonl` | Every line Polaris wrote in that UTC hour, one JSON object per line |
| `aggregated-logs/YYYYMMDD-HH.jsonl` | One summary row for that hour. **Written last: once it exists, the hour is complete** |

Everything else on the claim is not part of the contract: Polaris's own files
(`polaris-<pod>.log` and their hourly `.gz` rolls), and the batch's `malformed/`, `done/`,
`.tmp/` and `.state/`.

- **Hours are UTC.** `YYYYMMDD-HH` names the hour `[HH:00, HH+1:00)`.
- **Each file appears whole, by a rename.** A reader never sees a half-written file.
- **Every hour gets a file, even an empty one.** A `processed-logs/` file may hold no line.
- **Files are deleted 3 days after publication**, so the reader picks an hour up within 3 days.
- **The batch runs at minute 3 of every hour.** An hour is normally complete a few minutes
  after it ends; after an outage, the batch catches up to 72 hours per run, oldest first.

## A processed line

The JSON object Polaris wrote, unchanged, plus two fields the batch adds. UTF-8, one object per
line.

| Field | Type | From | What |
|---|---|---|---|
| `timestamp` | string | Polaris | When it was logged, UTC, up to nanoseconds: `2026-10-11T12:40:47.753600155Z` |
| `sequence` | integer | Polaris | The log record's sequence number in its pod |
| `loggerClassName` | string | Polaris | The logging facade's class |
| `loggerName` | string | Polaris | The category, e.g. `io.quarkus.http.access-log` |
| `level` | string | Polaris | `TRACE`, `DEBUG`, `INFO`, `WARN`, `ERROR` or `FATAL` |
| `message` | string | Polaris | The message |
| `threadName` | string | Polaris | The thread |
| `threadId` | integer | Polaris | The thread's id |
| `mdc` | object | Polaris | `requestId` and `realmId` while a request is served; `{}` otherwise |
| `ndc` | string | Polaris | Empty in what was measured |
| `hostName` | string | Polaris | The pod's name |
| `processName` | string | Polaris | The JVM's executable |
| `processId` | integer | Polaris | The JVM's process id |
| `event_id` | string | the batch | SHA-1, 40 hex digits, of the line's raw bytes: the same line always gets the same id, so a reader can use it as the document id |
| `log_hour` | string | the batch | The hour the line was published in, `YYYYMMDD-HH` |

Other fields may appear: Polaris's JSON format adds `exception` when a log record carries one
(not seen in what was measured). A reader must accept fields it does not know.

**Every HTTP request** Polaris serves on its API port writes one line with `loggerName`
`io.quarkus.http.access-log`, whose `message` is the access log line
(`client - principal [time] "METHOD path HTTP/1.1" status bytes`) and whose
`mdc.requestId` is the caller's `X-Request-ID`, or one Polaris generates when the caller sent
none. The management port's health probes are not logged. Request bodies, tokens and client
secrets are not in the lines.

Example (wrapped here):

```json
{"timestamp":"2026-10-11T12:40:47.753600155Z","sequence":1770,
 "loggerClassName":"org.jboss.logging.Logger","loggerName":"io.quarkus.http.access-log",
 "level":"INFO",
 "message":"172.17.0.1 - root [11/Oct/2026:12:40:47 +0000] \"GET /api/management/v1/catalogs HTTP/1.1\" 200 15",
 "threadName":"executor-thread-2","threadId":31,
 "mdc":{"requestId":"gate-req-0001","realmId":"POLARIS"},"ndc":"",
 "hostName":"polaris-7f4d69c67c-aaaaa","processName":"/usr/lib/jvm/java-21-openjdk/bin/java",
 "processId":1,"event_id":"…40 hex digits…","log_hour":"20261011-12"}
```

## The summary row

One line, no other field allowed:

| Field | Type | What |
|---|---|---|
| `contract_version` | `1` | This contract's version |
| `report_type` | `"summary"` | The only row type in v1 |
| `hour` | string | `YYYYMMDD-HH` |
| `window_start`, `window_end` | string | The hour's bounds, `+00:00` |
| `published_at` | string | When the batch published it, `+00:00` |
| `lines_in` | integer | Lines read for the hour: `processed + malformed` |
| `processed` | integer | Lines in the hour's `processed-logs/` file |
| `malformed` | integer | Lines that were not a timestamped JSON object, kept out of the hand-off |
| `pods` | array of strings | Pods with a line in the hour |
| `by_pod` | object | Lines per pod |
| `sources` | array | The files read: `file`, `lines` |
| `corrupt_files` | array | Rolls that could not be decoded: `file`, `error` |
| `orphans_moved` | array or null | Files of pods that are gone, moved aside after this hour: `file`, `pod`, `last_line_hour`, `unterminated_tail`. `null` when the pod list could not be read |
| `idle_log_files` | array or null | Files of live pods whose last line is older than the hour |
| `pod_list_error` | string or null | Why the pod list could not be read |

## Versions

- **v1** (this file): Polaris's lines as written, and the summary row.
- **v2** (planned, before the first release, #45): the archive's audit policy decides which
  lines are kept, and report rows join the summary. v1 ships in no release.
