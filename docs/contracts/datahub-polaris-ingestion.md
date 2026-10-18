# Datahub's ingestion of Polaris → the policy endpoint: the contract

What Datahub's ingestion of Polaris's catalogs asks Polaris, and so what the policy endpoint must
allow and refuse its principal (ADR-076 §4). The catalog gateway's team implements the policy
on the target clusters; the gate's stand-in policy (`gates/opa/policy.rego`) is checked
against this file. The request Polaris sends the endpoint is
[polaris-opa-input.md](polaris-opa-input.md). Every Polaris or Datahub upgrade re-reads this
file against the new versions' source (ADR-009, ADR-071 §1).

**Source:** `apache/polaris` tag `apache-polaris-1.6.0` (commit `dd30600`) and
`datahub-project/datahub` tag `v1.7.0.1` (commit `e99431e`,
`metadata-ingestion/src/datahub/ingestion/source/iceberg/`), read on 2026-10-18. The smoke
gate's `gates/expectations/datahub-ingestion.json` holds the recorder to the rows below on
every run (ADR-034).

## The principal

- **Name:** `datahub_ingest`, one per Polaris realm. `actor.principal` is that name; it has no
  principal role, so `actor.roles` is empty.
- **How it comes to exist:** this repository's install creates it as root and resets its
  credentials to the ones `scripts/init_env.py` generates (ADR-076 §3), on every install.
- **Its token:** `client_credentials` with `scope` `PRINCIPAL_ROLE:ALL`. The token endpoint
  never asks the policy endpoint.

## What it must be allowed

One run per catalog, daily. Datahub v1.7.0.1's Iceberg source asks Polaris for these, without
access delegation (`X-Iceberg-Access-Delegation` empty), so no request reaches S3 for it:

| Request | `action` | Target |
|---|---|---|
| List the top-level namespaces of the catalog | `LIST_NAMESPACES` | the `CATALOG` |
| Load a namespace's properties | `LOAD_NAMESPACE_METADATA` | the `NAMESPACE` |
| List a namespace's tables | `LIST_TABLES` | the `NAMESPACE` |
| Load a table | `LOAD_TABLE` | the `TABLE_LIKE` entity |

Every catalog the gateway lists in `DATAHUB_INGEST_POLARIS_CATALOGS` must allow all four.
`GET /v1/config` asks the endpoint nothing.

## What it must be refused

Everything else, among it:

- any write: `CREATE_NAMESPACE`, `CREATE_TABLE_*`, `ADD_TABLE_SNAPSHOT`, `DROP_TABLE_*`, and
  every other `PolarisAuthorizableOperation` that changes an entity;
- `LOAD_TABLE_WITH_WRITE_DELEGATION` and `LOAD_TABLE_WITH_READ_DELEGATION`: no vended
  credentials;
- views (`LIST_VIEWS`, `LOAD_VIEW`), which the source does not read;
- `LIST_CATALOGS` and every management call: the catalogs come from `.env`, never a listing.

The smoke gate asks for a namespace create and a delegated load as `datahub_ingest` and
requires both refused with 403 (ADR-076 §6.4).

## What the install asks as root

Once per install, from a pod in Datahub's namespace (ADR-076 §3): a token, then
`CREATE_PRINCIPAL` (an existing principal answers 409, which is kept) and `RESET_CREDENTIALS`
for `datahub_ingest`, both with the target `ROOT`. Root must be allowed both. The install cannot
tell whether the gateway allows `datahub_ingest`'s reads: until it does, each run's loads get
403 and the CronJob shows failed Jobs.

## For Datahub's operators

- **Not ingested:** views, and namespaces below the top level. Datahub v1.7.0.1's source does
  not read them.
- **Removed:** a table dropped in Polaris leaves Datahub on the next run of its catalog, through
  stateful ingestion, each catalog under its own pipeline `polaris-<catalog>`.
- **Where each table lands:** the dataset
  `urn:li:dataset:(urn:li:dataPlatform:iceberg,<catalog>.<namespace>.<table>,PROD)`, the
  catalog its platform instance, in the container of its namespace.
