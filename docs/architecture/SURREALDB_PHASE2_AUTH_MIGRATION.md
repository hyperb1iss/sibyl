# Sibyl Phase 2 — Auth Migration Plan

Branch: `feat/surrealdb-driver-phase1` (assumes Phase 1 server-mode work has landed before Phase 2
begins).

Revision 4 — current implementation status after the Surreal-first dev cutover. The original
Revision 3 plan is preserved below where it still describes rollout mechanics, but several sections
now read as a roadmap ledger rather than future design.

---

## TL;DR

Sibyl now defaults to the Surreal runtime for graph, content, and auth in local development.
`SIBYL_STORE=surreal` and `SIBYL_AUTH_STORE=surreal` are the default settings, `moon run dev` starts
the Surreal path, and `moon run dev-legacy` is the explicit FalkorDB + PostgreSQL escape hatch.

The remaining Phase 2 work is no longer "make auth run without Postgres." That is working. The
remaining work is release hardening: live cutover rehearsal, migration docs, noisy release guidance,
and one-release support for the legacy auth escape hatch before the old auth/RBAC code is removed.

---

## Context

Sibyl is a graph-backed knowledge + task system. The data layer has moved from FalkorDB/PostgreSQL
toward SurrealDB on `feat/surrealdb-driver-phase1`. The current branch runs for extended periods
with PostgreSQL off in fully Surreal mode.

Legacy PostgreSQL and FalkorDB support still exists intentionally. It is there for migration,
rollback, and one release of compatibility, not because the new default runtime needs it. Startup
helpers now only bootstrap the relational sidecar when the configured runtime still requires it.

---

## Scope — in and out

### In scope for Phase 2

**Auth/auth-adjacent tables (14):** `users`, `user_sessions`, `password_reset_tokens`,
`login_history`, `organizations`, `organization_members`, `organization_invitations`, `api_keys`,
`api_key_project_scopes`, `oauth_connections`, `device_authorization_requests`, `audit_logs`,
`teams`, `team_members`.

**RBAC tables (3):** `projects`, `project_members`, `team_projects` — these are the relational
source of truth for RBAC that `apps/api/src/sibyl/auth/authorization.py` queries on every authed
request, and the sync layer `apps/api/src/sibyl/db/sync.py` treats them authoritatively. They move
together with auth.

**Access layer:** the existing backend-agnostic contracts in
`packages/python/sibyl-core/src/sibyl_core/auth/contracts.py`, the legacy adapters in
`apps/api/src/sibyl/persistence/legacy/*`, `apps/api/src/sibyl/persistence/auth_runtime.py` (now a
backend dispatcher), and the request-time consumers in `apps/api/src/sibyl/auth/dependencies.py`,
`rls.py`, `authorization.py`, `middleware.py`, `api_keys.py`.

**Tooling:** extend `packages/python/sibyl-core/src/sibyl_core/migrate/archive.py` and
`apps/api/src/sibyl/cli/migrate.py` with an auth payload and a scripted replay harness.

### Explicit non-goals (Phase 3 or later)

- Any remaining relational sidecar consumers outside auth/RBAC. Treat these as Phase 3 cleanup items
  and verify each one against the current code before claiming it still needs PostgreSQL.
- Removing asyncpg, SQLAlchemy, or Alembic.
- Removing the `postgres` service from `docker-compose.yml`.
- Migrating `DocumentChunk` + pgvector embeddings.
- Changing crypto (PBKDF2 iterations, JWT signature algorithm, cookie flags).
- Frontend changes — the web client already treats IDs as opaque strings.

---

## Target architecture

