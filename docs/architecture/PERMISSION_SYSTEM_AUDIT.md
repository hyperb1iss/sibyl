# Sibyl Permission System Audit (API/CLI/Web/Core)

Date: 2026-01-04

Scope: `apps/api`, `apps/cli`, `apps/web`, `packages/python/sibyl-core`

This is a code-audit report of Sibyl’s authentication/authorization/multi-tenancy system, with an
emphasis on security properties, correctness gaps, and operational/performance risks.

Update (2026-03-29): Sibyl's internal agent runtime, approval flow, and sandbox control plane were
removed after this audit was written. The findings below have been trimmed so the document reflects
the remaining live surfaces.

Update (2026-05-13): v0.8 B0 reconciled this audit against the Surreal auth/runtime implementation.
The old Postgres/FalkorDB wording below is retained as historical context, but the active control
plane is now Surreal-backed. Treat this update and the trust-surface inventory as the current
release-planning source of truth.

Update (2026-05-15): this audit remains an active input for
[`SIBYL_1_0_ROADMAP.md`](SIBYL_1_0_ROADMAP.md). v0.8 and v0.9 closed the main memory-trust release
gates, but 1.0 still needs boring CLI session behavior, persisted memory-space control-plane
records, agent delegation, stricter API-key and MCP policy coverage, job policy receipts, and
complete inspect/audit parity.

## TL;DR (Highest-Risk Items)

1. **Project RBAC is now Surreal-backed, but project records remain the critical control-plane
   boundary.** Graph project create/update/delete writes project records through the Surreal auth
   runtime, and `verify_entity_project_access()` denies non-viewer fallbacks for missing or
   unregistered projects. The remaining risk is read-side drift where a route does not ask for
   accessible project IDs or direct project authorization.
2. **Direct entity list/get and raw-capture endpoints are the highest-priority read-side audit
   targets.** Search, explore, context packs, raw memory, and MCP retrieval pass project filters,
   but `GET /api/entities`, `GET /api/entities/{id}`, and `/api/entities/captures` need explicit
   project access status before project-private data can be considered release-safe.
3. **MCP is no longer an org-only bypass for the main memory loop, but generic graph mutation is not
   yet fully policy-shaped.** MCP `search`, `explore`, `context`, `remember`, and `reflect` carry
   user/org/project context; MCP `add` and `manage` still rely on generic graph/task authorization
   rather than raw-memory policy decisions.
4. **Memory policy exists and is shared for raw memory and reflection, but audit/inspect is still
   incomplete.** Raw remember/recall log policy decisions; context packs and native retrieval carry
   policy metadata internally; humans still need a first-class inspect surface for why memory was
   shown, hidden, written, or denied.
5. **Postgres RLS is no longer part of active request isolation.** The old RLS finding is now
   documentation debt, not an active Surreal release blocker. Any remaining RLS references should be
   labeled historical or removed from default-runtime docs.

First security pass for v0.8: harden direct entity/read surfaces in B2, route MCP/CLI/jobs through
one policy context in B3, add audit and inspect in B4, then run the B6 memory trust release gate.

## 2026-05-13 Surreal Auth Reconciliation

### Active authorization model

- **Auth store:** Surreal auth namespace/database via `apps/api/src/sibyl/persistence/surreal`.
- **Org context:** JWT and API-key auth resolve `AuthContext` with user, organization, org role,
  scopes, and API-key project restrictions.
- **Graph tenancy:** Surreal namespace-per-org for graph data. The active group ID is the current
  organization ID, not FalkorDB database-per-org.
- **Project control plane:** Surreal `projects`, `project_members`, `team_projects`, and
  `api_key_project_scopes` records are the project authorization source of truth.
- **Project IDs:** external APIs and graph entities use graph project IDs such as `project_<hash>`;
  the Surreal auth runtime stores them in `projects.graph_project_id`.
- **Project fallback:** if no project control-plane records exist for an org,
  `list_accessible_project_graph_ids()` falls back to graph project entities. This is migration
  compatibility and should be retired or feature-gated during B2.
