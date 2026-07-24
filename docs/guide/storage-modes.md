---
title: Storage Modes
description: The supported storage configurations and when to pick each
---

# Storage Modes

Sibyl's active runtime is SurrealDB. Current binaries only accept `SIBYL_STORE=surreal`; any other
value, including `legacy`, is rejected at startup. `legacy` is historical context for the v0.6
compatibility archives, not a runtime you can select today.

| Mode                          | `SIBYL_STORE` | Auth store | Coordination | External services               |
| ----------------------------- | ------------- | ---------- | ------------ | ------------------------------- |
| **Fully Surreal** _(default)_ | `surreal`     | SurrealDB  | `local`      | SurrealDB                       |
| **Archive rehearsal**         | `surreal`     | SurrealDB  | `local`      | SurrealDB + external PostgreSQL |

Active auth, content, crawler, raw-capture, graph, and RAG runtime paths resolve through SurrealDB.
PostgreSQL remains only for explicit historical archive import/restore rehearsal against an
operator-managed database. Fully Surreal is the only recommended target for new deployments.
`SIBYL_AUTH_STORE` now only accepts `surreal`; a leftover `SIBYL_AUTH_STORE=postgres` fails config
validation.

Existing installs should read the
[SurrealDB migration release notes](./surrealdb-migration-release-notes.md) before upgrading.

Set `SIBYL_COORDINATION_BACKEND=auto` (the default) and sibyld picks the right coordination backend
for each mode. Override it only when you need Redis-backed coordination for multi-process Surreal
dev.

## Archive And Rollback Policy

Archive rehearsal is evidence work, not an alternate runtime. Historical PostgreSQL dump payloads
may be restored only against an operator-managed rehearsal database, then verified through explicit
`sibyld migrate` commands.

Rollback has a narrow operational boundary:

- before SurrealDB accepts new production writes, point traffic back to the preserved source
  deployment and unfreeze source writes if needed;
- after SurrealDB accepts new writes, do not treat the historical PostgreSQL/FalkorDB stack as a
  lossless rollback target. Restore from Surreal backups or replay source archives deliberately.

## Fully Surreal (default)

**Pick this for:** new installs, self-hosted local dev, simpler ops.

Graph, content, and auth all live in one SurrealDB instance, with per-org isolation via namespaces
(`org_<uuid_hex>`). No PostgreSQL, no Redis, no FalkorDB.

```bash
SIBYL_STORE=surreal
# SIBYL_SURREAL_URL=ws://surrealdb:8000/rpc  (or)
# SIBYL_SURREAL_DATA_DIR=./.moon/cache/surreal-dev
```

- **Dev:** `moon run dev` starts local SurrealDB backed by SurrealKV automatically.
- **Prod:** run SurrealDB as a service (`ws://` or `http://` URL). In-memory mode (`memory://`) is
  rejected by the production config validator.
- **Server version:** use SurrealDB 3.x, and pin the exact server image/tag in production.

## Archive Rehearsal

**Pick this for:** validating retained migration archives or database dump restore behavior.

```bash
SIBYL_STORE=surreal
SIBYL_COORDINATION_BACKEND=local
```

- SurrealDB backs graph, auth/RBAC, content, and RAG runtime paths
- PostgreSQL dump payloads remain available only for migration and rollback evidence
- Redis/Valkey is optional for distributed coordination and must be enabled explicitly

## Switching modes

- **New install:** leave defaults alone. Fully Surreal is the default.
- **Legacy → Surreal:** see [migrating-from-falkor.md](./migrating-from-falkor.md). The migration is
  CLI-driven (`sibyld migrate export|import|verify`) and supports rehearsal runs.
- **Local legacy dev install:** `moon run dev` detects existing legacy data before starting a fresh
  Surreal runtime. The historical import command
  (`uv run --directory apps/api sibyld migrate import <archive> --source-type legacy-archive --target-mode surreal --yes --clean`)
  was removed in the v0.6–v1.0 line; current binaries only accept
  `--source-type surreal-archive --target-mode surreal`. See
  [migrating-from-falkor.md](./migrating-from-falkor.md) for the historical path.
- **PostgreSQL auth removal:** if an old `.env` still sets `SIBYL_AUTH_STORE=postgres`, remove it.
  The server rejects that value, and `moon run dev` normalizes local startup back to Surreal auth.