| Concern                           | Current state                                                                                     | Release target                                                                                                      |
| --------------------------------- | ------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------- |
| Auth records                      | SurrealDB SCHEMAFULL in `sibyl_auth/auth`; PostgreSQL retained behind `SIBYL_AUTH_STORE=postgres` | SurrealDB default for release; PostgreSQL auth escape hatch kept for one release                                    |
| Authorization (per-row)           | Application-layer authorization driven by `AuthContext`; legacy RLS isolated to legacy surfaces   | No auth/RBAC route depends on transaction-local PostgreSQL RLS                                                      |
| Password / token / API-key hashes | PBKDF2 in-app, hash stored in the active auth store                                               | Algorithm unchanged, Surreal remains default                                                                        |
| Session revocation lookup         | New access and refresh tokens carry `sid`; session cache and backend session checks exist         | Rehearsal must prove logout rejects `sid`-bearing access tokens immediately                                         |
| `api_keys.last_used_at`           | Surreal auth path performs field-scoped `UPDATE api_keys SET last_used_at ...`                    | Keep field-scoped updates; optional batched flusher can be a follow-up if auth hot-path profiling proves it matters |
| Schema migrations (auth)          | Idempotent Surreal bootstrap defines all 17 auth/RBAC tables as SCHEMAFULL                        | Bootstrap remains parser-tested and archive restore-safe                                                            |
| Relational sidecar                | Disabled in fully Surreal mode; enabled only for legacy store or PostgreSQL auth mode             | Warn loudly, document migration, then remove legacy auth mode after the compatibility release                       |

### Auth namespace layout

Dedicated top-level Surreal namespace `sibyl_auth`, database `auth`. Rationale: users are global
(`apps/api/src/sibyl/db/models.py:91-156` — unique email + github_id are enforced across all orgs),
first-user-becomes-admin logic is global (`apps/api/src/sibyl/auth/users.py:27-32`), and users span
multiple orgs via memberships (`apps/api/src/sibyl/persistence/legacy/auth.py:277-288`). Per-org
namespacing, which is correct for graph data, would fight the auth model.

Org-scoped auth tables (memberships, api_keys, invitations, audit_logs, etc.) live in the same
`sibyl_auth/auth` DB with `organization_id` columns; per-row org filtering is enforced by the
authorization layer, not by namespace isolation.

---

## Decision points — finalized

1. **Namespace layout**: dedicated top-level `sibyl_auth/auth`. Not per-org. (See above.)
2. **Projects/members/teams**: stay in dedicated Surreal tables. RBAC is relational in
   `apps/api/src/sibyl/auth/authorization.py:69-291` and synced via
   `apps/api/src/sibyl/db/sync.py:42-164`; making the graph the source of truth is a separate
   redesign, not a store migration.
3. **Document/content storage**: content now has a Surreal runtime and archive path. Any remaining
   relational sidecar surfaces belong to Phase 3 cleanup and should be tracked as concrete code
   references rather than assumed from the old PostgreSQL model.
4. **Cutover style**: strict archive-backed cutover with a brief write-freeze. **No production
   dual-write.** Auth flows already span writes across users/orgs/memberships/sessions/audit
   (`apps/api/src/sibyl/persistence/legacy/auth.py:309-347,606-683`) with no cross-store transaction
   boundary. Dual-write would manufacture divergence, not safety.

---

## Pre-existing latent bugs — track independently

These are real bugs Codex surfaced while auditing the current auth code. They are not caused by
Phase 2, but Phase 2 must not make them worse and should ideally fix them in the process.

### Bug 1 — access-token revocation was a no-op

HTTP auth decodes JWTs without consulting session state
(`apps/api/src/sibyl/auth/dependencies.py:56-91`, `apps/api/src/sibyl/auth/middleware.py:22-35`),
and WebSocket auth does the same (`apps/api/src/sibyl/api/websocket.py:309-320`).
`UserSession.revoked_at` is written on logout but never checked on subsequent access-token use. A
"revoked" user continues to authenticate until the JWT expires.

**Status:** new token issuance includes `sid`, and auth-flow replay asserts the `sid` claim. Keep
logout/revocation behavior in the cutover acceptance gate until it has passed against a live
production-like dataset.

### Bug 2 — RLS context gets silently dropped mid-request

`get_auth_session` sets transaction-local `set_config(..., true)` once per session
(`apps/api/src/sibyl/auth/rls.py:97-106,273-279`), but
`apps/api/src/sibyl/api/routes/users.py:153-154,201-202,243-244` commits mid-request. Subsequent
transactions in the same request run without RLS context, which on SELECT means returning empty rows
rather than erroring. Data-leak risk is low because `current_setting(..., true)` returns NULL on
mismatch, but availability is broken.