- **Project record repair:** owner/admins can preview or apply
  `POST /api/admin/backfill/project-records` to create missing Surreal auth `projects` records from
  existing graph project entities before stricter project gates are enforced.
- **Memory policy:** `sibyl_core.auth.memory_policy` currently enables private, verified project,
  and verified delegated scope reads/writes/reflection. Share is deny-only across scopes until
  promotion/share preview work lands. Team, organization, shared, and public scopes return stable
  denies.

### Trust Surface Inventory

| Surface                                          | Identity/context carried                                                   | Current policy status                                                                                                                                                                | Follow-up                                                                                           |
| ------------------------------------------------ | -------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------- |
| REST `/api/memory/raw`                           | user ID, org, optional agent, `project_id`, `memory_scope`, `scope_key`    | Uses shared write policy and logs `memory_policy_decision`. Project scope checks membership through `scope_key`; diary project metadata is separately verified.                      | B3 should canonicalize `project_id` and project `scope_key` so metadata and policy cannot drift.    |
| REST `/api/memory/raw/recall`                    | user ID, org, optional agent, `project_id`, `memory_scope`, `scope_key`    | Uses shared read policy and logs `memory_policy_decision`; project scope requires verified `scope_key`.                                                                              | B3 should align `project_id` filters with project scope and keep deny reasons stable.               |
| REST `/api/memory/reflection/promote`            | user ID, org, target project, target scope, related IDs                    | Verifies target project access and delegates reflect/write policy to native promotion.                                                                                               | B4 should expose promotion policy decisions and source IDs through inspect/audit.                   |
| REST `/api/context/pack`                         | user ID, org, optional agent, optional project, accessible projects        | Verifies explicit project reads or passes accessible project set into core retrieval. Raw-memory recall uses shared read policy internally.                                          | B4 should surface hidden/allowed policy reasons in context-pack metadata.                           |
| REST `/api/context/reflect`                      | user ID, org, optional project, accessible projects, project/private scope | Verifies explicit project reads and passes reflect/write policy context into core reflection when persisting.                                                                        | B3 should make route-level audit wording match raw memory decisions.                                |
| REST `/api/search` and `/api/search/explore`     | user ID, org, explicit project or accessible project set                   | Explicit projects are verified; unscoped searches pass accessible project IDs into core search/explore.                                                                              | B2 should add direct leak tests for project-private entities and related traversals.                |
| REST `/api/search/temporal`                      | org only                                                                   | Temporal edge queries are org-scoped compatibility/history reads and do not currently apply project policy context.                                                                  | B2 should either add project filtering or label the route historical/admin-only.                    |
| REST `/api/entities` list/get                    | org only on list/get route; write routes carry user/project checks         | Write routes verify project access. Direct list/get currently need explicit read-side project authorization status.                                                                  | B2 owns direct entity list/get filtering and negative tests.                                        |
| REST `/api/entities/captures`                    | org only; update requires org write role                                   | Raw capture sidecar is org-scoped and reviewable, but not memory-policy scoped.                                                                                                      | B4 should either classify it as legacy capture review or add project/source policy metadata.        |
| REST `/api/rag` and `/api/session`               | user ID, org, accessible project set                                       | RAG and session-bundle routes resolve accessible projects before retrieval, but are outside raw-memory policy decisions.                                                             | B4 should include them in inspect/audit output or explicitly classify them as derived context.      |
| MCP `search`, `explore`, `context`               | user ID, org, scopes, API-key project restrictions, optional project/agent | No longer org-only; accessible project IDs are resolved and passed to core tools.                                                                                                    | B3 should add explicit deny-case coverage for restricted API keys and missing users.                |
| MCP `remember`                                   | user ID, org, project, memory scope, raw source IDs, related IDs           | Policy-backed: writes raw memory first, logs `mcp_memory_policy_decision`, then creates graph memory with raw source metadata.                                                       | B4 should make raw/graph pairing inspectable.                                                       |
| MCP `reflect`                                    | user ID, org, project, accessible projects, memory scope                   | Passes policy context into core reflection and native writes.                                                                                                                        | B3/B4 should align audit and inspect output with REST reflection.                                   |
| MCP `add`                                        | user ID and org metadata; optional project                                 | Generic graph mutation path, not raw-memory policy-backed.                                                                                                                           | B3 should either route agent memory writes through `remember` or add project/policy gates to `add`. |
| MCP `manage`                                     | user ID and org injected into action data                                  | Depends on each management action; not a unified memory policy surface.                                                                                                              | B3 should classify each manage action and reject memory-sensitive actions without policy context.   |
| CLI `remember`, `recall`, `context`, `reflect`   | bearer token/API key, linked project, optional agent/scope flags           | Thin REST client; server policy is authoritative. `remember` now captures raw memory before graph entity creation.                                                                   | B3 should keep CLI output showing server policy reasons and avoid local policy forks.               |
| Prompt hook `user-prompt-submit.py` (removed)    | n/a                                                                        | Removed; per-prompt context injection substituted for the `sibyl` skill instead of prompting agents to invoke it. Agents now drive recall via `sibyl recall` / `sibyl context pack`. | n/a                                                                                                 |
| Async jobs `apps/api/src/sibyl/jobs/entities.py` | group/org ID plus queued entity/task data                                  | Jobs persist graph entities and task learning artifacts after the API has authorized the enqueueing route; job payloads do not carry a full policy decision.                         | B3 should carry actor/project/policy receipt fields into job payloads and learning artifacts.       |

