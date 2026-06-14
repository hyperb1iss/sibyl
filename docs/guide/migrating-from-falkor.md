---
title: Migrating from FalkorDB
description: CLI playbook for moving an existing install to SurrealDB
---

# Migrating from FalkorDB

Sibyl ships CLI tooling to move an organization (or a whole install) from the legacy FalkorDB +
PostgreSQL stack to SurrealDB. The migration is explicit and rehearsal-driven. Nothing happens
automatically when you upgrade.

SurrealDB is the default runtime now. Legacy graph/content data is a source-side migration concern
for existing installs, not a runtime mode for new deployments. Auth/RBAC always runs on SurrealDB in
current releases.

Read the [SurrealDB migration release notes](./surrealdb-migration-release-notes.md) first if you
are upgrading an existing install.

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
- **Choose the rollback window.** Source rollback is only safe while SurrealDB writes are still
  frozen. Once SurrealDB accepts new writes, recovery uses Surreal backups or deliberate replay.

## The three-step flow

### 1. Export from legacy

```bash
SIBYL_STORE=legacy sibyld migrate export \
  --org-id <org-uuid> \
  --output /tmp/sibyl-migration.tar.gz
```

Run this from the v0.6 compatibility release or a preserved legacy source environment before
upgrading the source install. The export writes a versioned archive containing:

- `graph.json`: entities, relationships, and episodes from FalkorDB
- `auth.json` / `content.json`: structured auth and content payloads
- `manifest.json`: counts and checksums for verification

Older compatibility-release archives may also contain a retained `postgres.sql` payload for
rehearsal or rollback validation. Current active backup and export commands do not create new
database dump sidecars.

### 2. Import to SurrealDB

```bash
SIBYL_STORE=surreal \
SIBYL_SURREAL_URL=ws://localhost:8000/rpc \
sibyld migrate import /tmp/sibyl-migration.tar.gz \
  --source-type legacy-archive \
  --target-mode surreal
```

The structured `auth.json` and `content.json` payloads restore directly into SurrealDB. New
structured archive exports read from Surreal; `--restore-database-dump` is only needed when
explicitly replaying a retained `postgres.sql` archive during rehearsal or rollback validation with
`--source-type legacy-archive --target-mode postgres-rehearsal`.

Legacy `episode` nodes and `mentions` edges are archive compatibility records. They are restored
only so older Graphiti/Falkor exports can round-trip and verify. Current runtime memory uses
`entity(entity_type='episode')` plus normal `relates_to` edges, and live graph stats do not count
the archive-only tables.

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

This imports and verifies an existing archive against the active Surreal target without touching
your production target. It's the safest pre-cutover check, and the rehearsal must pass green before
the real run.

The rehearsal runs the deterministic auth-flow replay by default. It signs up users, rotates tokens,
exercises API keys, invitations, org switching, device auth, logout revocation, session listing, and
password reset consumption through the configured email outbox.

## Auth flow gates

Run the live auth replay directly when you want a focused check:

```bash
moon run auth-flow-replay -- --base-url http://localhost:3334
```

For final auth cutover confidence, compare a Postgres-backed API and a Surreal-backed API:

```bash
moon run auth-flow-compare -- \
  --postgres-base-url http://localhost:3334 \
  --surreal-base-url http://localhost:3335
```

The comparison ignores generated IDs, raw tokens, and timestamps. It compares the replayed step
sequence, normalized JWT claim shape, and semantic observations such as API-key rejection, device
authorization pending errors, session listing behavior, and logout revocation.

The compare command rejects identical base URLs by default so it cannot accidentally compare one API
to itself. `--allow-same-base-url` is only for debugging the harness.

## Local archive import

`moon run dev` detects local legacy data before it starts a fresh Surreal runtime. The live local
FalkorDB export wrapper was retired after the v0.6.0 compatibility release, so local recovery now
uses the same archive import path as production rehearsal:

```bash
uv run --directory apps/api sibyld migrate import <archive> \
  --source-type legacy-archive \
  --target-mode surreal \
  --yes \
  --clean
```

## Cutover

1. Export the current archive from legacy.
2. Start a Surreal-backed API on a private cutover endpoint, but keep public traffic on the
   preserved legacy source deployment.
3. Stop writes to the legacy API (drain requests, flip the ingress).
4. Run the Surreal cutover acceptance gate while writes are frozen:

```bash
moon run migrate-cutover -- \
  /tmp/sibyl-migration.tar.gz \
  --write-freeze-confirmed \
  --base-url http://localhost:3334 \
  --yes
```

5. Freeze the legacy auth/RBAC tables so stale code paths cannot accept new writes:

```bash
moon run auth-readonly -- --mode freeze --apply --yes
```

6. Reopen writes on Surreal only after final operator sign-off:

```bash
moon run migrate-cutover -- \
  /tmp/sibyl-migration.tar.gz \
  --write-freeze-confirmed \
  --reopen-writes \
  --acknowledge-no-instant-rollback \
  --base-url http://localhost:3334 \
  --yes
```

7. Point clients at the new Surreal-backed API (`SIBYL_STORE=surreal`, new `SIBYL_SURREAL_URL`).
8. Keep the preserved legacy source deployment up for a few days as a read-only evidence source. It
   is a rollback target only until SurrealDB accepts new production writes.
9. Decommission FalkorDB + PostgreSQL once traffic has run cleanly on Surreal and your rollback
   window has closed.

## Rollback

If post-cutover verification reveals a problem:

1. Point clients back at the preserved legacy API only if SurrealDB has not accepted new production
   writes yet.
2. Remove the auth/RBAC read-only guard before resuming source writes:

```bash
moon run auth-readonly -- --mode unfreeze --apply --yes
```

3. If SurrealDB has accepted new production writes, keep clients on SurrealDB and recover through a
   Surreal backup restore, a corrected archive import, or deliberate replay of the affected writes.
4. File an issue with the archive manifest and the failing verification output.

## FAQ

**Do I need to migrate?** Yes, if you still have FalkorDB data. Legacy data should be exported from
the source install, rehearsed, and imported into SurrealDB; current releases should run on the
SurrealDB runtime.

**Can I migrate org-by-org?** Yes. Each export is scoped to a single `--org-id`. Run them in
whatever order suits your tenant sizing.

**What about embedding models?** Embeddings are migrated verbatim. If you switch embedding models
(`SIBYL_EMBEDDING_MODEL`), plan a re-indexing pass separately. The migration tool doesn't re-embed
content.

**Can I run both stacks in parallel?** Yes. Use different `COMPOSE_PROJECT_NAME` and port offsets
(see [environment.md](../deployment/environment.md#running-multiple-instances)).