**Status:** auth-only user routes no longer depend on `get_auth_session`; they resolve `AuthContext`
directly and call runtime-backed repositories. The non-auth tables using RLS still need a separate
Phase 3 decision if they keep relational storage.

---

## Implementation status

### Done on this branch

- `moon run dev` prefers the fully Surreal runtime and detects local legacy data before starting.
- `moon run dev -- --migrate-legacy` handles the common single-org local migration path without
  requiring users to know their org ID.
- `moon run dev-legacy` remains the explicit FalkorDB + PostgreSQL fallback.
- `SIBYL_STORE=surreal` and `SIBYL_AUTH_STORE=surreal` are the default runtime shape.
- `apps/api/src/sibyl/persistence/auth_runtime.py` dispatches by auth backend instead of
  re-exporting the legacy PostgreSQL implementation.
- `packages/python/sibyl-core/src/sibyl_core/backends/surreal/auth_schema.py` defines all 17
  auth/RBAC tables as SCHEMAFULL, including the formerly archive-only tables.
- Auth archive export/restore writes `auth.json`; backup archives include `auth.json` when auth runs
  on Surreal.
- `sibyld migrate auth-flow`, `auth-flow-compare`, `rehearse`, `cutover`, and `auth-readonly` exist
  and have unit coverage.
- Legacy auth/RBAC write-freeze SQL is generated by the migration CLI rather than hand-written
  during cutover.
- Release guidance now lives in `docs/guide/surrealdb-migration-release-notes.md` and links from the
  storage mode and FalkorDB migration guides.
- The generated runtime inventory at `docs/research/rust-port/INVENTORY.md` tracks the remaining
  SQLModel, raw SQL, and session-backed storage coupling from the current code.

### Remaining before release

- Run a live rehearsal against a production-like legacy archive and a fresh Surreal runtime.
- Run `auth-flow-compare` against live PostgreSQL-auth and Surreal-auth stacks, not only unit fakes.
- Validate the local migration path from a real single-org legacy install and a multi-org install.
- Keep the SurrealDB migration release notes current as live rehearsal results land.
- Keep `SIBYL_AUTH_STORE=postgres` for one compatibility release, then remove legacy auth/RBAC code.
- Keep the Phase 3 relational burn-down plan current as the generated inventory shrinks:
  `docs/architecture/SURREALDB_PHASE3_BURNDOWN.md`.

---

## Phased plan

### Phase 2.0 — Unblock surreal-mode dev (half-day)

**Status:** done, superseded by the fully Surreal local default.

**Goal:** `moon run dev` boots a working stack while Phase 2 is in flight, without pretending
Postgres has been removed.

- Drop the `legacy` profile gate on `postgres` in `docker-compose.yml:56-74`. FalkorDB stays gated.
- Add `postgres` to the services list in `tools/dev/run-surreal-dev.sh:31-53`.
- Centralize the "legacy Postgres still required" startup path and call it from both
  `apps/api/src/sibyl/main.py` and `apps/api/src/sibyl/api/app.py` when auth or any remaining
  Postgres-backed subsystem is enabled, even when `SIBYL_STORE=surreal`. Today both startup paths
  skip pieces of PG init in surreal mode, which means even a running postgres container is not
  enough: migrations may not run and `services/settings.py` may never load.
- Verify startup produces a working `/api/auth/me` for a logged-in dev user.

**Exit:** contributors can pull the branch, run `moon run dev`, log in, and exercise authed
endpoints without manual compose juggling.

### Phase 2.1 — Surreal auth schema + repo layer, parallel (week 1)

**Status:** done. The Surreal auth client, repositories, dispatcher, schema bootstrap, and table
contracts are implemented and covered by parser/bootstrap tests.

**Goal:** a complete Surreal-backed implementation of the auth data layer, flagged off.

- Add a dedicated Surreal auth client/session helper. Do **not** overload
  `packages/python/sibyl-core/src/sibyl_core/backends/surreal/driver.py` directly: that driver is
  group-id / graph oriented and assumes per-org namespace switching, while auth needs a fixed
  top-level namespace/database.
