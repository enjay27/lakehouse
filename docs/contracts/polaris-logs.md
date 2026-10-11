# The Polaris log files: contract v2

What the log batch hands to the observability team's Fluent Bit (ADR-024): where the files
are, what each line holds, and when an hour is complete. The decisions are ADR-014, ADR-024
and ADR-036; this file is the format they call a contract. A change to it is a new version,
announced to the observability team before it ships (ADR-024).

Lines measured on 2026-10-11 from Polaris 1.6.0 (the image pinned in
`charts/polaris/values-lakehouse.yaml`) with the file appender this repository sets. Every
Polaris upgrade re-checks this file (ADR-009).

The machine-readable form is five JSON Schemas (2020-12) next to this file: one for a
processed line, `polaris-logs-processed.schema.json`, and one per row type of the aggregated
file, named by its `report_type`: `polaris-logs-summary`, `-resource`, `-principal` and
`-app-dropped.schema.json`. The smoke gate checks every line the stand-in Fluent Bit reads
against them (`gates/contract.py`).

## Where

The claim `polaris-logs` (`ReadWriteMany`) in `NS_CATALOG_POLARIS`. The observability team's
reader mounts it in that namespace and reads two directories:

| Path | What |
|---|---|
| `processed-logs/YYYYMMDD-HH.jsonl` | The lines of that UTC hour the audit policy keeps, one JSON object per line |
| `aggregated-logs/YYYYMMDD-HH.jsonl` | A summary row for that hour, then its resource, principal and app_dropped rows. **Written last: once it exists, the hour is complete** |

Everything else on the claim is not part of the contract: Polaris's own files
(`polaris-<pod>.log` and their hourly `.gz` rolls), and the batch's `malformed/`, `done/`,
`.tmp/` and `.state/`.

- **Hours are UTC.** `YYYYMMDD-HH` names the hour `[HH:00, HH+1:00)`.
- **Each file appears whole, by a rename.** A reader never sees a half-written file.
- **Every hour gets a file, even an empty one.** A `processed-logs/` file may hold no line.
- **Files are deleted 3 days after publication**, so the reader picks an hour up within 3 days.
- **The batch runs at minute 3 of every hour.** An hour is normally complete a few minutes
  after it ends; after an outage, the batch catches up to 72 hours per run, oldest first.

## Which lines are kept: the audit policy

