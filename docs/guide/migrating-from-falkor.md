---
title: Migrating from FalkorDB
description: CLI playbook for moving an existing install to SurrealDB
---

# Migrating from FalkorDB

Sibyl ships CLI tooling to move an organization (or a whole install) from the legacy FalkorDB +
PostgreSQL stack to SurrealDB. The migration is an explicit, reversible operation — nothing happens
automatically when you upgrade.

## Before you start

- **Both stacks online.** Keep FalkorDB + PostgreSQL running during migration. You'll only stop them
  after verification passes.
- **SurrealDB reachable.** Have a Surreal instance running. For local dev, `moon run dev` starts
  local SurrealDB automatically. For production, set `SIBYL_SURREAL_URL=ws://...`.
- **Take a fresh backup** of the source org first. `sibyld db backup --org-id <uuid>` dumps the
  current graph; archive archives are read-only so keeping a pre-migration snapshot is cheap
  insurance.
- **Rehearse.** Run the migration against a staging SurrealDB first to confirm the archive loads and
  counts match.

## The three-step flow

### 1. Export from legacy

```bash
SIBYL_STORE=legacy sibyld migrate export \
  --org-id <org-uuid> \
  --output /tmp/sibyl-migration.tar.gz
```

This writes a versioned archive containing:

- `graph.json` — Entities, relationships, and episodes from FalkorDB
- `postgres.sql` — Optional relational dump (users, API keys, crawled docs)
- `auth.json` / `content.json` — Structured auth and content payloads
- `manifest.json` — Counts and checksums for verification

### 2. Import to SurrealDB

```bash
SIBYL_STORE=surreal \
SIBYL_AUTH_STORE=surreal \
SIBYL_SURREAL_URL=ws://localhost:8000/rpc \
sibyld migrate import /tmp/sibyl-migration.tar.gz \
  --restore-database-dump   # Replay relational dump too
```

Restore order is: PostgreSQL dump → graph → auth → content. `--restore-database-dump` is only
needed if the target is in mixed mode or you want to keep a relational copy during transition.

### 3. Verify

```bash
SIBYL_STORE=surreal sibyld migrate verify /tmp/sibyl-migration.tar.gz
```

Verification samples entities and episodes, compares counts against the archive manifest, and
confirms relationship integrity. Non-zero exit on mismatch.

## Rehearsal mode

For production migrations, rehearse against a disposable SurrealDB first:

```bash
moon run migrate-rehearse -- /tmp/sibyl-migration.tar.gz --yes
```

This runs the full export → import → verify cycle without touching your production target. It's the
safest pre-cutover check — the rehearsal must pass green before the real run.

## Local shortcut

For single-org local dev moves:

```bash
moon run migrate-local-surreal
```

This wraps export → import → verify in one command against the local Surreal instance. If there is
exactly one legacy organization, Sibyl selects it automatically. If there are multiple
organizations, the command lists their IDs and asks you to rerun with `--org-id <org-uuid>`.

## Cutover

1. Stop writes to the legacy API (drain requests, flip the ingress).
2. Run export → import → verify against the current data.
3. Point clients at the new Surreal-backed API (`SIBYL_STORE=surreal`, new `SIBYL_SURREAL_URL`).
4. Keep legacy containers up for a few days as a rollback option. They hold read-only history until
   you're confident.
5. Decommission FalkorDB + PostgreSQL once traffic has run cleanly on Surreal.

## Rollback

If post-cutover verification reveals a problem:

1. Point clients back at the legacy API (`SIBYL_STORE=legacy`).
2. The legacy stack is unchanged — the export is non-destructive.
3. File an issue with the archive manifest and the failing verification output.

## FAQ

**Do I need to migrate?** Not immediately, but yes, you should plan it. Legacy mode remains available
for existing installs during the transition; SurrealDB is the default runtime and the path forward.

**Can I migrate org-by-org?** Yes. Each export is scoped to a single `--org-id`. Run them in
whatever order suits your tenant sizing.

**What about embedding models?** Embeddings are migrated verbatim. If you switch embedding models
(`SIBYL_EMBEDDING_MODEL`), plan a re-indexing pass separately — the migration tool doesn't re-embed
content.

**Can I run both stacks in parallel?** Yes. Use different `COMPOSE_PROJECT_NAME` and port offsets
(see [environment.md](../deployment/environment.md#running-multiple-instances)).
