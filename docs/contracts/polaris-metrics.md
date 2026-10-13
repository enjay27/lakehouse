# Polaris's metrics: what the observability team's Prometheus scrapes

What a release exposes for metrics and how a scraper reaches it (ADR-046). Read from the pinned
versions, Polaris 1.6.0 and CloudNativePG 1.30.1. The gate's smoke step `check-metrics` proves
it on every platform PR, through a Prometheus stand-in that loads
[polaris-prometheus-scrape.yaml](polaris-prometheus-scrape.yaml) as it is.

## Targets

All in Polaris's namespace (`NS_CATALOG_POLARIS` in `.env`):

| Job | Pods | Container port | Path |
|---|---|---|---|
| `polaris` | `app.kubernetes.io/name=polaris`, `app.kubernetes.io/instance=polaris` | 8182, named `polaris-mgmt` | `/q/metrics` |
| `polaris-pg` | `cnpg.io/cluster=polaris-pg`, `cnpg.io/podRole=instance` | 9187, named `metrics` | `/metrics` |

- Plain HTTP, Prometheus's text format, no authentication.
- Every series of the `polaris` job carries `application=Polaris`; realm IDs are not tags.
- CNPG caches the results of its queries for 30 s (`metricsQueriesTTL`), so a shorter scrape
  interval on `polaris-pg` repeats the same values.
- A release creates no `ServiceMonitor` or `PodMonitor`. How the scraper finds these targets
  is the observability team's own configuration; the scrape file is one way, for a plain
  Prometheus.

## Reaching them

- Polaris's namespace denies ingress by default (ADR-044). The scraper's namespace needs the
  label in `METRICS_NAMESPACE_LABEL` (one `key=value`), which opens 8182 on Polaris and 9187
  on polaris-pg's instances, and nothing else.
- Pod discovery needs `get`, `list` and `watch` on pods in Polaris's namespace.

## Series the gate requires

`check-metrics` fails unless each holds on the release under test. An upgrade of Polaris or
CNPG that renames one changes this file in the same PR.

| Series | Job | Holds |
|---|---|---|
| `up` | both | 1 for every running pod of each job |
| `http_server_requests_seconds_count` | `polaris` | a sample above 0 after the smoke tests' requests |
| `cnpg_collector_up` | `polaris-pg` | 1 for every instance |