- New file `packages/python/sibyl-core/src/sibyl_core/backends/surreal/auth_schema.py`. Define 17
  SCHEMAFULL tables: the 14 auth/auth-adjacent tables above plus `projects`, `project_members`, and
  `team_projects`. Keep stable UUIDs identical to Postgres IDs. Indexes:
  - UNIQUE on `users.email`, `users.github_id`, `organizations.slug`,
    `organization_invitations.token`, `password_reset_tokens.token_hash`,
    `device_authorization_requests.device_code_hash`, `device_authorization_requests.user_code`.
  - UNIQUE composite on `oauth_connections(provider, provider_user_id)`,
    `teams(organization_id, slug)`, `projects(organization_id, graph_project_id)`,
    `organization_members(organization_id, user_id)`, `team_members(team_id, user_id)`,
    `project_members(project_id, user_id)`, `team_projects(team_id, project_id)`, and
    `api_key_project_scopes(api_key_id, project_id)`.
  - NON-UNIQUE on `api_keys.key_prefix` and `projects(organization_id, slug)`. API key auth
    intentionally queries all prefix matches then verifies hashes; project slug lookup must tolerate
    sparse restored rows with empty or missing slugs.
  - Keep denormalized `organization_id` on `project_members` and `team_projects` for parity with the
    current schema and cheaper policy filters.
- Add `updated_at` to mutable records. Add `version` only where compare-and-swap semantics are
  required, starting with `user_sessions` and only extending further if a concrete concurrent-write
  path needs it.
- Implement Surreal repositories behind the existing `sibyl_core.auth` contracts (`UserRepository`,
  `OrganizationRepository`, `OrganizationMembershipRepository`, `SessionRepository`). Keep thin
  adapters in `apps/api/src/sibyl/persistence/surreal/` only where callers still expect the current
  function-style surfaces. This keeps the Wave 1 seams intact instead of cloning the legacy layer
  wholesale.
