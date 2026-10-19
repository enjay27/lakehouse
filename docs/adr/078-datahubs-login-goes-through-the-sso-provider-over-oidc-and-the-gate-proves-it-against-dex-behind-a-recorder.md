# ADR-078: Datahub's login goes through the SSO provider over OIDC, and the gate proves it against Dex behind a recorder

- **Status:** Proposed
- **Date:** 2026-10-19
- **Related:** ADR-000, ADR-002, ADR-006, ADR-017, ADR-025, ADR-031, ADR-034, ADR-045, ADR-048, ADR-050, ADR-059, ADR-067, ADR-071, ADR-072, ADR-076

## Context

**What is already decided:**
- **ADR-025 and ADR-071 §9:** Datahub's login goes through the SSO provider over OIDC, with an
  OIDC stand-in on the gate. ADR-071 §9 leaves the login, and the frontend's route, to a later
  ADR. ADR-071 §5 adds no Ingress or route for the frontend: "it enters with the SSO login".
- **ADR-031:** the SSO provider is another team's service, its issuer given in `.env`
  (`DATAHUB_OIDC_ISSUER_URL`, already in `.env.template`). Credentials for other teams' services
  are typed into `.env` by hand, never generated. The stand-ins live in `NS_STANDINS`, are
  installed by the gate's scripts, never released, and their credentials are generated in the
  job (ADR-017). The OIDC provider enters with Datahub (P3).
- **ADR-034:** a stand-in that a service of this repository calls over HTTP can be recorded by
  adding a recorder port. The recorder writes HTTP JSON bodies up to 64 KiB in full.
- **ADR-071 §8:** `check-datahub` signs the root user `datahub` in through the frontend's native
  login (`/logIn`), from a pod in the ingress controller's namespace.

**What Datahub v1.7.0.1's frontend needs** (`datahub-frontend/conf/application.conf` at tag
`v1.7.0.1`, and the frontend subchart of `charts/datahub`):
- The authorization-code flow through pac4j: a client ID and secret, a discovery URI, and a base
  URL. The callback it registers with the provider is `{base URL}/callback/oidc`.
- PKCE is on by default (pac4j 6). Users are created on their first login
  (`AUTH_OIDC_JIT_PROVISIONING_ENABLED`, default true).
- The chart's `oidcAuthentication` with provider `other` sets the discovery URI, the scope
  (`openid profile email`), the username claim (default `email`) and its regex (default
  `([^@]+)`). The client secret can come from a Secret (`clientSecretRef`).
- Native and JAAS login stay on beside OIDC unless `AUTH_NATIVE_ENABLED` and `AUTH_JAAS_ENABLED`
  are set false.
- The browser reaches the provider's login page; the frontend reaches its discovery, token and
  key endpoints itself. On the gate, both must reach the same issuer URL.

**Stand-ins compared with Kade (2026-10-19):**

| | Keycloak 26.7.5 | Dex v2.45.1 | mock-oauth2-server | Our own provider |
|---|---|---|---|---|
| Fidelity | The target's SSO product | Standard OIDC | Any username, no password | What we wrote |
| On the gate | JVM, about 0.7 to 1 GiB, 30 to 60 s to start | Go binary, about 30 MiB, about 1 s | JVM, about 300 MiB | Small |
| Upkeep | Realm import JSON; a login form the check must parse; a release every 2 to 4 weeks | One YAML file; a release every 3 to 6 months | One JSON file; releases almost daily | Protocol code we own; the standard library cannot sign RS256 |

Kade chose Dex: the target's own provider is what users meet, so the gate proves Datahub's half
(discovery, the code exchange, the username claim, provisioning) with the lightest stand-in.

**Dex v2.45.1's password connectors** (`server/server.go`, `connector/mock/connectortest.go`):
- `staticPasswords` take a bcrypt hash, which neither the standard library nor the gate's
  pinned tools can make.
- The `mockPassword` connector takes a plain username and password from the config, and
  returns one fixed identity: email `kilgore@kilgore.trout`, username `Kilgore Trout`.

## Decision

### 1. What a release configures

