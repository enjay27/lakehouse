# ADR-009: The Iceberg catalog is Apache Polaris, from a local chart at a pinned version

- **Status:** Proposed
- **Date:** 2026-10-10
- **Related:** ADR-007, ADR-008, ADR-011, ADR-015

## Context

The lakehouse needs an Iceberg REST catalog. The archive runs Apache Polaris from a local copy
of its chart (`charts/polaris/`, version 1.6.0, upgraded and checked on OrbStack on
2026-09-18: metastore schema v4, management port 8182). The suite was written against
Polaris's REST API and its PostgreSQL schema. Proven on one OrbStack node only.

## Decision

- **Apache Polaris** is the Iceberg catalog, in `NS_CATALOG_POLARIS` (ADR-007), with its own
  PostgreSQL (ADR-008) and object storage for the warehouse (ADR-011).
- **Version pinned: 1.6.0**, chart and image together. An upgrade is a PR of its own, through
  the gates, that names the metastore schema change.
- **The chart is a local copy** under `charts/polaris/`, so the values Polaris actually uses
  are in this repository and rendered by the static gate.
- **Polaris itself is not modified:** no patched image, no custom schema (ADR-015). The suite
  measures upstream Polaris.
- Its management port is 8182 (Polaris's default differs in older documents); the chart
  values name it explicitly.

## Diagram

```mermaid
flowchart LR
    C["Iceberg clients,<br/>suite"] -->|"REST"| P["Polaris 1.6.0"]
    P --> PG[("PostgreSQL<br/>metastore")]
    P --> S3["object storage<br/>warehouse"]
```

## Alternatives considered

- **Upstream chart from a Helm repository:** less to maintain, but values drift silently
  between chart versions, and the archive needed changes Polaris's chart did not expose.
- **Another catalog** (Nessie, Hive Metastore, Unity Catalog OSS): the suite and every
  measurement so far target Polaris.

## Consequences

- Upgrades follow upstream releases by hand, each through the gates (ADR-021 proves the
  upgrade path).
- The local chart must be compared with upstream at each upgrade; the diff goes in the PR.
