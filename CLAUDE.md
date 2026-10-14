# lakehouse

Apache Polaris and Datahub on Kubernetes, each with its own stores (PostgreSQL on
CloudNativePG; Datahub's Kafka and OpenSearch), and the suite that measures them. Object
storage, observability, SSO and the catalog gateway are other teams' services, reached through
`.env`, with stand-ins on the gate (ADR-031). Public repository (ADR-002).

## Decisions first (ADR-000, ADR-001)

- `docs/adr/` is the source of truth; `docs/adr/README.md` is the index. **Code enters only
  under an Accepted ADR**, in a PR that cites it.
- A new ADR is Proposed; Kade accepts it in a commit of its own after reading the final text.
  Accepted ADRs are immutable: change one with a new ADR that supersedes or amends it.
- The index changes in the same commit as any ADR; an ADR changes in the same commit as the
  code it affects. Living documents (this file, `README.md`, `.claude/`, `docs/plans/`, the
  index) cite only ADRs in force.
- The archive `enjay27/lakehouse-k8s` is evidence only: cite by path and commit, copy code
  only under an Accepted ADR.

## Layout

| Path | What |
|---|---|
| `docs/adr/` | Decisions and their index |
| `docs/plans/roadmap.md` | The plan to the first release: scope, phases P0 to P7, open items |
| `docs/measurements/`, `docs/runbooks/` | Measurements, with evidence and the versions measured, once the first is taken (ADR-003); runbooks, one per procedure, merged once proven on a Linux test cluster (ADR-043) |
| `scripts/adr_check.py` | ADR-000 checks: names, numbers, headers, index rows, citations |
| `scripts/repo_check.py` | No env file, key file, secret pattern or CRLF in what git tracks (ADR-002, ADR-006) |
| `scripts/claude_md_check.py` | This file at most 100 lines; `.claude/rules/*.md` at most 80 with `paths:` |
| `scripts/auto_merge_gate.py` | Merge, wait or stop from a head's checks, drafts and paths (ADR-022, ADR-030) |
| `scripts/gate_check.py` | gate-check: platform files need a dry-run and a smoke covering the head (ADR-039), batch and fixture spec files a log-batch (ADR-040) |
| `scripts/branch_cleanup.py` | Which merged `claude/*` and stale `gate/*` branches to delete (ADR-032) |
| `gates/` | The kind gate: pinned tools and node image, the cluster, CNPG, cert-manager and the Barman Cloud Plugin, the stand-ins (SeaweedFS, OPA with `gates/opa/policy.rego`), the recorder that `check-traffic` reads, the PyIceberg smoke tests (`gates/iceberg/`), the console's sign-in check (`gates/console/`), `check-restore` (a backup of polaris-pg restored, ADR-043), `check-logs` with a stand-in Fluent Bit reading the log batch's output; `network.py`, the gate's own allow policies and `check-network`, one allowed and one refused connection per rule (ADR-048); `metrics.py`, a Prometheus stand-in loading `docs/contracts/`'s scrape file and `check-metrics` (ADR-046); `fixtures.py`, Polaris's files across a real UTC hour, and `log_batch_gate.py`, the batch over them through Fluent Bit into an OpenSearch stand-in (ADR-040); on the gate only, the gate's Secrets (ADR-017, ADR-026, ADR-031, ADR-034, ADR-036, ADR-045) |
| `charts/`, `images/` | Local charts: `polaris-postgres` with its backups (ADR-010, ADR-043); `polaris-network-policies`, ingress to Polaris's namespace (ADR-048, ADR-046); `polaris`, upstream's 1.6.0, and `polaris-console`, polaris-tools at a pinned commit, each unchanged with our `values-lakehouse.yaml` (ADR-009, ADR-028); `images/polaris-console` builds the console on the gate from that commit with our lockfile; `polaris-log-batch` and `images/polaris-log-batch`, the log batch's CronJob and image, its contract in `docs/contracts/` (ADR-036); tests render each chart, check its values schema, run kubeconform on CNPG's CRD and the pinned NetworkPolicy schema (ADR-016) |
| `install/` | `release.py` installs a release in ADR-045's order (step 0 the CNPG and plugin preconditions; step 1 Polaris's network policies, its PostgreSQL, the S3 and policy endpoints answering, the realm bootstrap, Polaris; step 2 the log batch, whose failure stops the install (ADR-037), then the console when enabled, never blocking); `release_tag.py` names tags, reads the release gate, writes notes (ADR-033) |
| `scripts/init_env.py` | init-env: `.env` with credentials per clone; `--apply-secrets` (ADR-006) |
| `.github/workflows/` | `static.yml`, `gate-check.yml` (ADR-039), `dry-run.yml`, `smoke.yml` (ADR-050), `log-batch.yml`, `create-fixtures.yml` (ADR-040), `auto-merge.yml` (ADR-022), `branch-cleanup.yml` (ADR-032), `delete-gate-branch.yml` (ADR-049), `release-gate.yml`, `release.yml` (ADR-033, ADR-042), `labels.yml` |
| `.github/labels.json` | The state labels, synced by `labels.yml` (ADR-003) |
| `.env.template` | Every per-environment variable, credentials empty (ADR-002) |