### Test Coverage Status

- Present: core memory policy allow/deny tests for private, project, delegated, and disabled scopes.
- Present: REST raw memory tests cover project membership allow/deny, missing scope key, diary
  constraints, and promotion project verification.
- Present: REST context tests cover accessible project scoping and inaccessible project denial.
- Present: REST entity list/get fixtures cover project-private, unassigned, inaccessible, and
  project-entity-as-scope read behavior. Core search, explore, and context-pack related hydration
  fixtures use the same project-entity-as-own-ID policy helper.
- Present: MCP tests cover accessible-project resolution, restricted credentials, remember policy,
  and reflect project context.
- Missing before release: raw-capture visibility classification, temporal search access tests, MCP
  generic `add/manage` policy tests, and job-payload policy receipt tests.

---

## 1) System Overview (As Implemented)

### Identity & tokens

- **Web**: cookie-based JWT access token (`sibyl_access_token`) + refresh token cookie
  (`sibyl_refresh_token`); refresh is “rotating” (`POST /api/auth/refresh`).
  - `apps/api/src/sibyl/api/routes/auth.py`
  - `apps/web/src/lib/api.ts` (refresh-on-401 behavior)
  - `apps/web/src/proxy.ts` (page gating by cookie presence)
- **CLI**: stores access token + refresh token in `~/.sibyl/auth.json` (0600) and uses Bearer auth.
  - `apps/cli/src/sibyl_cli/auth_store.py`
  - `apps/cli/src/sibyl_cli/client.py`
- **API keys**: `sk_*` keys stored hashed (PBKDF2) in Postgres; can be used as Bearer tokens.
  - `apps/api/src/sibyl/auth/api_keys.py`
  - `apps/api/src/sibyl/auth/dependencies.py#L68` (API key fallback)

### Tenancy & authorization layers

- **Graph tenancy**: group isolation via FalkorDB graph “database-per-org” (`group_id == org.id`).
  - `packages/python/sibyl-core/src/sibyl_core/graph/entities.py#L44-L64`
- **Org RBAC**: `OrganizationRole` (`owner/admin/member/viewer`) enforced by FastAPI dependencies.
  - `apps/api/src/sibyl/auth/dependencies.py#L131-L158`
- **Project RBAC (Postgres)**: `projects`, `project_members`, `team_projects` tables exist, and the
  project role resolution + filtering functions exist.
  - `apps/api/src/sibyl/auth/authorization.py`
  - `apps/api/alembic/versions/0005_project_permissions.py`
