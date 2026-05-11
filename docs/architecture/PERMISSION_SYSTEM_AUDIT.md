# Sibyl Permission System Audit (API/CLI/Web/Core)

Date: 2026-01-04

Scope: `apps/api`, `apps/cli`, `apps/web`, `packages/python/sibyl-core`

This is a code-audit report of Sibyl’s authentication/authorization/multi-tenancy system, with an
emphasis on security properties, correctness gaps, and operational/performance risks.

Update (2026-03-29): Sibyl's internal agent runtime, approval flow, and sandbox control plane were
removed after this audit was written. The findings below have been trimmed so the document reflects
the remaining live surfaces.

## TL;DR (Highest-Risk Items)

1. **Project RBAC appears effectively disabled by default** because Postgres `projects` rows are not
   automatically created for graph projects; the auth code explicitly falls back to “allow org
   members” when projects are not registered.
2. **`/api/projects/{project_id}/members` uses Postgres project UUIDs**, but the rest of the system
   (web + graph) uses graph project IDs like `project_<hash>`. The web calls this endpoint with
   graph IDs, which will 422. This blocks project membership management and undermines project RBAC.
3. **`verify_entity_project_access()` can bypass required project roles** when entities are missing
   project metadata or the backing Postgres project row.
4. **Postgres RLS policies are intentionally “ALLOW ALL when context is NULL”** and the app does not
   set RLS session variables anywhere in the request DB session. This means RLS is currently not
   providing defense-in-depth for tenant isolation.

If you want one “first security pass” patch list: fix project registration + fix project member
endpoints (graph ID vs UUID) + remove the project access bypasses + decide what RLS is supposed to
do.

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

- Projects are created in the graph with deterministic IDs (`project_<hash>`), but **no automatic
  creation of the Postgres `projects` row is done** on project creation.
  - `packages/python/sibyl-core/src/sibyl_core/tools/add.py#L165-L177` (project IDs are graph IDs)
  - `apps/api/src/sibyl/api/routes/entities.py#L438-L559` (creates graph entity, no Postgres sync)
- The only sync path found is a CLI admin utility, not a REST flow.
  - `apps/api/src/sibyl/db/sync.py`
  - `apps/api/src/sibyl/cli/db.py` (calls `sync_projects_from_graph()`)

**Impact**:

- Users can likely access or mutate project-scoped entities without project membership being
  enforceable (because the project-to-Postgres mapping is missing or incomplete).

**Recommendation**:

- Make project registration automatic (create/update Postgres `projects` row when a graph project is
  created/renamed/archived), or make project RBAC depend on a single canonical store.
- Remove or time-bound “migration mode” fallbacks for **write** paths, or gate them behind an
  explicit feature flag.

---

### B. Critical: Project membership endpoint uses the wrong identifier + missing org RBAC invariant

**What**: `/api/projects/{project_id}/members` expects `project_id: UUID` (Postgres project primary
key), but the web app calls it using graph project IDs (e.g. `project_<hash>`).

- API expects UUID:
  - `apps/api/src/sibyl/api/routes/project_members.py#L70-L76`
- Web calls it with graph project IDs from explore/entities:
  - `apps/web/src/lib/api.ts#L1787-L1820` (projects via explore; members endpoint uses same
    `projectId`)
  - `packages/python/sibyl-core/src/sibyl_core/tools/add.py#L165-L177` (graph project IDs)

**Why it matters**:

- The membership management UI/API path likely does not work (422 validation errors).
- Without working membership management, project RBAC cannot be configured and will remain in the
  “migration/no-projects” fallback mode described above.

**Second issue**: the route only checks that the project belongs to the org in the token; it **does
not require org membership** (`OrganizationMember`) and does not use `require_org_role()`.

- `apps/api/src/sibyl/api/routes/project_members.py#L70-L76` (no `require_org_role` dependency)
- `_get_project_and_user_role()` checks `ProjectMember` but does not check `OrganizationMember`.
  - `apps/api/src/sibyl/api/routes/project_members.py#L30-L58`

**Impact**:

- Even after fixing the ID mismatch, a user removed from an org could potentially retain project
  access if `ProjectMember` rows remain (org removal does not cascade project membership removal).
- This also violates a strong invariant: “project membership implies org membership”.

**Recommendation**:

- Change the route to accept **graph project IDs** (the canonical ID used by tasks, epics, and graph
  entities), and resolve Postgres projects internally (like `resolve_project_by_graph_id()` does).
- Enforce org membership (e.g., `dependencies=[Depends(require_org_role(...))]`) and/or enforce a DB
  invariant (foreign keys / triggers / service-layer cleanup) so removing an org member removes all
  project memberships and revokes sessions.

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
  - RLS tests explicitly assert this behavior:
    - `apps/api/tests/test_rls_policies.py#L75-L86`
- The app does not set `app.org_id` / `app.user_id` on its regular DB sessions
  (`get_session_dependency()`).
  - `apps/api/src/sibyl/db/connection.py#L109-L119`
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

**Recommendation**:

- Once `has_users` is true, gate these endpoints behind admin-only, or disable them entirely.
- Add rate limiting to `validate-keys` and consider removing outbound calls from unauth endpoints.

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