- **The frontend's `oidcAuthentication`** in `charts/datahub/values-lakehouse.yaml`, provider
  `other`, set by the install from `.env`:

  | `.env` | Sets | Typed or generated |
  |---|---|---|
  | `DATAHUB_OIDC_ISSUER_URL` (exists) | `discoveryUri`: the issuer with `/.well-known/openid-configuration` | typed |
  | `DATAHUB_OIDC_CLIENT_ID` | `clientId` | typed |
  | `DATAHUB_OIDC_CLIENT_SECRET` | the Secret `datahub-oidc-client`, key `client_secret`, through `clientSecretRef` | typed, by hand (ADR-031) |
  | `DATAHUB_OIDC_USER_NAME_CLAIM` | `user_name_claim`; empty keeps the chart's `email` and `([^@]+)` | typed |
  | `DATAHUB_FRONTEND_URL` | `oidcBaseUrl`: the URL users reach the frontend at | typed |

- **`init_env.py`** leaves the five values empty in `.env.template` and never generates them.
  `--apply-secrets` creates `datahub-oidc-client` in `NS_CATALOG_DATAHUB` when
  `DATAHUB_OIDC_CLIENT_SECRET` is set. No credential reaches the chart another way (ADR-006).
- **Empty `DATAHUB_OIDC_ISSUER_URL` means no SSO login:** the install says so and goes on, as
  for an empty caller class (ADR-048 §2). When it is set, Datahub's install part stops if
  `DATAHUB_OIDC_CLIENT_ID` or `DATAHUB_FRONTEND_URL` is empty, or `datahub-oidc-client` is
  missing or lacks its key, reading key names only. These checks join Datahub's part of step 1
  before its `helm upgrade` (ADR-045, ADR-071 §7). The install order does not change.
- **The upstream chart stays as ADR-072 left it:** only values change.

### 2. The stand-in: Dex, gate and Linux test clusters only

- **`ghcr.io/dexidp/dex`,** pinned by digest in `gates/pins.json`, at the newest release at least
  two weeks old (v2.45.1 today; v2.46.0 from 2026-10-21). One pod in `NS_STANDINS`, on port
  5556, installed by `gate.py standins` with the others (ADR-031).
- **Its config is a file inside a Secret,** as SeaweedFS's is: in-memory storage, one
  `mockPassword` connector, the approval screen skipped, and one static client whose redirect URI
  is `DATAHUB_FRONTEND_URL/callback/oidc`. The connector's password and the client's secret are
  generated in the job (ADR-017). The client's ID and secret, and the URLs, go in the gate's
  `.env` like a target's.
- **The issuer is the recorder's OIDC port:** `http://<recorder service>.<NS_STANDINS>.svc.cluster.local:<port>/dex`.
  Dex's `issuer` names that URL, so the browser, the frontend and the tokens all use it, and every
  exchange passes the recorder (ADR-034). Plain HTTP, as on the gate's other stand-ins.

### 3. The recorder gains an OIDC port, and redacts tokens

- **One more `--http` listener** forwarding to Dex.
- **Its records keep no credential** (ADR-002): on this port, the token endpoint's JSON response is
  recorded as its length and sha256, and the `code` query parameter in a `Location` header is
  `<redacted>`. The login form's body is not JSON, so it is already only its length and sha256.
  The redaction gets its unit test before the code.

### 4. The network rows this needs

| Target | Port | Allowed from |
|---|---|---|
| The recorder | its OIDC port | Datahub's namespace (the frontend's discovery, token and key requests); the gate's check pods |
| Dex | 5556 | the recorders; the gate's check pods |

These are new rows in ADR-048 §3's `NS_STANDINS` table. ADR-048 is already amended by ADR-067,
so under ADR-000 rule 6 they enter through an ADR that supersedes ADR-048 whole and restates
ADR-067's rows. This ADR is not Accepted before that one. `check-network` gains the cases: from an
unlabelled pod in `NS_CATALOG_DATAHUB`, the OIDC port connects and Dex is refused.

### 5. What the smoke gate proves: `check-datahub-sso`

A new `gate.py check-datahub-sso`, its own step in `smoke.yml` after `check-datahub`. It plays
the browser from a pod in the ingress controller's namespace, as ADR-071 §8's login check does,
following each redirect itself and keeping cookies:
- **The flow:** the frontend's `/authenticate` sends it to the issuer with a PKCE challenge and
  the registered callback. The connector's login form takes the gate's username and password.
  Dex redirects to `/callback/oidc` with a code, and the frontend sets its session.
- **The user:** with that session, GraphQL's `me` answers `kilgore`, the claim `email` cut by the
  chart's regex, a user Datahub created on this login.
