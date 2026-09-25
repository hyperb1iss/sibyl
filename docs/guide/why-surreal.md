---
title: Why SurrealDB
description: Why Sibyl uses SurrealDB as the default store
---

# Why SurrealDB

A memory system needs a graph, a vector index, full-text search, relational auth records, and a
place for documents. Split across separate databases, that means separate upgrade paths, backup
strategies, health checks, and connection strings in every compose file and chart. For a tool that's
supposed to give you memory, that operational surface would be heavier than the product itself.

**SurrealDB covers all of it with one engine.**

## What you get

- **One engine, one backup strategy.** Graph memory, document chunks, auth records, API keys, and
  tasks can live in the same SurrealDB instance. Per-org graph isolation is a namespace, not a
  separate cluster. Backups are a single SurrealKV data directory or one SurrealQL export.
- **Embedded mode for dev.** Point Sibyl at a local `surrealkv://` path and you're running with zero
  external services. No Docker required for a fresh checkout.
- **Native hybrid search.** HNSW vector indexes and full-text search live next to the graph data, so
  retrieval doesn't have to fan out across stores.
- **Fewer connection boundaries.** One driver, one auth model, one set of queries. The API and
  worker talk to the same WebSocket endpoint.
- **One archive format.** `sibyld migrate export` and the API backups write Sibyl's own SurrealDB
  archive, and `sibyld migrate import` restores it. There is no second format to keep compatible.

## Honest tradeoffs

- **Less battle-tested than Postgres** for deep relational workloads. Mature Postgres operations
  (point-in-time recovery, managed services, read replicas) have no one-to-one SurrealDB equivalent
  yet, so plan backups around logical exports and storage snapshots.
- **Embedded mode is single-writer.** Multi-process local dev on embedded Surreal serializes through
  one writer; for real concurrency, run SurrealDB as a service (`ws://...`).
- **Younger tooling.** Third-party tooling around SurrealDB (observability dashboards, migration
  frameworks) is thinner than Postgres'.

See [Storage Modes](./storage-modes.md) for the connection options and
[Backup And Restore](../admin/backup-restore.md) for the recovery story.
