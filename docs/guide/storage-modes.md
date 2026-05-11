---
title: Storage Modes
description: The three supported storage configurations and when to pick each
---

# Storage Modes

Sibyl supports three storage configurations, controlled by two environment variables:

| Mode                          | `SIBYL_STORE` | `SIBYL_AUTH_STORE` | Coordination | External services             |
| ----------------------------- | ------------- | ------------------ | ------------ | ----------------------------- |
| **Fully Surreal** _(default)_ | `surreal`     | `surreal`          | `local`      | SurrealDB                     |
| **Mixed (transitional)**      | `surreal`     | `postgres`         | `local`      | SurrealDB + PostgreSQL        |
| **Legacy**                    | `legacy`      | `postgres`         | `redis`      | FalkorDB + PostgreSQL + Redis |

Mixed and legacy modes are compatibility paths for existing installs. Fully Surreal is the only
recommended target for new deployments, and the PostgreSQL auth store is planned for removal after
one compatibility release once the migration gates are green.

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
SIBYL_AUTH_STORE=surreal
# SIBYL_SURREAL_URL=ws://surrealdb:8000/rpc  (or)
# SIBYL_SURREAL_DATA_DIR=./.moon/cache/surreal-dev
```

- **Dev:** `moon run dev` starts local SurrealDB backed by RocksDB automatically.
- **Prod:** run SurrealDB as a service (`ws://` or `http://` URL). In-memory mode (`memory://`) is
  rejected by the production config validator.
- **Server version:** use SurrealDB 3.x, and pin the exact server image/tag in production.

## Mixed (transitional)

**Pick this for:** existing deploys moving from legacy, where you have a mature PostgreSQL
operational story (backups, PITR, replicas) and want to keep auth there while graph/content move to
SurrealDB.

```bash
SIBYL_STORE=surreal
SIBYL_AUTH_STORE=postgres
```

This is a stepping stone, not a long-term target. Do not start a new install here. Once you're
comfortable, migrate auth to Surreal too (`SIBYL_AUTH_STORE=surreal`) and retire Postgres.

## Legacy

**Pick this for:** existing production deploys that aren't ready to migrate yet, or teams with
strict dependencies on FalkorDB/Postgres tooling during the compatibility window.

```bash
SIBYL_STORE=legacy
SIBYL_AUTH_STORE=postgres
SIBYL_COORDINATION_BACKEND=redis
```

- FalkorDB backs the knowledge graph
- PostgreSQL backs auth, crawled docs, embeddings
- Redis/Valkey backs the job queue and coordination

All three services are required. See [environment.md](../deployment/environment.md) for the full
variable list.

## Switching modes

- **New install:** leave defaults alone. Fully Surreal is the default.
- **Legacy → Surreal:** see [migrating-from-falkor.md](./migrating-from-falkor.md). The migration is
  CLI-driven (`sibyld migrate export|import|verify`) and supports rehearsal runs.
- **Local legacy dev install:** `moon run dev` detects existing legacy data before starting a fresh
  Surreal runtime. For the common single-org case, run `moon run dev -- --migrate-legacy` and Sibyl
  selects the only org automatically.
- **Mixed → Fully Surreal:** export auth with `sibyld migrate export --skip-graph --skip-content`,
  rehearse the archive, flip `SIBYL_AUTH_STORE=surreal`, import with
  `sibyld migrate import <archive> --skip-graph --skip-content`, then run the auth-flow gate. Freeze
  the legacy auth/RBAC tables with `moon run auth-readonly -- --mode freeze --apply --yes` for the
  rollback window before Postgres is decommissioned.