- **A wrong password:** Dex answers with its form again, and no code reaches the frontend.
- **The exchange:** `check-traffic` reads `gates/expectations/datahub-sso.json`: the discovery,
  token and key requests come from Datahub's namespace, the token request answered 200, and
  no record holds a token.
- **Clean-up:** it hard-deletes the user, as the system client.

The `datahub-recovery` job (ADR-077) does not run it.

### 6. Open for Kade

1. **Root's native login beside SSO.** Kept: nothing changes, and ADR-071 §8's check stays.
   Turned off (`AUTH_NATIVE_ENABLED` and `AUTH_JAAS_ENABLED` false): only SSO users sign in, and
   a lost provider locks everyone out. It also changes ADR-071 §6 and §8, and ADR-071 is already
   amended by ADR-072, so it would be superseded whole.
2. **The frontend's route.**
   (a) The release creates an Ingress or HTTPRoute for the frontend, its host from
   `DATAHUB_FRONTEND_URL` and its class or parent from `.env`.
   (b) The release creates none: `DATAHUB_FRONTEND_URL` names a route the ingress team keeps,
   reaching 9002 through ADR-071 §5's rule.
   Either way, §1's `DATAHUB_FRONTEND_URL` is the callback's base. On the gate the check pod uses
   the frontend's service URL.

## Diagram

```mermaid
sequenceDiagram
    participant B as check pod (browser),<br/>ingress namespace
    participant F as Datahub frontend
    participant R as recorder, OIDC port<br/>(NS_STANDINS)
    participant D as Dex (NS_STANDINS)
    F->>R: discovery, keys
    R->>D: forwarded, recorded
    B->>F: GET /authenticate
    F-->>B: 302 to the issuer, PKCE challenge
    B->>R: GET authorize, then the login form
    R->>D: forwarded, recorded
    B->>R: POST username, password
    R-->>B: 302 to /callback/oidc with a code (redacted in the record)
    B->>F: GET /callback/oidc
    F->>R: POST token (client secret, PKCE verifier)
    R->>D: forwarded; tokens recorded as length and sha256
    F-->>B: session cookies
    B->>F: GraphQL me: kilgore
```

## Alternatives considered

| Option | Why not |
|---|---|
| **Keycloak as the stand-in** | Same product as the target, but about 1 GiB and up to a minute on a gate whose `smoke` job sets the run's wall time (#231), and a login form that changes between frequent releases. Users meet the target's own Keycloak, not this one. |
| **mock-oauth2-server** | A JVM for a login that takes any username without a password, so a wrong password cannot be refused. Releases almost daily. |
| **Our own provider in the standard library** | Protocol and signing code to maintain; no RS256 without a new dependency, and HS256 is not what a target's provider sends. |
| **Dex's `staticPasswords`** | Needs a bcrypt hash made in the job, so a new tool or a pinned dependency for a gate-only user. |
| **The frontend calls Dex directly, not through the recorder** | Simpler, but the gate would not show that the token exchange is server-side, or that no token reaches a public log. |
| **No stand-in: SSO proven only on a target cluster** | ADR-031 requires a stand-in, and target clusters are never reached from here (ADR-004). |

## Consequences

- **`.env.template` gains four values** (`DATAHUB_OIDC_CLIENT_ID`, `DATAHUB_OIDC_CLIENT_SECRET`,
  `DATAHUB_OIDC_USER_NAME_CLAIM`, `DATAHUB_FRONTEND_URL`), all typed per environment.
- **The gate's username is fixed by the connector,** so the check proves the claim and regex
  path with `email`. A target that uses another claim sets `DATAHUB_OIDC_USER_NAME_CLAIM`; the
  gate does not prove that value.
- **Groups are not proven:** the connector sends none. Group extraction stays upstream's default.
- **Dex's `mockPassword` connector is a test connector.** If a later Dex drops it, the gate moves to
  `staticPasswords` with a bcrypt tool, under a new ADR.
- **Code follows in PRs after Acceptance, and after the ADR superseding ADR-048:**
  - the recorder's redaction, with its test first;
  - Dex in `gates/standins.py` and `gates/pins.json`, and the network rows;
  - the install's checks and values, `init_env.py`, `.env.template`;
  - `check-datahub-sso`, its expectations and its `smoke.yml` step.
  Those PRs touch `charts/`, `install/`, `gates/` and `scripts/init_env.py`. They need dry-run,
  smoke and smoke-fresh on their heads (ADR-039, ADR-050), and the ones touching the workflow or
  gate scripts are drafts Kade merges (ADR-059).
