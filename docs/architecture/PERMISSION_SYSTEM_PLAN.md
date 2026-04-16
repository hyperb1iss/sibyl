# Multi-Tier Permission System (Org + Project): Code-Audited Plan

This is the updated, code-audited permission plan for Sibyl. It reconciles the desired model with
what already exists in the schema and runtime, and enumerates the minimal missing pieces needed to
ship secure project-level authorization without over-engineering.

---

## Executive Summary

Sibyl currently enforces **org-level RBAC** across REST endpoints and scopes graph access by org
(`group_id == org.id`). The Postgres schema for **project-level RBAC** does not exist yet and needs
to be created. This plan covers both schema creation and enforcement wiring.

This plan:

- Creates the Postgres permission tables (`projects`, `project_members`, `team_projects`).
- Implements **app-level** project authorization everywhere (REST + MCP + graph filtering).
- Treats Postgres RLS as **optional hardening** (Phase 3) after core enforcement is proven.

---

## 0. Current State (As Implemented Today)

### 0.1 Auth & Tenancy (Works Today)

- **JWT org scoping**: JWT contains `org`; graph ops must use that as `group_id`.
  - `apps/api/src/sibyl/auth/tenancy.py`
  - `packages/python/sibyl-core/src/sibyl_core/graph/client.py`
- **Org RBAC**: FastAPI dependencies enforce `OrganizationRole` for many routes.
  - `apps/api/src/sibyl/auth/dependencies.py`
  - routes under `apps/api/src/sibyl/api/routes/`
- **API key scope gating (coarse)**: REST requests via API keys require `api:read`/`api:write` (in
  addition to org scoping), but scopes are not fine-grained per resource.
  - `apps/api/src/sibyl/auth/dependencies.py`

### 0.2 Authorization Gaps (Security-Relevant)

- **Project RBAC is not enforced**. Any org member can currently:
  - read/list tasks/agents/approvals across all “projects” within the org
  - mutate project-scoped entities as long as they meet org-level role checks
- **Projects in the UI are graph entities**. Web lists projects via `POST /api/search/explore` and
  `GET /api/entities/{id}` (not `/api/projects`).
  - `apps/web/src/lib/api.ts` (projects via explore)
  - `apps/api/src/sibyl/api/routes/search.py` (explore delegates into `sibyl_core`)
- **WebSocket is org-scoped only** (connections carry `org_id`, not `user_id`), so per-user
  invalidation is not currently possible without extending the protocol.
  - `apps/api/src/sibyl/api/websocket.py`

---

## 1. Authorization Model (Target)

### 1.1 Org Roles (Already Implemented)

Enum: `OrganizationRole` (`owner`, `admin`, `member`, `viewer`).

### 1.2 Project Roles (Already in Schema)

Enum: `ProjectRole`:

- `project_owner`
- `project_maintainer`
- `project_contributor`
- `project_viewer`

### 1.3 Project Visibility (Already in Schema)

Enum: `ProjectVisibility`:

- `private`: only explicit grants (plus org owner/admin override)
- `project`: explicit grants (direct or via teams) (plus org owner/admin override)
- `org`: all org members get a default role (plus explicit grants can elevate)

### 1.4 Inheritance Rules

- Org `owner`/`admin` ⇒ implicit `project_owner` for all projects in the org.
- Org `member`/`viewer` ⇒ access determined by project visibility and explicit membership/team
  grants.

---

## 2. Data Model (Needs Creation)

### 2.1 Source of Truth

Models will live in `apps/api/src/sibyl/db/models.py`. Key permission tables to add:

- `projects` (canonical project record; links to graph via `graph_project_id`)
- `project_members` (direct user membership)
- `team_projects` (team → project grants)
- `api_key_project_scopes` (optional: restrict an API key to a subset of projects)

Optional tables for later phases:

- `global_admins` (platform-wide admins; global roles are _not_ stored on `users`)
- `permission_audit_log` (audit trail for permission changes)
- `ownership_transfer_requests` (org/project ownership transfer workflow)

### 2.2 Migrations & RLS

**Current migrations:** 0001-0004 (initial schema, api_keys, agent_messages, tool_tracking)

**Need to create:**

- `apps/api/alembic/versions/0005_project_permissions.py` (tables + enums + indexes)
- `apps/api/alembic/versions/0006_enable_rls_policies.py` (optional: enables RLS + policies)

Important: RLS requires setting Postgres session variables per request:

- `app.user_id`
- `app.org_id`

RLS is Phase 3 (optional hardening) - no middleware sets these variables yet.

---

## 3. ID Mapping: Graph ↔ Postgres

This is the most important “glue” detail for correctness:

- REST/MCP and graph use **graph entity IDs** (strings), e.g. deterministic `project_<hash>`.
  - `packages/python/sibyl-core/src/sibyl_core/tools/add.py` generates deterministic project ids.
- Postgres uses a UUID primary key `projects.id` and stores the graph id in
  `projects.graph_project_id`.

### Rule

All authorization checks for a project referenced by REST/MCP must start by resolving:

`graph_project_id (string)` → `Project row (UUID + org_id + visibility + owner_user_id + defaults)`

This keeps external APIs stable while using Postgres as the permission source of truth.

---

## 4. Backend Enforcement Plan (Minimal, Secure, Not Over-Engineered)

### 4.1 New Authorization Module (Add This)

Create `apps/api/src/sibyl/auth/authorization.py` with:

- `resolve_project_by_graph_id(session, org_id, graph_project_id) -> Project | 404/403`
- `get_effective_project_role(session, ctx, project) -> ProjectRole | None`
  - considers org role override, project owner, direct membership, team grants, visibility defaults