The batch reads every line of the hour, then keeps some and counts the rest; nothing is dropped
without a number in the aggregated file. The policy is the archive's policy v5
(`enjay27/lakehouse-k8s` at `09a4773`), the batch port of the Fluent Bit Lua filter it ran
before, and gives the same lines and counters as that filter (checked again on 2026-10-11 with
the archive's `logging/scripts/step16-batch-lua-parity.py`), with one change: Polaris 1.6.0 logs
commits from `org.apache.polaris.service.catalog.iceberg.LocalIcebergCatalog`, where the
archive's Polaris had `IcebergCatalog` (measured on 2026-10-11). The first rule that matches
wins:

| # | A line that is | Kept? |
|---|---|---|
| * | any line whose message holds an unmasked `clientSecret` | The value becomes `<redacted>` and `secret_redacted: true` is added, before any rule |
| 1 | level `ERROR` or `WARN` | **Kept** |
| 2a | from `LocalIcebergCatalog`: `Successfully committed to table/view ... in N ms` | Its time is counted on the table's or view's row, then 2c |
| 2b | from `IcebergExceptionMapper` or `PolarisServiceImpl` | Decided by its request's access line (below) |
| 2c | from any other application logger | Dropped, counted per logger (`app_dropped`) |
| 3' | an access line with status 404 | Dropped, counted (`counted_404`) |
| 3 | an access line with status 400 or above, or one that does not parse | **Kept** |
| 4 | an access line for PUT, DELETE or PATCH | **Kept** |
| 5 | an access line for a POST under `/api/management/` | **Kept** |
|   | an access line for any other POST | Dropped, counted (`counted_post`) |
| 6 | an access line for a successful GET or HEAD | Dropped, counted (`counted_read`) |
| 7 | anything else | **Kept** |

So every refused request (401, 403) and every change to principals, roles and grants is kept.

**A request's application lines (2b).** They are decided by the access line with the same
`mdc.requestId`, the nearest one at or after them within 30 s, else the nearest before them,
across the hour's boundary: a 404 drops them too (`app_dropped_404`); any other status keeps
them; with no access line within 30 s they are kept with `held_orphan: true`
(`held_orphans`); with no request ID they are kept.

## A processed line

The JSON object Polaris wrote, without four fields, plus what the batch adds. UTF-8, one
object per line.

| Field | Type | From | What |
|---|---|---|---|
| `timestamp` | string | Polaris | When it was logged, UTC, up to nanoseconds: `2026-10-11T12:40:47.753600155Z` |
| `sequence` | integer | Polaris | The log record's sequence number in its pod |
| `loggerName` | string | Polaris | The category, e.g. `io.quarkus.http.access-log` |
| `level` | string | Polaris | `TRACE`, `DEBUG`, `INFO`, `WARN`, `ERROR` or `FATAL` |
| `message` | string | Polaris | The message, its client secret redacted |
| `threadName` | string | Polaris | The thread |
| `threadId` | integer | Polaris | The thread's id |
| `mdc` | object | Polaris | `requestId` and `realmId` while a request is served; `{}` otherwise |
| `hostName` | string | Polaris | The pod's name |
| `client_ip`, `user_principal_name`, `http_method`, `api_path` | string | the batch | On an access line: parsed from its message. `user_principal_name` is `-` for a request without a principal |
| `http_status`, `response_size` | integer | the batch | On an access line; `response_size` is 0 where the log has `-` |
| `access_log_parse_error` | `true` | the batch | On an access line whose message does not parse |
| `secret_redacted` | `true` | the batch | When a client secret was redacted |
| `held_orphan` | `true` | the batch | Rule 2b found no access line for the request |
| `event_id` | string | the batch | SHA-1, 40 hex digits, of the line's raw bytes as Polaris wrote them: the same line always gets the same id, so a reader can use it as the document id |
| `log_hour` | string | the batch | The hour the line was published in, `YYYYMMDD-HH` |

Polaris's `loggerClassName`, `ndc`, `processName` and `processId` are removed. Other fields may
appear, and a reader must accept fields it does not know. One is known: `exception`, which
Polaris's JSON format adds when a log record carries one. Measured on 2026-10-11: a request
Polaris 1.6.0 answered 503 wrote one such line, `IcebergExceptionMapper`'s `Full
RuntimeException` at `INFO`, which rule 2b keeps; one it answered 500 (its S3 endpoint did not
answer) wrote `Unhandled exception returning INTERNAL_SERVER_ERROR` at `ERROR`, which rule 1
keeps (`gates/fixtures/polaris-1.6.0-roll/`); its 400, 404 and 409 answers wrote none. Its
shape:

| Field | Type | What |
|---|---|---|
| `exceptionType` | string | The Java class, e.g. `org.apache.iceberg.exceptions.RuntimeIOException` |
| `message` | string | The exception's message |
| `frames` | array | The stack, each `{"class", "method", "line"}`: 65 frames on the line measured |
| `causedBy` | object | `{"exception": {...}}`, the cause in the same shape, when there is one |
| `suppressed` | array | The suppressed exceptions in the same shape, when there are some: the S3 client's failed attempts on the 500 |
| `refId` | integer | The exception's number within the record, from 1 |

Such a line is long: about 15 KB on the 503, 40 KB on the 500 with five suppressed.

**Every HTTP request** Polaris serves on its API port writes one access line, whose `message`
is the access log line (`client - principal [time] "METHOD path HTTP/1.1" status bytes`) and
whose `mdc.requestId` is the caller's `X-Request-ID`, or one Polaris generates when the caller
sent none. A request refused for want of a token is logged the same way, with principal `-`
and bytes `-` (measured on 2026-10-11). The management port's health probes are not logged.
Request bodies, tokens and client secrets are not in the lines.

Example (wrapped here): a request without a token, kept by rule 3.

```json
{"timestamp":"2026-10-11T12:40:48.283104402Z","sequence":1771,
 "loggerName":"io.quarkus.http.access-log","level":"INFO",
 "message":"172.17.0.1 - - [11/Oct/2026:12:40:48 +0000] \"GET /api/management/v1/catalogs HTTP/1.1\" 401 -",
 "threadName":"vert.x-eventloop-thread-3","threadId":27,
 "mdc":{"requestId":"gate-req-0002","realmId":"POLARIS"},
 "hostName":"polaris-7f4d69c67c-aaaaa","client_ip":"172.17.0.1","user_principal_name":"-",
 "http_method":"GET","api_path":"/api/management/v1/catalogs","http_status":401,
 "response_size":0,"event_id":"…40 hex digits…","log_hour":"20261011-12"}
```

## The aggregated file

The summary row first, then resource rows, principal rows and app_dropped rows, each sorted by
its key. Every row starts with the same fields:

| Field | Type | What |
|---|---|---|
| `contract_version` | `2` | This contract's version |
| `report_type` | string | `summary`, `resource`, `principal` or `app_dropped` |
| `window_start`, `window_end` | string | The hour's bounds, `+00:00` |
| `window_seconds` | `3600` | The hour's length |

### The summary row

| Field | Type | What |
|---|---|---|
| `hour` | string | `YYYYMMDD-HH` |
| `policy` | `"policy-v5"` | The audit policy that kept the lines |
| `lines_in` | integer | Lines read for the hour: `processed + dropped + malformed` |
| `processed`, `dropped` | integer | Lines kept in the hour's `processed-logs/` file, and lines the policy counted instead |
| `malformed` | integer | Lines that were not a timestamped JSON object, kept out of the hand-off |
| `pods` | array of strings | Pods with a line in the hour |
| `by_pod` | object | Lines per pod |
| `sources` | array | The files read: `file`, `lines` |
| `corrupt_files` | array | Rolls that could not be decoded: `file`, `error` |
| `orphans_moved` | array or null | Files of pods that are gone, moved aside after this hour: `file`, `pod`, `last_line_hour`, `unterminated_tail`. `null` when the pod list could not be read |
| `idle_log_files` | array or null | Files of live pods whose last line is older than the hour |
| `pod_list_error` | string or null | Why the pod list could not be read |
| `access_seen`, `access_kept`, `access_counted` | integer | Access lines read, kept, and counted instead: `access_kept = access_seen - access_counted` |
| `counted_read`, `counted_post`, `counted_404` | integer | Access lines counted by rule 6, rule 5 and rule 3' |
| `errors_kept`, `parse_errors` | integer | Access lines kept by rule 3, and those that did not parse |
| `errors_4xx`, `errors_5xx`, `auth_denied`, `bytes_total` | integer | Over every access line: statuses 400-499 (401 and 403 also in `auth_denied`), 500 and above, and response bytes |
| `distinct_resources`, `distinct_principals` | integer | Resource rows with a request, principal rows |
| `resources_other`, `resources_other_distinct`, `principals_other`, `role_keys_forced` | integer | What the row caps did (below) |
| `app_dropped_total`, `app_dropped_404`, `held_orphans` | integer | Application lines dropped by rule 2c, dropped with their 404, kept without an access line |
| `min_record_time`, `max_record_time` | string | The first and last access line's `timestamp`; absent in an hour without one |
| `message` | string | The counters in one sentence, for people |
| `published_at` | string | When the batch published it, `+00:00` |

An hour whose counts do not add up (`lines_in`, `dropped = access_counted + app_dropped_total
+ app_dropped_404`) is not published: the batch fails and the next run tries again.

### A resource row

One per resource, keyed by `resource`: the path without its query, cut after the first of 21
patterns that matches (`/tables/t/metrics` counts on `/tables/t`; a grant
`/catalog-roles/cr/grants` on the role `cr`). A commit `cat.a.b.t` counts on
`/api/catalog/v1/cat/namespaces/a%1Fb/tables/t`.

| Field | Type | What |
|---|---|---|
| `resource` | string | The key |
| `resource_kind` | string | `table`, `view`, `namespace`, `catalog`, `principal`, `principal-role`, `catalog-role`, `collection`, `auth`, `config`, `transaction`, `other`; `error` for `__errors__` |
| `api_kind` | string | `catalog`, `management`, `other`; `mixed` for `__errors__` and `__other__` |
| `requests`, `reads`, `writes` | integer | Access lines, GET/HEAD, POST/PUT/DELETE/PATCH |
| `errors`, `errors_4xx`, `errors_5xx`, `auth_denied` | integer | Statuses 400 and above, split as in the summary |
| `response_bytes` | integer | Response bytes |
| `last_read_bytes`, `last_write_bytes` | integer | The size of the last successful read or write, when there was one |
| `commit_count`, `commit_ms_sum`, `commit_ms_min`, `commit_ms_max` | integer | The commits Polaris logged, when there were some |

**Caps.** At most 500 resource rows; the rest count on `__other__`. A failed request makes no
row of its own unless its resource succeeded or committed somewhere in the hour: it counts on
`__errors__`, so a client knocking on names that do not exist cannot fill the rows. Failed
requests on roles may still make up to 100 rows (`role_keys_forced`), since a refused grant is
what a role's row is there to show.

### A principal row

One per caller, keyed by `user_principal_name` (`-` for a request without one), at most 200
(the rest count on `__other__`): the same counters as a resource row, without
`last_*_bytes`.

### An app_dropped row

One per application logger whose lines rule 2c dropped, at most 50 (the rest count on
`__other__`): `logger_name` and `dropped`. A logger that appears here for the first time is
worth a look.

## Versions

- **v1** (PR #49): Polaris's lines as written, every one of them, and the summary row. Shipped
  in no release.
- **v2** (this file, #45): the audit policy decides which lines are kept, the report rows join
  the summary. The first contract a release ships. Corrected before any release shipped it
  (#54): rule 2a names Polaris 1.6.0's `LocalIcebergCatalog`, so commits reach the rows instead
  of only the `app_dropped` counts; the `exception` field's shape is recorded.
