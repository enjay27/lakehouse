# lakehouse

The catalog services of an Iceberg lakehouse on Kubernetes: Apache Polaris and Datahub, each
with its own stores, and the suite that measures them. Object storage, observability and SSO
are provided by other teams' services, configured in `.env` (ADR-031). The plan to the first
release is [`docs/plans/roadmap.md`](docs/plans/roadmap.md).

**This repository starts from its decision records.** Nothing else here is trusted until an
ADR covering it is Accepted: see [`docs/adr/`](docs/adr/README.md). Code arrives only under an
Accepted ADR (ADR-001).

Configuration that differs per environment lives in `.env`, which is never committed (ADR-002).
After cloning, run `python3 scripts/init_env.py` from the repository root: it writes `.env`
from [`.env.template`](.env.template) with new random credentials for this clone, and never
overwrites a value already set. It lists the credentials of other teams' services, which you
type in by hand. Set `KUBE_CONTEXT` in `.env`, then
`python3 scripts/init_env.py --apply-secrets` creates the Kubernetes Secrets (ADR-006).