- **Project filtering of graph results**: `POST /api/search` and `/api/search/explore` compute
  accessible projects (from Postgres) and pass them down for filtering.
  - `apps/api/src/sibyl/api/routes/search.py#L62-L103`
  - `packages/python/sibyl-core/src/sibyl_core/tools/search.py#L390-L396`
  - `packages/python/sibyl-core/src/sibyl_core/tools/explore.py#L220-L226`

### MCP surface

- MCP is hosted at `/mcp` alongside REST at `/api/*`.
  - `apps/api/src/sibyl/main.py`
  - `apps/api/src/sibyl/server.py`
- MCP auth uses the FastMCP OAuth provider and accepts JWTs and API keys.
  - `apps/api/src/sibyl/auth/mcp_oauth.py`
  - `apps/api/src/sibyl/auth/mcp_auth.py`

---

## 2) Findings (Security/Correctness)

Severity rubric (rough): **Critical** (tenant/project isolation bypass or takeover), **High**
(unintended cross-user control, broad data exposure), **Medium** (abuse/DoS or policy drift),
**Low** (hardening/ergonomics).

### A. Critical: Project RBAC is likely non-functional in practice

**What**: Project RBAC enforcement relies on the Postgres `projects` table being populated with
`graph_project_id` rows. If _no_ projects exist in Postgres for an org,
`list_accessible_project_graph_ids()` returns `None`, and callers treat that as “skip filtering /
migration mode”.

- `apps/api/src/sibyl/auth/authorization.py#L225-L235` (returns `None` to skip filtering)
- `apps/api/src/sibyl/api/routes/search.py#L62-L85` (passes `accessible_projects` down)
- `apps/api/src/sibyl/auth/authorization.py#L481-L488` (if project not registered: allow org
  members)

**Why it matters**: Until Postgres projects are registered, project-level auth is effectively
disabled: filtering can be skipped for reads and `verify_entity_project_access()` will “allow org
members” even for project-scoped writes when the project cannot be resolved in Postgres.

**Evidence of missing wiring**:

- Graph project create/update/delete paths now synchronize Surreal auth `projects` records, but
  existing graph projects can still predate that synchronization.
- `POST /api/admin/backfill/project-records` repairs missing auth project records from graph project
  entities with a dry-run default. Apply mode creates organization-visible project records owned by
  the acting owner/admin and emits an audit event with the created graph project IDs.
- The old graph-to-Postgres sync CLI was removed after Surreal auth became canonical; project RBAC
  now stays anchored in the Surreal auth/runtime paths instead of resurrecting the mirror.

**Impact**:

- Users can likely access or mutate project-scoped entities without project membership being
  enforceable (because the project-to-Postgres mapping is missing or incomplete).

**Recommendation**:

- Keep graph project registration automatic for new create/update/archive operations.
- Use the project-record backfill before enforcing stricter gates against migrated dogfood data.
- Remove or time-bound “migration mode” fallbacks for **read** paths, or gate them behind an
  explicit feature flag once existing projects are repaired.

---

### B. Critical: Project membership graph-ID and org-membership invariant

**B2.2 update, 2026-05-13**: project-member routes now accept graph project IDs through the Surreal
organization runtime, and project membership no longer grants access unless the actor is still an
organization member. Adding or updating project members also requires the target user to belong to
the organization. Member listings filter stale `project_members` rows for users who are no longer
org members, while removal remains available so old grants can be cleaned up.

**Historical issue**: `/api/projects/{project_id}/members` and the web app needed to agree on graph
project IDs such as `project_<hash>`, while the runtime had to resolve those IDs to Surreal auth
`projects.uuid` before reading or writing `project_members`.

**Why it matters**:

- A user removed from an org must not retain access through stale `project_members` rows.
- Membership management must use graph project IDs at route boundaries and Surreal auth project
  UUIDs internally.

**Recommendation**:

- Keep route-boundary IDs as graph project IDs and resolve internally through the Surreal auth
  runtime.
- Add cleanup or cascade follow-up for stale `project_members` rows when org membership is removed.
- Keep tests covering actor org membership, target org membership, stale-row filtering, and
  stale-row removal.