- New file `apps/api/src/sibyl/persistence/surreal/audit.py` implementing an append-only contract at
  the repo layer: the only exposed operation is `append(event)`; no update, no delete, no overwrite.
  Immutability moves from documentation (`apps/api/src/sibyl/db/models.py:364-408` claims it but
  `apps/api/src/sibyl/auth/audit.py:23-44` doesn't enforce it) to compiled-in behavior.
- Extract shared auth/RBAC enums and DTOs out of `apps/api/src/sibyl/db/models.py` before cleanup.
  Today many non-ORM callers still import `OrganizationRole`, `ProjectRole`, `ProjectVisibility`,
  and `TeamRole` from that module.
- Environment flag `SIBYL_AUTH_STORE` (values: `postgres` default, `surreal`).
  `apps/api/src/sibyl/persistence/auth_runtime.py` becomes a dispatcher, not a re-export.
- Zero consumer flips yet. Unit-tested in isolation.

**Exit:** running with `SIBYL_AUTH_STORE=postgres` is unchanged; running with
`SIBYL_AUTH_STORE=surreal` against an ephemeral Surreal produces equivalent outcomes for a curated
battery of contract/repository tests.

### Phase 2.2 — RLS → application-layer authorization (week 1)

**Status:** substantially done for auth/RBAC routes. Legacy RLS remains available for legacy
relational surfaces and should not be imported by newly migrated route code.

**Goal:** replace five distinct Postgres RLS policy shapes with explicit, testable application
checks.

The five shapes (derived from `apps/api/alembic/versions/0006_row_level_security.py:28-53,105-197`)
are:

1. **Org-scoped**: `organization_members`, `teams`, `projects`, `project_members`, `team_projects`,
   `audit_logs`, `organization_invitations`. (`crawl_sources` stays on the legacy Postgres path for
   Phase 3.)
2. **User-scoped**: `user_sessions`, `login_history`, `password_reset_tokens`, `oauth_connections`.
3. **User+org combined**: `api_keys`.
4. **Join-derived ownership**: e.g., `api_key_project_scopes` rows are visible iff the owning
   `api_key` row is visible.
5. **Pending-public / user-scoped hybrid**: `device_authorization_requests` are visible while
   pending and collapse back to user-scoped once approved or consumed.

Work:

- Replace `apps/api/src/sibyl/auth/rls.py`'s ambient Postgres-session-variable model with explicit
  authorization helpers in `apps/api/src/sibyl/auth/authorization.py`. One helper per policy shape,
  each taking `AuthContext` plus the row/query intent, returning either a filter predicate or a
  boolean authorization result.
- Rewrite `apps/api/src/sibyl/auth/dependencies.py` and related consumers so request auth resolution
  comes from the runtime-backed repositories rather than direct SQLModel auth rows.
- Split today's `AuthSession` usage into "auth context" and "plain storage session" where
  appropriate. Auth-only user/task routes now depend on `AuthContext` directly.
- Delete auth-specific `set_config` usage and `get_rls_session` / `apply_rls_from_auth_context` /
  `require_rls_session` wiring once no auth/RBAC route depends on them. Any remaining non-auth RLS
  usage becomes an explicit Phase 3 follow-up, not a hidden Phase 2 dependency.
- Keep `apps/api/src/sibyl/api/routes/tasks.py` and `apps/api/src/sibyl/api/routes/users.py` on
  `AuthContext`, with the direct-storage guard blocking route imports of `sibyl.auth.rls`.
- Treat **latent bug 2** as resolved for auth-only user routes; any remaining transaction-local RLS
  behavior belongs to non-auth relational surfaces.
- Add negative-case tests per policy shape: impersonate Org A, assert no Org B rows visible on
  reads/lists/joins.
- Add one integration test that exercises the full matrix (every route, two orgs, cross-org attempt)
  and asserts zero cross-org data leak.

**Exit:** no surface in `apps/api/src/sibyl/` reads `current_setting('app.user_id')` or
`current_setting('app.org_id')` for an auth-migrated table. Policies are enforced at the repo layer.

### Phase 2.3 — Concurrency + session hot path (week 2)

**Status:** partially done. `sid` issuance, session cache plumbing, and optimistic refresh-token
rotation exist. The Redis-batched `api_keys.last_used_at` flusher was replaced by direct
field-scoped Surreal updates unless profiling shows the extra queue is worth it.

**Goal:** handle the hot path and concurrent-mutation cases that Postgres RLS + MVCC were implicitly
covering.

- Change token issuance first: mint a concrete session UUID before signing tokens, add `sid` to
  access tokens, and require `sid` on newly issued refresh tokens. Thread this through signup/login,
  org switch, invitation acceptance, device auth, and browser login flows. Today
  `create_access_token` has no `sid`, and initial refresh tokens are also minted without one.
- Session cache: Redis hash keyed by `session_id`, value
  `{user_id, org_id, revoked, refresh_token_expires_at}`. TTL = time-to-`refresh_token_expires_at`,
  **not** JWT exp. Reason: `UserSession.refresh_token_expires_at`
  (`apps/api/src/sibyl/db/models.py:331-340`, `apps/api/src/sibyl/auth/sessions.py:104-115`,
  `apps/api/src/sibyl/api/routes/auth.py:957-1028`) is the authoritative session lifetime; caching
  to JWT exp would cause 30-day refresh cookies to 401 after one hour.
- Revocation path: `revoke_session` writes `revoked_at` in Surreal and sets `revoked=true` on the
  Redis entry. Access-token auth consults Redis by `sid` claim on every request; during the
  compatibility window for pre-`sid` access tokens, fall back to a short-lived
  `revoked_access_token:{sha256(token)}` entry keyed to the access-token expiry. This fixes **latent
  bug 1** without lying about legacy-token compatibility.
- `api_keys.last_used_at` flusher:
  - On auth, write `last_used_at` to Redis keyed by the resolved `api_key_id`.
  - A periodic arq task (every 60s) flushes pending entries via scoped SurrealQL
    `UPDATE api_key SET last_used_at = $ts WHERE uuid = $uuid` — field-scoped update only. **Never**
    full-record upsert (Surreal graph path uses delete+create,
    `packages/python/sibyl-core/src/sibyl_core/graph/surreal/ops/entity_node_ops.py:61-87`, which
    would resurrect revoked keys by clearing `revoked_at`).
  - Flusher skips records whose `revoked_at` is non-null at flush time (defense in depth).
- Refresh-token rotation concurrency:
  - `apps/api/src/sibyl/auth/sessions.py:117-141` replaces token hashes in place; three code paths
    trigger this today (refresh exchange at `apps/api/src/sibyl/persistence/legacy/auth.py:606-652`,
    org switch at `apps/api/src/sibyl/persistence/legacy/orgs.py:63-103`, invitation accept at
    `apps/api/src/sibyl/persistence/legacy/org_invitations.py:141-187`).
  - Migrate each to an optimistic CAS on `user_sessions.version`:
    `UPDATE user_session SET refresh_token_hash = $new, version = version + 1 WHERE uuid = $uuid AND version = $expected`.
    On version mismatch, retry once; on second mismatch, return
    `401 Session conflict, please re-authenticate`.
- Redis-backed locks (via existing `apps/api/src/sibyl/locks.py`) for first-user-admin bootstrap,
  signup-with-email-race, and OAuth account linking.

**Exit:** two concurrent refreshes on the same session produce exactly one new token pair + one
`401 Session conflict`; a revoked API key stops working within the next authenticated call (before
the next flush completes); `/api/auth/logout` invalidates newly issued `sid`-bearing access tokens
immediately and still catches pre-`sid` tokens during the compatibility window.

### Phase 2.4 — Auth archive + snapshot replay harness (week 2)

**Status:** implemented in CLI and tests. The remaining release gate is a live rehearsal with real
legacy data and a fresh Surreal target.

**Goal:** end-to-end export/import/verify tooling for auth, plus a scripted auth flow that must pass
against both stores before cutover.

This phase **replaces the Phase 2.4 "dual-write" slot from draft 1**. Dual-write is cut.

- Extend `packages/python/sibyl-core/src/sibyl_core/migrate/archive.py:18-20,300-373` with an
  `auth.json` payload alongside `graph.json` and `postgres.sql`. Structure: one array per auth/RBAC
  table, records serialized with Surreal-compatible types and stable UUIDs.
- Add export logic in `apps/api/src/sibyl/cli/migrate.py` that dumps all 17 Phase 2 auth/RBAC tables
  from Postgres into `auth.json`.
- Add import logic that reads `auth.json` and bulk-inserts into Surreal via the new
  `persistence/surreal/` layer. Idempotent: re-running against a populated Surreal is a no-op.
- Extend the existing migration tooling rather than inventing a parallel cutover path.
  `tools/dev/migrate-auth-rehearsal.sh` can exist as a thin wrapper, but the source of truth should
  live in `apps/api/src/sibyl/cli/migrate.py`.
- New deterministic auth flow harness:
  1. Signup new user (email + password)
  2. Log in, obtain access + refresh tokens
  3. Refresh token exchange
  4. Create API key
  5. Authenticate with API key
  6. Revoke API key, verify next call fails
  7. Invite user to org, accept invitation
  8. Switch active org
  9. Device auth flow (request + approve + exchange)
  10. Logout, verify access token is rejected immediately (latent bug 1 regression guard)
  11. List user's sessions, verify correct
  12. Password reset request + consume
- Harness must pass against both `SIBYL_AUTH_STORE=postgres` and `SIBYL_AUTH_STORE=surreal` with
  **normalized semantic equivalence**, not byte equality. Compare status codes, response shapes,
  role/scope behavior, decoded JWT claims (`sub`/`org`/`typ`/`sid` presence) after ignoring
  signature/`iat`/`exp`/`jti`, and observable side effects such as memberships, sessions, and
  revocations. Raw tokens, timestamps, and generated IDs are expected to differ.
- Verification addition to `apps/api/src/sibyl/cli/migrate.py:177-231,576-735`: before `cutover`
  succeeds, the harness must green for the target environment.

**Exit:** one command seeds a staging Surreal from a Postgres archive; one command runs the full
auth flow against either store; cutover is gated on a green run.

### Phase 2.5 — Cutover (week 3, single PR per environment)

**Status:** tooling exists; release execution remains.

**Goal:** flip the production flag with a defensible rollback story.

- `SIBYL_AUTH_STORE=surreal` becomes the deployment default. `postgres` retained as an escape hatch
  for exactly one release.
- Per environment:
  1. Announce write-freeze window (~5 minutes).
  2. Drain in-flight auth writes.
  3. Run the extended `sibyl migrate cutover` auth gate: archive auth from Postgres, verify row
     counts match (`users`, `organizations`, `organization_members`, `api_keys`, active
     `user_sessions`, RBAC tables), import to Surreal, run the Phase 2.4 harness, then flip the env
     flag.
  4. Unfreeze.
- **Explicit non-promise:** once Surreal accepts new auth writes, "flip reads back to Postgres" is
  not a real rollback. Doing so after N new signups have landed on Surreal produces divergence, not
  recovery. If cutover fails, the rollback is "restore Postgres from the pre-cutover archive, replay
  Surreal writes manually if any occurred, flip flag back before resuming traffic."
- Postgres auth/RBAC tables become read-only for one release (trigger-based write rejection, not
  process guidance) so an accidental code path can't double-write.

**Exit:** prod runs `SIBYL_AUTH_STORE=surreal` for one release cycle with Postgres alive but
read-only on auth tables.

### Phase 2.6 — Auth-only cleanup (week 3-4)

**Status:** not started. Do this after one compatibility release on Surreal auth.

**Goal:** remove the auth slice of Postgres. Nothing more.

- Delete: `apps/api/src/sibyl/persistence/legacy/auth.py`, `persistence/legacy/orgs.py`,
  `persistence/legacy/org_invitations.py`, `persistence/legacy/project_members.py`.
- Before removing auth SQLModel classes from `apps/api/src/sibyl/db/models.py`, move shared
  enums/value objects that non-auth code still imports into a non-ORM module and update imports.
- Preserve historical Alembic revisions as legacy history. Do **not** rewrite old migration files in
  place. Instead, add a new migration that drops the auth/RBAC tables and update docs to mark the
  older auth revisions as historical-only once Phase 2 is complete.
- Drop auth tables from Postgres via a new migration.
- Remove the `SIBYL_AUTH_STORE=postgres` dispatch branch from
  `apps/api/src/sibyl/persistence/auth_runtime.py`. The flag becomes inert and is removed.
- **Keep:** `apps/api/src/sibyl/db/connection.py`, asyncpg, SQLAlchemy, Alembic, the `postgres`
  compose service, `services/settings.py`, `routes/crawler.py`, `jobs/backup.py`,
  `routes/entities.py` raw captures, `services/document_search.py`, `persistence/legacy/rag.py`.
  These stay until Phase 3.
- Update `README.md`, `CLAUDE.md`, `skills/sibyl/SKILL.md`, and the `sibyld db` subcommands to
  reflect that auth no longer lives in Postgres but non-auth state still does.
- Append rollout notes and deviations to this doc (or a short follow-up retro) after cutover, and
  capture any non-obvious learnings in Sibyl.

**Exit:** Sibyl's auth layer has no Postgres dependency. Non-auth Postgres dependencies are
untouched.

---

## Phase 3 — scope preview (not planned in this doc)

A follow-up phase must migrate or remove the remaining Postgres consumers before `asyncpg` /
`sqlalchemy` / `alembic` / the `postgres` compose service can be removed. Use
`docs/research/rust-port/INVENTORY.md` as the source of truth and
`docs/architecture/SURREALDB_PHASE3_BURNDOWN.md` as the execution plan.

Current status:

- Settings, backups, raw captures, crawl sources, crawled documents, and document chunks all have
  Surreal runtime paths.
- The active content and settings dispatchers no longer import the legacy session module at load
  time.
- Legacy SQL remains in the compatibility implementations, archive/export helpers, auth/RBAC
  fallback, and DB maintenance commands.

The Phase 3 burn-down should split into:

- Delete the legacy-only auth/RBAC runtime after one compatibility release.
- Prove the Surreal settings, backup, and content paths with live migration rehearsals.
- Decide whether `postgres.sql` backup sidecars remain supported after content/auth are
  Surreal-only.
- Remove or replace the remaining RLS helpers once no active route depends on ambient Postgres RLS.
- Drop SQLModel tables, Alembic, asyncpg, pgvector, and the Postgres compose service only after the
  inventory shows no active relational consumers.

Phase 3 is out of scope for this plan.

---

## Risks and mitigations

| Risk                                                                                  | Mitigation                                                                                                                                                                        |
| ------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Silent data loss on cutover                                                           | Phase 2.4 harness must green; archive retained; hash counts (`users`, `organizations`, `organization_members`, active `user_sessions`, `api_keys`) verified pre- and post-import. |
| Cross-org data leak during RLS → app-layer translation                                | Per-policy-shape negative-case tests in Phase 2.2; full-matrix integration test; code review checklist flags any Surreal query missing an authorization filter.                   |
| Concurrent refresh collisions producing orphaned tokens                               | Optimistic CAS on `user_sessions.version` in Phase 2.3; loser returns 401 and re-auths, not wins silently.                                                                        |
| Revoked API key "resurrected" by `last_used_at` flusher                               | Field-scoped UPDATE only; flusher skips `revoked_at IS NOT NULL`.                                                                                                                 |
| Redis session cache outlives refresh token                                            | TTL derived from `refresh_token_expires_at`, not JWT exp.                                                                                                                         |
| `sid` rollout breaks existing access-token revocation semantics                       | Add `sid` to newly issued access tokens and keep a temporary token-hash revocation fallback for pre-`sid` tokens until the compatibility window expires.                          |
| First-user-becomes-admin race creates multiple admins                                 | Protect the bootstrap path with a distributed lock around `has_any_users()` + create-user issuance.                                                                               |
| Embedded Surreal storage concurrency (Phase 1 blocker)                                | Phase 2 does not start until Phase 1 server-mode landing is confirmed.                                                                                                            |
| OAuth token encryption keys mis-travel during migration                               | `oauth_connections.access_token_encrypted` uses app-layer crypto; migration copies ciphertext bytes unchanged and re-verifies decryption post-import on a sample.                 |
| Rollback after partial Surreal writes during cutover                                  | No promise of hot rollback. Rollback is freeze → archive-restore → flag-flip → thaw.                                                                                              |
| Audit log mutation regression                                                         | Append-only contract enforced at the repo layer in Phase 2.1, not process guidance.                                                                                               |
| Latent bug 1 (access-token revocation no-op) regressing                               | Phase 2.4 harness step 10 (logout → verify access token rejected) is a hard gate.                                                                                                 |
| Latent bug 2 (mid-request commits dropping RLS context) regressing on non-auth tables | Out of Phase 2 scope, but tracked as a separate task; Phase 2 must not introduce equivalent patterns in the new authorization layer.                                              |
| Cleanup rewrites migration history in ways old envs cannot reproduce                  | Preserve historical Alembic files and add forward-only cleanup migrations instead of editing old revisions.                                                                       |

---

## Success criteria

- All auth and auth-adjacent routes work against `SIBYL_AUTH_STORE=surreal`, and their observable
  behavior matches `postgres` mode after the Phase 2.4 normalization rules are applied.
- Touched projects pass `moon run :check`, and targeted auth/migration suites are green in both
  `postgres` and `surreal` auth-store modes.
- Phase 2.5 cutover runs cleanly in a staging environment with a production-like dataset.
- The Wave 1 contract seams remain intact: no new direct storage access leaks out of persistence
  modules.
- One release cycle in prod on `SIBYL_AUTH_STORE=surreal` with Postgres auth tables read-only and
  zero auth-related incidents attributable to the migration.
- Phase 2.6 PR removes auth-only Postgres code without touching non-auth consumers.

---

## Out of plan, worth capturing separately

- Sibyl task: fix latent bug 1 — access-token revocation must consult session state (can be fixed
  standalone before Phase 2.3 if timing allows).
- Sibyl task: decide the non-auth RLS posture for any relational tables that survive the Surreal
  cutover. Auth-only user/task routes no longer depend on transaction-local RLS state.
- Sibyl task (informational): document in `docs/architecture/` that audit-log immutability is a
  repo-layer contract, not a DB-layer guarantee, and the invariant must be preserved in Phase 3 for
  any audit table that moves later.
