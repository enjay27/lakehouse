# lakehouse

An Iceberg lakehouse on Kubernetes (object storage, Apache Polaris, PostgreSQL, logging and
monitoring) and the suite that measures it.

**This repository starts from its decision records.** Nothing else here is trusted until an
ADR covering it is Accepted: see [`docs/adr/`](docs/adr/README.md). Code arrives only under an
Accepted ADR (ADR-001).

Configuration that differs per environment lives in `.env`, which is never committed (ADR-002).
After cloning, run `python3 scripts/init_env.py` from the repository root: it writes `.env`
from [`.env.template`](.env.template) with new random credentials for this clone, and never
overwrites a value already set. Set `KUBE_CONTEXT` in `.env`, then
`python3 scripts/init_env.py --apply-secrets` creates the Kubernetes Secrets (ADR-006).
