# Polaris 1.6.0 on S3 without STS: who holds which key

How Polaris 1.6.0 reads and writes a catalog on S3-compatible storage that offers no STS
(ADR-031), and what an Iceberg client must send. Measured on 2026-10-11 against the pinned
images (Polaris 1.6.0, SeaweedFS 4.47, OPA 1.21.0, PyIceberg 0.12.0 with PyArrow 25.0.1)
through the recorder (ADR-034); the code paths are from `apache/polaris` tag
`apache-polaris-1.6.0`. Every Polaris upgrade re-checks this file (ADR-009).

## The catalog

- `storageConfigInfo` with `stsUnavailable: true`, `pathStyleAccess: true` and `endpoint`;
  no role ARN is needed.
- Polaris then vends no credentials (`AwsCredentialsStorageIntegration.compute`) and reads and
  writes table metadata with the **AWS SDK's default credentials**: `AWS_ACCESS_KEY_ID` and
  `AWS_SECRET_ACCESS_KEY` in its environment. `polaris.storage.aws.access-key` and
  `secret-key`, which the chart's `storage.secret` sets, reach only the STS client
  (`StorageConfiguration.stsCredentials`). With neither, every table create fails: "Unable to
  load credentials from any of the providers in the chain".
- `charts/polaris/values-lakehouse.yaml` therefore passes the `polaris-s3` Secret both ways.

## A key in the catalog's properties

Polaris's own S3 client also takes the catalog's `table-default.*` properties, so a catalog
created with `table-default.s3.access-key-id` and `table-default.s3.secret-access-key` works
without a key in Polaris's environment. But `table-default.*` properties become every new
table's properties, so the **secret key is then written into each table's `metadata.json`**
on S3 and returned in every load-table answer, to any principal allowed to load the table; the
management API's catalog answer shows it too. Plain `s3.access-key-id` in the catalog's
properties is ignored by Polaris. This repository never puts a key in a catalog's properties;
`gates/iceberg/smoke.py` checks that a table's metadata holds none.

## The client

- The client writes data files and manifests with **its own S3 key**, from its own
  configuration (PyIceberg: `s3.endpoint`, `s3.region`, `s3.access-key-id`,
  `s3.secret-access-key`); Polaris writes only `*.metadata.json`.
- It must **not ask for vended credentials**. PyIceberg sends
  `X-Iceberg-Access-Delegation: vended-credentials` by default; Polaris 1.6.0 then answers
  every create and load of a table with 400 "Credential vending was requested for table …, but
  no credentials are available" (`IcebergCatalogHandler`, asserted by upstream's
  `RestCatalogMinIOSpecialIT`). The create has already written the table when it answers 400.
  PyIceberg's `header.X-Iceberg-Access-Delegation` set to an empty string turns it off.
- Without delegation the policy endpoint is asked plain actions (`CREATE_TABLE_DIRECT`,
  `LOAD_TABLE`); with it, `*_WITH_WRITE_DELEGATION` and `*_WITH_READ_DELEGATION`
  ([polaris-opa-input.md](polaris-opa-input.md)).
