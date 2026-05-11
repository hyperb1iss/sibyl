---
title: SurrealDB Migration Release Notes
description: Release guidance for the SurrealDB-first storage cutover
---

# SurrealDB Migration Release Notes

Sibyl now starts the SurrealDB runtime by default. New installs should use SurrealDB for graph,
content, and auth. Do not start new deployments on FalkorDB or PostgreSQL auth.

Legacy FalkorDB + PostgreSQL content support remains available for existing installs during the
migration window. It is a compatibility bridge, not the long-term runtime. PostgreSQL auth was
removed after the v0.6.0 compatibility release; auth/RBAC now resolves through SurrealDB.

## What changed

- `moon run dev` starts the SurrealDB runtime.
- `SIBYL_STORE=surreal` and `SIBYL_AUTH_STORE=surreal` are the default settings.
- `moon run dev-legacy` is the explicit FalkorDB + PostgreSQL fallback.
- `moon run dev` detects local legacy data before creating a fresh Surreal dev runtime.
- `moon run dev -- --migrate-legacy` migrates the common single-org local setup automatically.

## Existing local installs

If you have an old local FalkorDB + PostgreSQL install, run:

```bash
moon run dev -- --migrate-legacy
```

For the common single-org setup, Sibyl selects the only organization automatically. You do not need
to know the org ID.

If multiple organizations exist, Sibyl prints the available org IDs and asks you to rerun with:

```bash
moon run migrate-local-surreal -- --org-id <org-uuid>
```

To keep using the old stack for debugging or migration work:

```bash
moon run dev-legacy
```

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
`docs/architecture/SURREALDB_PHASE2_LIVE_GATES.md` before tagging the SurrealDB-first release.

`sibyld migrate auth-flow-compare` refuses to compare one API to itself by default. Start one
legacy-auth API and one Surreal-auth API, then pass distinct `--postgres-base-url` and
`--surreal-base-url` values. Use `--allow-same-base-url` only when debugging the harness itself.

## Compatibility window

These paths stay available while Phase 3 removes the remaining legacy storage:

- `moon run dev-legacy`
- `SIBYL_STORE=legacy`
- PostgreSQL content archive export
- FalkorDB graph archive export

The old PostgreSQL auth/RBAC runtime has been removed. Remaining non-auth PostgreSQL consumers will
be removed in the Phase 3 storage cleanup.

## Rollback posture

The migration is explicit and archive-backed. Upgrading does not destructively rewrite your legacy
data. If a cutover fails before Surreal accepts new writes, point traffic back to the legacy API.

Once Surreal accepts new production writes, treating PostgreSQL as an instant read-write rollback is
unsafe. Restore from the pre-cutover archive and replay any Surreal-only writes deliberately.
