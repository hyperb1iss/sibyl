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

- **One engine, one backup strategy.** Graph memory, document chunks, auth records, API keys, and
  tasks can live in the same SurrealDB instance. Per-org graph isolation is a namespace, not a
  separate cluster. Backups are a single RocksDB directory or one SurrealQL export.
- **Embedded mode for dev.** Point Sibyl at a local `surrealkv://` path and you're running with zero
  external services. No Docker required for a fresh checkout.
- **Native hybrid search.** HNSW vector indexes and full-text search live next to the graph data, so
  retrieval doesn't have to fan out across stores.
- **Fewer connection boundaries.** One driver, one auth model, one set of queries. The API and
  worker talk to the same WebSocket endpoint.
- **Graphiti transition compatibility.** The SurrealDriver plugs into Graphiti during migration, so
  existing entity, episode, and community behavior can keep working while native SurrealDB paths are
  built and verified.

## Honest tradeoffs

- **Less battle-tested than Postgres** for deep relational workloads. If you have a mature Postgres
  story (PITR, managed service, replicas), PostgreSQL archives can still support migration rehearsal
  and rollback evidence, but they are not a long-term runtime destination.
- **Embedded mode is single-writer.** Multi-process local dev on embedded Surreal serializes through
  one writer; for real concurrency, run SurrealDB as a service (`ws://...`).
- **Younger tooling.** Third-party tooling around SurrealDB (observability dashboards, migration
  frameworks) is thinner than Postgres'. The remaining relational path exists to stage legacy
  content migration, not to keep two product stacks forever.

## When to stay on legacy

Run `SIBYL_STORE=legacy` if you already have FalkorDB in production and haven't planned a migration
window. PostgreSQL is now an archive/export compatibility surface, not the active content runtime.
See [storage-modes.md](./storage-modes.md) for the mode matrix and
[migrating-from-falkor.md](./migrating-from-falkor.md) for the cutover playbook.

For the larger product and architecture direction, see
[Sibyl Northstar](../architecture/SIBYL_NORTHSTAR.md).

The direction is clear: new installs default to fully Surreal, existing installs migrate
deliberately, then FalkorDB and PostgreSQL leave the product surface.
