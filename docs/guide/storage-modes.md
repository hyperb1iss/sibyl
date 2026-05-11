---
title: Storage Modes
description: The supported storage configurations and when to pick each
---

# Storage Modes

Sibyl supports two storage configurations, controlled by `SIBYL_STORE`. Auth is always stored in
SurrealDB.

| Mode                          | `SIBYL_STORE` | Auth store | Coordination | External services             |
| ----------------------------- | ------------- | ---------- | ------------ | ----------------------------- |
| **Fully Surreal** _(default)_ | `surreal`     | SurrealDB  | `local`      | SurrealDB                     |
| **Legacy graph/content**      | `legacy`      | SurrealDB  | `redis`      | FalkorDB + PostgreSQL + Redis |

Legacy mode is a compatibility path for existing installs that still need FalkorDB graph storage or
PostgreSQL content sidecars. Fully Surreal is the only recommended target for new deployments.
`SIBYL_AUTH_STORE=postgres` was removed after the v0.6.0 compatibility release.

Existing installs should read the
[SurrealDB migration release notes](./surrealdb-migration-release-notes.md) before upgrading.

Set `SIBYL_COORDINATION_BACKEND=auto` (the default) and sibyld picks the right coordination backend
for each mode. Override it only when you need Redis-backed coordination for multi-process Surreal
dev.

## Fully Surreal (default)

**Pick this for:** new installs, self-hosted local dev, simpler ops.

Graph, content, and auth all live in one SurrealDB instance, with per-org isolation via namespaces
(`org_<uuid_hex>`). No PostgreSQL, no Redis, no FalkorDB.

```bash
SIBYL_STORE=surreal
# SIBYL_SURREAL_URL=ws://surrealdb:8000/rpc  (or)
# SIBYL_SURREAL_DATA_DIR=./.moon/cache/surreal-dev
```

- **Dev:** `moon run dev` starts local SurrealDB backed by RocksDB automatically.
- **Prod:** run SurrealDB as a service (`ws://` or `http://` URL). In-memory mode (`memory://`) is
  rejected by the production config validator.
- **Server version:** use SurrealDB 3.x, and pin the exact server image/tag in production.

## Legacy graph/content

**Pick this for:** existing production deploys that aren't ready to migrate yet, or teams with
strict dependencies on FalkorDB/Postgres tooling during the compatibility window.

```bash
SIBYL_STORE=legacy
SIBYL_COORDINATION_BACKEND=redis
```

- FalkorDB backs the knowledge graph
- PostgreSQL backs crawled docs, embeddings, and relational sidecars
- SurrealDB backs auth/RBAC
- Redis/Valkey backs the job queue and coordination

All four services are required in legacy mode. See [environment.md](../deployment/environment.md)
for the full variable list.

## Switching modes

- **New install:** leave defaults alone. Fully Surreal is the default.
- **Legacy → Surreal:** see [migrating-from-falkor.md](./migrating-from-falkor.md). The migration is
  CLI-driven (`sibyld migrate export|import|verify`) and supports rehearsal runs.
- **Local legacy dev install:** `moon run dev` detects existing legacy data before starting a fresh
  Surreal runtime. For the common single-org case, run `moon run dev -- --migrate-legacy` and Sibyl
  selects the only org automatically.
- **PostgreSQL auth removal:** if an old `.env` still sets `SIBYL_AUTH_STORE=postgres`, remove it.
  The server rejects that value, and `moon run dev` normalizes local startup back to Surreal auth.
