---
title: SurrealDB Migration Release Notes
description: Release guidance for the SurrealDB-first storage cutover
---

# SurrealDB Migration Release Notes

> **Note (2026-07):** FalkorDB and PostgreSQL are fully removed from Sibyl as of the v0.6–v1.0 line.
> This guide documents the historical migration path; the current `sibyld migrate` CLI supports only
> `surreal-archive` → `surreal`. The `--source-type legacy-archive`,
> `--target-mode postgres-rehearsal`, `--restore-database-dump`, and `--postgres-base-url` flags
> below no longer exist.

Sibyl now starts the SurrealDB runtime by default. New installs should use SurrealDB for graph,
content, and auth. Do not start new deployments on FalkorDB or PostgreSQL auth.

Legacy FalkorDB runtime support was retired after the v0.6.0 compatibility release. PostgreSQL auth
was removed after the same release, and active content/RAG runtime paths now resolve through
SurrealDB. Structured auth/content archive export now reads SurrealDB; retained `postgres.sql`
payloads are restore-only evidence for rehearsal or rollback validation, not ambient runtime
sidecars.

## What changed

- `moon run dev` starts the SurrealDB runtime.
- `SIBYL_STORE=surreal` and `SIBYL_AUTH_STORE=surreal` are the default settings.
- The local `moon run dev-legacy` fallback has been retired after the v0.6.0 compatibility release.
- `moon run dev` detects local legacy data before creating a fresh Surreal dev runtime.
- The live local FalkorDB migration wrapper has been retired; use archive import instead.
- Graphiti Core is no longer a supported `sibyl-core` dependency. Legacy Graphiti-shaped archives
  and records are handled by Sibyl-owned Surreal projection and import code.

## Existing local installs

If you have an old local FalkorDB + PostgreSQL install, export an archive from the v0.6
compatibility release before upgrading, then import it into SurrealDB:

```bash
# HISTORICAL (removed v0.6–v1.0): the --source-type legacy-archive on-ramp no longer exists.
# The current CLI accepts only --source-type surreal-archive --target-mode surreal.
uv run --directory apps/api sibyld migrate import <archive> \
  --source-type legacy-archive \
  --target-mode surreal \
  --dry-run

uv run --directory apps/api sibyld migrate import <archive> \
  --source-type legacy-archive \
  --target-mode surreal \
  --yes \
  --clean
```

Every archive import, rehearsal, and cutover requires an explicit source type and target mode. Use
`--source-type surreal-archive --target-mode surreal` for Surreal-native archive restores. (The
`--source-type legacy-archive` on-ramp for historical FalkorDB/PostgreSQL migration archives was
removed in the v0.6–v1.0 line and no longer exists.)

Historically, `--restore-database-dump` replayed PostgreSQL rehearsal evidence, always paired with
`--source-type legacy-archive --target-mode postgres-rehearsal`; `postgres.sql` was a historical
migration payload, never the default restore path. Those flags were removed in the v0.6–v1.0 line
and no longer exist.

## Existing production installs

Use the full migration playbook, not the local shortcut:

1. Back up the legacy install.
2. Export the legacy archive.
3. Rehearse the archive against a disposable SurrealDB target.
4. Import into SurrealDB.
5. Start a Surreal-backed API on a private cutover endpoint.
6. Run archive verification.
7. Run the auth-flow replay against the Surreal-backed API.
8. Run the auth-flow replay against the target API.
9. Run the Surreal cutover gate while writes are frozen.
10. Freeze legacy auth/RBAC writes during the rollback window.

Start with [Migrating from FalkorDB](./migrating-from-falkor.md).

Use SurrealDB 3.x for the target runtime, and pin the exact server image/tag used during rehearsal
before cutting over production.

Release owners should execute the live gate checklist in
`docs/_archive/SURREALDB_PHASE2_LIVE_GATES.md` before tagging the SurrealDB-first release.

Historically, `sibyld migrate auth-flow-compare` compared a legacy Postgres-auth API against a
Surreal-auth API (distinct `--postgres-base-url` and `--surreal-base-url` values, with
`--allow-same-base-url` only for harness debugging). With PostgreSQL auth fully removed there is no
legacy API to start, so this cross-runtime comparison is historical.

## Phase 3 archive policy

These paths remain available as migration and rollback evidence while Phase 3 closes the old storage
surface:

- archive import, verify, and cutover commands for existing legacy installs
- Surreal-native archive restore through explicit
  `--source-type surreal-archive --target-mode surreal`
- retained `postgres.sql` restore — historically via
  `--restore-database-dump --source-type legacy-archive --target-mode postgres-rehearsal`; these
  flags were removed in the v0.6–v1.0 line and no longer exist
- graph archive payload import into SurrealDB with dry-run restore review before writes

The old PostgreSQL auth/RBAC runtime, active PostgreSQL content sidecars, ambient PostgreSQL
startup/sync code, local FalkorDB runtime fallback, and Graphiti FalkorDB adapter have been removed.
`SIBYL_STORE=legacy` is now source-side migration context for old installs, not a supported product
runtime for new deployments.

The active backup job writes Surreal auth/content snapshots and graph exports. It does not produce
new `postgres.sql` sidecars in fully Surreal mode.

## Rollback posture

The migration is explicit and archive-backed. Upgrading does not destructively rewrite your legacy
data. If a cutover fails before SurrealDB accepts new writes, point traffic back to the legacy API.

Once SurrealDB accepts new production writes, treating PostgreSQL or FalkorDB as an instant
read-write rollback is unsafe. Restore from Surreal backups or replay any affected writes
deliberately.
