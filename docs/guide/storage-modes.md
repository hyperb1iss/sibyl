---
title: Storage Modes
description: How Sibyl connects to SurrealDB and when to pick each connection mode
---

# Storage Modes

SurrealDB is Sibyl's only store. Graph memory, content, auth, tasks, raw captures, and derived
indexes all live in one SurrealDB data plane, with per-org isolation through namespaces
(`org_<uuid_hex>`). `SIBYL_STORE` and `SIBYL_AUTH_STORE` accept only `surreal`, and any other value
fails config validation at startup.

What varies is how a process reaches SurrealDB:

| Mode                   | Setting                                 | Resolved URL                 | Use it for                |
| ---------------------- | --------------------------------------- | ---------------------------- | ------------------------- |
| **Server** _(prod)_    | `SIBYL_SURREAL_URL=ws://host:8000/rpc`  | `ws://...` or `http://...`   | Production, multi-process |
| **Embedded SurrealKV** | `SIBYL_SURREAL_DATA_DIR=./path/to/data` | `surrealkv://./path/to/data` | Single-process local dev  |
| **In-memory**          | neither set                             | `memory://`                  | Tests only                |

Set exactly one of `SIBYL_SURREAL_URL` and `SIBYL_SURREAL_DATA_DIR`; setting both fails config
validation. With neither set, Sibyl falls back to `memory://`, which the production config validator
rejects. The embedded modes also accept `surrealkv+versioned://`, `file://`, and `mem://`; any other
scheme, such as `rocksdb://`, fails at startup. See
[SurrealDB URL forms](../deployment/environment.md#surrealdb-url-forms) for the full list.

## Server

**Pick this for:** production, shared dev stacks, and anything that runs the API and a worker as
separate processes.

```bash
SIBYL_SURREAL_URL=ws://surrealdb:8000/rpc
SIBYL_SURREAL_USERNAME=root
SIBYL_SURREAL_PASSWORD=<secure-password>
```

- Run SurrealDB 3.x as a service and pin the exact server image or tag in production.
- `moon run dev` uses this mode: it starts a local RocksDB-backed SurrealDB server with data under
  `.moon/cache/surreal-dev` and points Sibyl at `ws://127.0.0.1:8000/rpc`.
- Each org gets its own connection-pooled client scoped to its namespace, so queries within an org
  run concurrently.

## Embedded SurrealKV

**Pick this for:** a fresh checkout with zero external services.

```bash
SIBYL_SURREAL_DATA_DIR=./data/surreal
```

- Use a directory of its own. The `moon run dev` server keeps RocksDB data in
  `.moon/cache/surreal-dev`, so do not point SurrealKV there.
- Embedded mode is single-writer. Sibyl clamps embedded clients to one connection, so keep it to a
  single process. For real concurrency, run SurrealDB as a server.
- In production, embedded mode also needs `SIBYL_ALLOW_EMBEDDED_SINGLE_WRITER=1`. Set it only when
  one daemon owns the database.

## In-Memory

`memory://` (or `mem://`) exists for test suites. It holds nothing across restarts and is forbidden
when `SIBYL_ENVIRONMENT=production`.

## Coordination

Coordination (jobs, locks, pub/sub, pending state) is separate from storage. Leave
`SIBYL_COORDINATION_BACKEND=auto`, the default, and sibyld resolves it to in-process `local`
coordination unless Redis settings are present. Set `redis` explicitly for multi-process or
multi-replica deployments.

## Backups and Restore

Back up SurrealDB with logical exports or storage snapshots, and restore Sibyl archives with
`sibyld migrate import <archive> --source-type surreal-archive --target-mode surreal`. Restore
accepts only Sibyl's own archives (from `sibyld migrate export`, `merge`, or `consolidate`) and API
backups. See [Backup and Restore](../admin/backup-restore.md) for the full procedure.