- FastAPI dependencies:
  - `require_project_read(project_id_param="project_id")`
  - `require_project_write(project_id_param="project_id")`
  - `require_project_admin(project_id_param="project_id")` (owner/maintainer)
- Optional: `list_accessible_project_graph_ids(session, ctx) -> set[str]`

Do not add Redis permission caching in v1. These are indexed Postgres lookups.

### 4.2 Enforce Project RBAC Everywhere It Matters

Project RBAC must apply to:

- REST endpoints that accept `project_id` (graph id) directly:
  - agent spawn/list filters
  - approvals filters
  - task creation (requires project)
- REST endpoints that _resolve_ a project by entity id:
  - actions on tasks/epics/agents/approvals: resolve entity → project_id → check access
- Search/explore endpoints (critical):
  - `POST /api/search` and `POST /api/search/explore` must not leak project-scoped entities.

### 4.3 Graph Filtering Strategy (Two-Stage, Minimal)

FalkorDB cannot enforce Postgres RLS, so we must filter in the app:

1. Determine allowed projects for the caller (set of `graph_project_id`).
2. Filter results for project-scoped entity types where `project_id` exists:
   - task, epic, agent, approval, note (and any other entity that has `project_id`)

If a request explicitly targets a single project (e.g. `project=...`), check access once and then
allow the graph query to proceed scoped to that project.

---

## 5. Critical Prerequisites / Code Gaps to Fix (Before RBAC)

### 5.1 Ensure Structured Graph Properties Are Persisted for Direct Inserts

Many core graph queries filter on `n.project_id` (not just metadata). However,
`EntityManager.create_direct()` currently writes a `metadata` JSON but does not persist the
normalized properties the same way `EntityManager.create()` does (which calls
`_persist_entity_attributes()`).

If we plan to rely on graph filtering by `project_id`, we must align `create_direct()` to persist
structured fields (especially `project_id`) consistently.

Relevant code:

- `packages/python/sibyl-core/src/sibyl_core/graph/entities.py`

### 5.2 Actor Attribution for Auditability

Populate `created_by` / `modified_by` (graph metadata + node properties) in write paths:

- entity create/update/delete
- task/epic/agent lifecycle operations

This is important for:

- debugging and forensics
- future “private project” semantics if we ever use creator-based rules in the graph

---

## 6. API Keys (Least Privilege)

### 6.1 Existing Behavior

- API keys are stored hashed; scopes exist (`api_keys.scopes`) and REST scope gating is enforced in
  `apps/api/src/sibyl/auth/dependencies.py`.

### 6.2 Missing Wiring: Project Scoping

Schema provides `api_key_project_scopes` (project-level allowlist), but runtime does not enforce it.

Plan:

- When authenticating an API key, resolve any allowed projects:
  - if the key has entries in `api_key_project_scopes`, restrict access to those projects only
  - otherwise, fall back to normal RBAC (org role + project membership)

Do not add granular resource scopes (“tasks:write”, etc.) until project RBAC is fully enforced and
stable.

---

## 7. WebSocket Invalidation (Keep It Simple)

Current WS is org-scoped only. Implement one org-scoped event:

- `permission_changed` `{ project_graph_id?: string, reason?: string }`

Emit it when:

- project visibility changes
- project membership changes
- team grants change

Frontend should respond by invalidating cached queries (React Query already used). No per-user WS
targeting needed for v1.

---

## 8. Rollout Plan (Phased)

### Phase 0: Schema Creation

- Add `ProjectRole`, `ProjectVisibility` enums to models.py.
- Add `Project`, `ProjectMember`, `TeamProject`, `ApiKeyProjectScope` models.
- Create Alembic migration `0005_project_permissions.py`.
- Fix `create_direct()` to persist structured properties like `project_id`.
- Add actor attribution (`created_by`/`modified_by`) to entity writes.

### Phase 1: Wire App-Level Authorization

- Implement `auth/authorization.py` and dependencies.
- Add project filtering to REST + MCP entrypoints.
- Add project backfill/sync logic:
  - on-demand: ensure `projects` row exists for a graph project id
  - one-off: backfill all graph projects into Postgres

### Phase 2: Harden + UX

- Add structured 403 payloads (error code + required role) to improve web/CLI UX.
- Add `permission_changed` event and query invalidation.

### Phase 3 (Optional): Enable Postgres RLS

- Add middleware/dependency that sets `app.user_id` + `app.org_id` for each DB session/transaction.
- Create and enable `0006` RLS policies only after tests confirm no breaking behavior.

---

## 9. Success Criteria (Realistic)

- No private project data leaks through list/search/explore endpoints.
- Mutations require appropriate project role even if the user is a valid org member.
- Minimal API surface changes: keep graph project IDs as external identifiers for now.
- No Redis permission caching until proven necessary.

---

## 10. References (Code + Docs)

- Schema:
  - `apps/api/src/sibyl/db/models.py`
- Migrations:
  - `apps/api/alembic/versions/0005_project_permissions.py`
  - `apps/api/alembic/versions/0006_enable_rls_policies.py`
- Existing spec:
  - `docs/_archive/security/ORG_PROJECT_PERMISSIONS_SPEC.md`
- Current org RBAC dependency:
  - `apps/api/src/sibyl/auth/dependencies.py`

---

## Appendix: Open Questions (Resolve Before Implementation)

1. Should org `admin` have implicit override on _private_ projects? (RLS helper function implies
   yes.)
2. For API keys, should the default be “all accessible projects” or “must be explicitly scoped”?
3. Do we want to introduce stable REST endpoints for Postgres-backed projects (`/api/projects`) or
   keep projects purely graph-addressed (`project_<hash>`) for now?
