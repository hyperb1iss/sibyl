---
title: SurrealDB Migration Release Notes
description: Release guidance for the SurrealDB-first storage cutover
---

# SurrealDB Migration Release Notes

Sibyl now starts the SurrealDB runtime by default. New installs should use SurrealDB for graph,
content, and auth. Do not start new deployments on FalkorDB or PostgreSQL auth.

Legacy FalkorDB runtime support was retired after the v0.6.0 compatibility release. PostgreSQL auth
was removed after the same release, and active content/RAG runtime paths now resolve through
SurrealDB. Structured auth/content archive export now reads SurrealDB; retained `postgres.sql`
payloads are restore-only evidence for rehearsal or rollback validation.

## What changed

- `moon run dev` starts the SurrealDB runtime.
- `SIBYL_STORE=surreal` and `SIBYL_AUTH_STORE=surreal` are the default settings.
- The local `moon run dev-legacy` fallback has been retired after the v0.6.0 compatibility release.
- `moon run dev` detects local legacy data before creating a fresh Surreal dev runtime.
- The live local FalkorDB migration wrapper has been retired; use archive import instead.
- `graphiti-core` is no longer a default `sibyl-core` runtime dependency. Install the
  `sibyl-core[compatibility]` extra only when running named Graphiti compatibility, migration, or
  admin surfaces that still require the old contracts.

## Existing local installs

If you have an old local FalkorDB + PostgreSQL install, export an archive from the v0.6
compatibility release before upgrading, then import it into SurrealDB:

```bash
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

Every archive import, rehearsal, and cutover now requires an explicit source type and target mode.
Use `--source-type surreal-archive --target-mode surreal` for Surreal-native archive restores. Use
`--source-type legacy-archive --target-mode surreal` for historical FalkorDB/PostgreSQL migration
archives imported into SurrealDB.

Use `--restore-database-dump` only for PostgreSQL rehearsal evidence, and always pair it with
`--source-type legacy-archive --target-mode postgres-rehearsal`. `postgres.sql` is a historical
migration payload, not the default restore path.

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

`sibyld migrate auth-flow-compare` refuses to compare one API to itself by default. Start one
legacy-auth API and one Surreal-auth API, then pass distinct `--postgres-base-url` and
`--surreal-base-url` values. Use `--allow-same-base-url` only when debugging the harness itself.

## Phase 3 archive policy

These paths remain available as migration and rollback evidence while Phase 3 closes the old storage
surface:

- archive import, verify, and cutover commands for existing legacy installs
- Surreal-native archive restore through explicit
  `--source-type surreal-archive --target-mode surreal`
- retained `postgres.sql` restore through explicit
  `--restore-database-dump --source-type legacy-archive --target-mode postgres-rehearsal` rehearsal
  commands
- graph archive payload import into SurrealDB with dry-run restore review before writes

The old PostgreSQL auth/RBAC runtime, active PostgreSQL content sidecars, ambient PostgreSQL
startup/sync code, local FalkorDB runtime fallback, and Graphiti FalkorDB adapter have been removed.
`SIBYL_STORE=legacy` is now source-side migration context for old installs, not a supported product
runtime for new deployments.

The active backup job writes Surreal auth/content snapshots and graph exports. It does not produce
new `postgres.sql` sidecars in fully Surreal mode.

## Rollback posture

The migration is explicit and archive-backed. Upgrading does not destructively rewrite your legacy
data. If a cutover fails before Surreal accepts new writes, point traffic back to the legacy API.

Once Surreal accepts new production writes, treating PostgreSQL as an instant read-write rollback is
unsafe. Restore from the pre-cutover archive and replay any Surreal-only writes deliberately.