---

### C. High: `verify_entity_project_access()` bypasses `required_role` in important cases

**What**:

- If an entity has **no project_id**, `verify_entity_project_access()` returns `ProjectRole.VIEWER`
  for any org member, regardless of the `required_role` passed in.
  - `apps/api/src/sibyl/auth/authorization.py#L473-L479`
- If the entity’s project is **not registered in Postgres**, it also returns `VIEWER` for any org
  member, regardless of `required_role`.
  - `apps/api/src/sibyl/auth/authorization.py#L481-L488`

**Why it matters**:

- For write endpoints that call `verify_entity_project_access(..., required_role=MAINTAINER)` (e.g.
  entity deletion), a missing/unregistered project causes the check to succeed, permitting the write
  if org-level RBAC permits it.
  - `apps/api/src/sibyl/api/routes/entities.py#L731-L735` (delete requires MAINTAINER, but
    bypassable)

**Recommendation**:

- Treat “no project_id” and “project unregistered” as a separate authorization domain:
  - Either map them to **org-level** permissions (e.g. only org admins can delete unassigned
    entities), or
  - Enforce `required_role` consistently (if required_role > viewer, deny).
- Log + metric these fallbacks; they’re security-relevant.

---

### D. High: MCP bypasses project RBAC and lacks user context

**What**: MCP tools are scoped by org only (`_require_org_id()` reads `org` claim). They do not
compute accessible projects for a user and therefore cannot filter per-project. This is a direct
side channel around project RBAC once project permissions matter.

- `apps/api/src/sibyl/server.py#L20-L71` (org-only context extraction)
- `apps/api/src/sibyl/server.py` tools call core tools with `organization_id=org_id` only.

Also: scopes default to `mcp` when absent, meaning access tokens issued for the web/REST effectively
grant MCP access unless a more explicit “audience/scope” strategy is adopted.

- `apps/api/src/sibyl/auth/mcp_auth.py#L24-L32` (default scopes -> `["mcp"]`)
- `apps/api/src/sibyl/auth/mcp_oauth.py#L76-L84` (default scopes -> `[OAUTH_SCOPE]`)
- `apps/api/src/sibyl/auth/jwt.py#L33-L63` (access tokens do not set scope by default)

**Recommendation**:

- If project RBAC is real, MCP must be able to derive **user_id + org_role** from the token and
  filter results by accessible projects (like REST does).
- Consider explicit audiences (`aud`) or explicit scopes for MCP vs web sessions.

---

### E. Medium/High: Postgres RLS is “allow-all on NULL context” and is not wired into request sessions

**What**:

- RLS policies explicitly allow access when `current_setting('app.org_id', true) IS NULL` (and same
  for `app.user_id`), which makes “no context” a bypass.
  - `apps/api/alembic/versions/0006_row_level_security.py#L64-L79`
  - The old PostgreSQL-only RLS integration test was removed when the active SQLModel/Alembic
    runtime island was retired.
- The historical app did not set `app.org_id` / `app.user_id` on its regular DB sessions. Active
  Surreal runtime paths no longer expose those PostgreSQL sessions.
- A helper exists to set session variables (`get_rls_session()`), but it is not used anywhere, and
  it claims “policies should deny by default” which is not consistent with the policy design.
  - `apps/api/src/sibyl/auth/rls.py#L144-L146`

**Impact**:

- RLS currently provides little to no tenant isolation hardening; isolation depends on application
  filters.
- Any missing org filter in SQL can become a cross-tenant read/write.

**Recommendation**:

- Decide what you want:
  - If RLS is **hardening**, remove the NULL-bypass in policies (or gate it on a privileged DB
    role).
  - If NULL-bypass is required for migrations, use a dedicated migration role or explicit bypass GUC
    only settable by superuser.
- Retired after the v0.6.0 Surreal auth cutover: relational RLS session dependencies no longer
  participate in request handling.

---

### F. Medium: Setup endpoints stay unauthenticated after setup

**What**: `/api/setup/*` endpoints have no auth and remain callable after users/orgs exist.

