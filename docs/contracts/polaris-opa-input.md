# Polaris 1.6.0 → policy endpoint: the contract

The request Polaris sends to its OPA policy endpoint and the answer it acts on (ADR-026). The
catalog gateway's team implements the production side; the gate's stand-in policy
(`gates/opa/policy.rego`) is checked against it. Every Polaris upgrade re-reads this file
against the new version's source (ADR-009).

**Source:** `apache/polaris` tag `apache-polaris-1.6.0` (commit `dd306009d81a`), read on
2026-10-11. Paths below are relative to that tag; `OPA/` is
`extensions/auth/opa/impl/src/main/java/org/apache/polaris/extension/auth/opa/`. Everything
here was read in source and tests. The rows marked *seen* were also recorded on the wire
(ADR-034), from Polaris 1.6.0 serving PyIceberg 0.12.0 (`gates/iceberg/smoke.py`); the smoke
gate's `gates/expectations/iceberg.json` holds the recorder to them.

**Status upstream:** Beta (`OPA/OpaAuthorizationConfig.java:35-37`); Polaris logs a
production-readiness warning for it. Tests say the input will change when Polaris moves to
intent-based authorization (`OpaPolarisAuthorizerTest.java:802-805`).

## Configuration

| Key | Default | Notes |
|---|---|---|
| `polaris.authorization.type` | `internal` | `opa` selects the OPA authorizer |
| `polaris.authorization.opa.policy-uri` | none, required | The full URL Polaris POSTs to, used as given. It names the **package**: `http://<host>:8181/v1/data/polaris/authz` |
| `polaris.authorization.opa.http.timeout` | `PT2S` | The response timeout only; no connect timeout is set |
| `polaris.authorization.opa.http.verify-ssl` | `true` | `false` is a severe readiness issue: startup aborts |
| `polaris.authorization.opa.http.trust-store-path`, `-password` | none | For an endpoint with a private CA |
| `polaris.authorization.opa.auth.type` | `none` | `bearer` with `auth.bearer.static-token.value` or `auth.bearer.file-based.path` |

`OPA/OpaAuthorizationConfig.java:40-186`. **Ignore the keys in the shipped
`application.properties:238-239`** (`polaris.authorization.opa.url`, `...policy-path`): they do
not exist in the config mapping.

## The request

`POST <policy-uri>`, `Content-Type: application/json`, plus `Authorization: Bearer <token>`
when bearer auth is on (`OPA/OpaPolarisAuthorizer.java:245-268`). One request per authorized
operation. Body, snake_case, schema in `extensions/auth/opa/impl/opa-input-schema.json`:

```json
{"input": {
  "actor":    {"principal": "alice", "roles": ["reader"]},
  "action":   "LOAD_TABLE",
  "resource": {"targets": [
    {"type": "TABLE_LIKE", "name": "t1", "parents": [
      {"type": "ROOT", "name": "root_container", "parents": []},
      {"type": "CATALOG", "name": "cat1", "parents": []},
      {"type": "NAMESPACE", "name": "ns1", "parents": []}]}]},
  "context":  {"request_id": "<a UUID per request>"}}}
```

- `actor.roles` are the principal's **principal roles** only; catalog roles are never sent,
  and Polaris's own grants decide nothing in OPA mode.
- `action` is a `PolarisAuthorizableOperation` name
  (`polaris-core/.../auth/PolarisAuthorizableOperation.java`).
- `type` is a `PolarisEntityType` name: `ROOT`, `PRINCIPAL`, `PRINCIPAL_ROLE`, `CATALOG`,
  `CATALOG_ROLE`, `NAMESPACE`, `TABLE_LIKE` (tables **and** views), `POLICY`.
- Catalog paths start with the `ROOT` parent `root_container`; each level of a nested namespace
  is its own `NAMESPACE` parent; each parent carries `"parents": []`.
- `targets` and `secondaries` are **omitted when empty**, not sent as `[]`.

Upstream's documentation example (`site/.../external-pdp/opa.md:184-213`) differs from the
code in three ways: it shows `TABLE` (the code sends `TABLE_LIKE`), no `ROOT` parent, and
`"secondaries": []`. Follow the code.

### What is sent for common operations

| Request | `action` | Target |
|---|---|---|
| Create a catalog | `CREATE_CATALOG` | `ROOT`; the new catalog's name is not sent |
| List catalogs | `LIST_CATALOGS` | `ROOT` |
| Create a namespace | `CREATE_NAMESPACE` | the parent (the catalog, or the parent namespace); the new name is not sent |
| List namespaces | `LIST_NAMESPACES` | the catalog, or the parent namespace |
| Create a table | `CREATE_TABLE_DIRECT` (`..._WITH_WRITE_DELEGATION` with credential vending; `CREATE_TABLE_STAGED...` for a staged create); *seen*, both | the namespace; the table's name is not sent |
| Load a table | `LOAD_TABLE`; with credential vending `LOAD_TABLE_WITH_WRITE_DELEGATION`, and on a deny a second request, `LOAD_TABLE_WITH_READ_DELEGATION`; *seen*, all three | the `TABLE_LIKE` entity |
| Append to a table (PyIceberg's commit) | `ADD_TABLE_SNAPSHOT`, then `SET_TABLE_SNAPSHOT_REF`; *seen*. Never `UPDATE_TABLE` | the `TABLE_LIKE` entity |
| Drop a table | `DROP_TABLE_WITHOUT_PURGE`; *seen* | the `TABLE_LIKE` entity |
| Rotate a principal's credentials, by root | `ROTATE_CREDENTIALS`; *seen* | `ROOT` |

A client that sends `X-Iceberg-Access-Delegation: vended-credentials` (PyIceberg's default) to
a catalog without STS is allowed by the policy endpoint and then refused by Polaris itself:
see [polaris-s3-without-sts.md](polaris-s3-without-sts.md).

## The answer

`OPA/OpaPolarisAuthorizer.java:277-292`:

| Answer | Polaris does |
|---|---|
| HTTP 200, `{"result": {"allow": true, ...}}` | allows |
| HTTP 200, `allow` false or missing, or no `result` | denies: the caller gets 403 |
| HTTP 200, `{"result": true}` (a policy URI naming the rule, not the package) | **denies everything** |
| Any other status | denies: 403 |
| Timeout, refused connection, a body that is not a JSON object | fails the request: the caller gets **500** |

Nothing fails open.

## What does not ask the policy endpoint

- **Bootstrap** (the realm, the root principal and its credentials).
- A principal rotating its own credentials; `GET /v1/config`; the token endpoint.
- Everything else does, **root included**: in OPA mode root is `actor.principal == "root"`
  with roles `["service_admin"]`, and a policy that does not allow it locks root out.