## Gates

| Part | Gate |
|---|---|
| `scripts/`, `gates/`, `install/`, `charts/`, `images/polaris-log-batch/` | `python3 -m unittest discover -s <dir>` for each; `charts/` needs `gates/gate.py tools BIN_DIR helm kubeconform` and `charts/requirements.txt` |
| `docs/adr/`, living documents | `python3 scripts/adr_check.py` |
| Everything tracked | `python3 scripts/repo_check.py`; `git diff --check $(git hash-object -t tree /dev/null) HEAD` |
| `CLAUDE.md`, `.claude/` | `python3 scripts/claude_md_check.py` |
| `init_env.py --apply-secrets` | Needs a Linux test cluster: label the issue `cmd:verify-needs-linux` |

CI runs these as the **static** gate on every push to `claude/**` and every PR (ADR-050). **gate-check**
(ADR-039, every PR into `main`): `charts/`, `images/`, `install/`, `gates/` or the gate workflows need
passing dry-run and smoke runs on the head; batch or fixture spec files, a log-batch (ADR-040 §5).
`install/`, `scripts/init_env.py` or a file added to or removed from `charts/` also need smoke-fresh,
unread by gate-check (ADR-050 §3). How to push each head to `gate/<level>/<task>`: `.claude/rules/gates.md`.

## Environments and clusters (ADR-004, ADR-005)

- Linux only. Releases are proven on the Linux gate; target clusters are never reached from
  here.
- Against a cluster: the context must equal `KUBE_CONTEXT` from `.env` (no default), every
  command names its namespace, and destructive commands need Kade's approval when they run.
- Suite environments `local`, `dev`, `prod`: mutating code calls `require_not_prod(...)`;
  teardown is `local`-only.

## Public repository and credentials (ADR-002, ADR-006)

- Never commit: hostnames, URLs or IPs of non-local systems, employer names, people's names
  or roles, credentials (not even examples), `.env`, keys. Issues, PRs and Actions logs are
  public too; write findings about real environments in neutral terms.
- Per-environment values live in `.env` (gitignored), from `.env.template`. Credentials are
  generated per clone by `python3 scripts/init_env.py`; `--apply-secrets` creates the
  Kubernetes Secrets, the only way credentials reach charts.
- A credential that reaches a commit is rotated; pushed history is never rewritten.

## State (ADR-003)

- Work, open questions and unverified items are issues in this repository, read over the
  REST API. No `MEMORY.md`, no session or handoff files.
- Labels: `cmd:status-now` (in progress), `cmd:verify-not-verified` (a check could not run),
  `cmd:verify-needs-linux` (needs a Linux test cluster). A decision that is Kade's: the issue
  is assigned to him, no label.
- The PR is the session record: *What changed and why*, *Verified*, *NOT VERIFIED*, *Wrong
  turns*, and `Closes #N`.

## Version control (ADR-022, ADR-030, ADR-033)

- Sessions work on `claude/<task>`, one task per PR, from an up-to-date `main`.
- `auto-merge.yml` (not GitHub's setting) merges a green `claude/*` PR, deletes the branch and
  closes its `Closes #N` issues; a new check workflow joins its `workflows:` list in that PR.
  It never merges a draft, nor a PR changing `.github/workflows/`, `scripts/auto_merge_gate.py`
  or `scripts/gate_check.py` (ADR-030): open those as drafts Kade merges by hand. It is read
  from `main`, so a change to it acts after its own merge. Kade's branches merge by hand.
- A gate run deletes its `gate/*` branch when it ends (ADR-049); `branch-cleanup.yml` deletes merged `claude/*` branches and any gate branch left over (ADR-032).
- Releases (ADR-033): a PR from `main` into `rc`, gated by `release-gate.yml`, merged by Kade;
  `release.yml` on `rc` tags it. Sessions open such a PR or run a release only when asked.

## Overrides of global rules

None. (Kade's global rule already defers to a repository's own auto-merge workflow.)