- `apps/api/src/sibyl/api/routes/setup.py#L1-L21` and endpoints below

Notably, `/setup/validate-keys` uses stored provider keys to call external APIs; this can be abused
for rate/usage pressure even if it doesn’t leak secrets.

**B2.3 update, 2026-05-13**: setup mode now stays open only until an owner/admin organization is
initialized, rather than keying solely off the presence of users. `/setup/validate-keys` requires
owner/admin authorization after bootstrap, and initialized unauthenticated setup calls return a
structured `setup_already_initialized` detail so the web setup flow can redirect instead of showing
a generic server failure. `/setup/status` remains public so login and setup routing can detect
first-run state, but `validate_keys=true` only triggers external key validation before setup is
complete.

**Recommendation**:

- Keep `/setup/status` public and side-effect free.
- Keep key validation and config mutation owner/admin-only after setup initialization.
- Add rate limiting to `validate-keys` if external API validation remains callable from the web UI.

---

### G. Medium: Web server-side caching may risk cross-user cache pollution

**What**: `apps/web/src/lib/api-server.ts` performs `fetch()` with cookies attached and uses Next
fetch caching strategies (`force-cache`, `revalidate`). Depending on Next.js caching semantics, this
can risk caching authenticated responses across users/orgs.

- `apps/web/src/lib/api-server.ts#L39-L66`

**Recommendation**:

- Confirm Next’s caching behavior when request headers include cookies.
- Consider `cache: 'no-store'` for any request that includes auth cookies, and cache only truly
  public endpoints.

---

### H. Low/Medium: API keys: coarse scopes; project scoping not enforced

**What**:

- REST scope gating is coarse (`api:read`/`api:write`) and only applied to API keys.
  - `apps/api/src/sibyl/auth/dependencies.py#L22-L49`
- Project scoping exists in schema (`api_key_project_scopes`) but is not enforced in request auth.
  - `apps/api/alembic/versions/0005_project_permissions.py#L161-L188`

**Recommendation**:

- Enforce `api_key_project_scopes` at auth time and incorporate it into project filtering.

---

## 3) “Good News” (What Looks Solid)

- API key hashing + verification is reasonable (PBKDF2 + constant-time compare).
  - `apps/api/src/sibyl/auth/api_keys.py`
- Refresh token rotation is implemented and rate limited.
  - `apps/api/src/sibyl/api/routes/auth.py#L1085-L1187`
- CLI token storage enforces restrictive file/dir permissions and atomic writes.
  - `apps/cli/src/sibyl_cli/auth_store.py`
- Core graph operations require explicit org context (`group_id`) to create managers.
  - `packages/python/sibyl-core/src/sibyl_core/graph/entities.py#L44-L64`

---

## 4) Suggested Remediation Plan (Prioritized)

### Phase 0: Safety fixes (fast, high impact)

1. Fix project membership routing:
   - Accept graph project IDs, not Postgres UUIDs, and resolve Postgres project row internally.
2. Automatically create/update Postgres `projects` rows when graph projects are created.
3. Add org RBAC guard (`require_org_role`) to `project_members` endpoints.
4. Remove/write-gate `verify_entity_project_access()` bypasses for write paths.
5. Gate setup endpoints after initial bootstrap.

### Phase 1: Hardening / defense-in-depth

1. Decide RLS posture:
   - enforce in app sessions + remove NULL-bypass for app DB role
2. Ensure org-scoped tables are always queried with `organization_id` filters (even with RLS).
3. Decide MCP scope/audience strategy and add user-derived project filtering.

### Phase 2: Tests + tooling

1. Add tests proving:
   - “removed org member cannot manage project members”
   - project access checks fail closed when Postgres project rows are missing
   - MCP requests do not bypass project filtering once project RBAC is enforced
2. Add an admin endpoint/job that shows whether Postgres projects are synced to graph projects.

---

## 5) Notes on Audit Process

- Sibyl server wasn’t reachable in this environment, so I couldn’t use `sibyl search` to pull prior
  knowledge graph patterns; this report is a static code audit.
