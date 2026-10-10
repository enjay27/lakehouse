# lakehouse

An Iceberg lakehouse on Kubernetes (object storage, Apache Polaris, PostgreSQL, logging and
monitoring) and the suite that measures it.

**This repository starts from its decision records.** Nothing else here is trusted until an
ADR covering it is Accepted: see [`docs/adr/`](docs/adr/README.md). Code arrives only under an
Accepted ADR (ADR-001).

Configuration that differs per environment lives in `.env`, which is never committed; copy
[`.env.template`](.env.template) to start (ADR-002).
