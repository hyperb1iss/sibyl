---
title: Why SurrealDB
description: Why Sibyl uses SurrealDB as the default store
---

# Why SurrealDB

Sibyl used to run on three separate databases: FalkorDB for the knowledge graph, PostgreSQL for
relational auth and crawled docs, and Redis for the job queue. It worked, but three backends means
three upgrade paths, three backup strategies, three health checks, and three sets of connection
strings in every compose file and chart. For a tool that's supposed to give you memory, the
operational surface was heavier than the product itself.

**SurrealDB replaces the whole stack with one engine.**

## What you get

- **One database, one namespace, one backup.** Graph nodes, document chunks, user accounts, and API
  keys all live in the same SurrealDB instance. Per-org isolation is a namespace, not a separate
  cluster. Backups are a single RocksDB directory or one SurrealQL export.
- **Embedded mode for dev.** Point Sibyl at a local `surrealkv://` path and you're running with zero
  external services. No Docker required for a fresh checkout.
- **Native hybrid search.** HNSW vector indexes and full-text search live next to the graph data, so
  retrieval doesn't have to fan out across stores.
- **Fewer connection boundaries.** One driver, one auth model, one set of queries. The API and
  worker talk to the same WebSocket endpoint.
- **Graphiti compatibility.** The SurrealDriver plugs into Graphiti the same way the FalkorDriver
  does, so the entity/episode/community model stays identical. No data model rewrite.

## Honest tradeoffs

- **Less battle-tested than Postgres** for deep relational workloads. If you have a mature Postgres
  story (PITR, managed service, replicas), the legacy stack still makes sense for auth — run mixed
  mode with `SIBYL_STORE=surreal` + `SIBYL_AUTH_STORE=postgres`.
- **Embedded mode is single-writer.** Multi-process local dev on embedded Surreal serializes through
  one writer; for real concurrency, run SurrealDB as a service (`ws://...`).
- **Younger tooling.** Third-party tooling around SurrealDB (observability dashboards, migration
  frameworks) is thinner than Postgres'. We keep an Alembic path for relational auth so teams that
  depend on that ecosystem can stage the move.

## When to stay on legacy

Run `SIBYL_STORE=legacy` if you already have FalkorDB + PostgreSQL in production, haven't planned a
migration window, or have internal tooling that reads the Postgres schema directly. Both stacks are
supported. See [storage-modes.md](./storage-modes.md) for the mode matrix and
[migrating-from-falkor.md](./migrating-from-falkor.md) for the cutover playbook.

The direction is clear — new installs default to fully Surreal — but nobody has to migrate on a
schedule they didn't pick.
