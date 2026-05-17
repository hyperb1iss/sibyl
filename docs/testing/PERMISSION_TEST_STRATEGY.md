# Permission System Test Strategy

Testing strategy for Sibyl's multi-tier permission system.

## Permission Tiers Overview

Sibyl scopes access at three levels: organization, project, and memory. Org and project each have an
ordered role enum; project visibility controls who can see a project at all.

```
Organization role (sibyl_core.auth.OrganizationRole)
    |-- owner   (super admin within the org)
    |-- admin
    |-- member
    |-- viewer

Project role (sibyl_core.auth.ProjectRole)
    |-- project_owner
    |-- project_maintainer
    |-- project_contributor
    |-- project_viewer

Project visibility (sibyl_core.auth.ProjectVisibility)
    |-- private  (creator/explicit grants only)
    |-- project  (project members)
    |-- org      (every org member)
```

Memory access adds a fourth axis through `MemoryScope` (private vs project) plus API-key memory
scopes. Teams exist as `Team` entities with a `members` list; there is no separate team-role enum.
OWNER is the cross-cutting super-admin role surfaced through `AuthContext`.

## Test Layout

Auth and permission tests live as flat `test_auth_*.py` modules in `apps/api/tests/`. Shared
fixtures and factories are in `apps/api/tests/conftest.py`. Run the whole auth suite with a name
filter:

```bash
moon run api:test -- -k auth
```

| File                               | Focus                                          |
| ---------------------------------- | ---------------------------------------------- |
| `test_auth_authorization.py`       | Project-role hierarchy and `require_project_*` |
| `test_auth_context.py`             | `AuthContext` construction and role resolution |
| `test_auth_dependencies.py`        | FastAPI auth dependencies and guards           |
| `test_auth_tenancy.py`             | Org isolation and namespace-per-org scoping    |
| `test_auth_rls.py`                 | Row-level access enforcement                   |
| `test_auth_api_keys.py`            | API key issue/verify lifecycle                 |
| `test_auth_api_key_scopes_rest.py` | API-key scope enforcement on REST endpoints    |
| `test_auth_jwt.py`                 | JWT issue, verify, and expiry                  |
| `test_auth_mcp_token_verifier.py`  | MCP bearer-token verification                  |
| `test_auth_invitation_token.py`    | Org invitation token boundaries                |
| `test_auth_signup_locks.py`        | Signup race and lock handling                  |
| `test_auth_session_cache.py`       | Session cache behavior and invalidation        |
| `test_auth_errors.py`              | Auth error mapping and safe responses          |
| `test_auth_http.py`                | End-to-end auth over the HTTP app              |
| `test_auth_flow_replay.py`         | Recorded auth-flow replay checks               |

## Test Categories

### 1. Project Authorization Logic

`test_auth_authorization.py` covers the project-role engine in `sibyl.auth.authorization`:

- `PROJECT_ROLE_LEVELS` ordering (`viewer < contributor < maintainer < owner`)
- `_max_role` resolution across multiple grants
- `require_project_read`, `require_project_write`, `require_project_admin`, `require_project_role`
- `list_accessible_project_graph_ids` for visibility-filtered listing

Example:

```python
def test_role_levels_order() -> None:
    assert PROJECT_ROLE_LEVELS[ProjectRole.VIEWER] < PROJECT_ROLE_LEVELS[ProjectRole.CONTRIBUTOR]
    assert PROJECT_ROLE_LEVELS[ProjectRole.MAINTAINER] < PROJECT_ROLE_LEVELS[ProjectRole.OWNER]
```

Run:

```bash
moon run api:test -- tests/test_auth_authorization.py
```

### 2. Organization Role Enforcement

Org-role checks run through `AuthContext`. `test_auth_context.py` exercises context construction and
role resolution; `test_auth_dependencies.py` covers the FastAPI guards that gate endpoints.

Owner-only operations include deleting the org, transferring ownership, and managing invitations.
Admins manage members and roles but cannot delete the org.

| Operation              | Owner | Admin | Member | Viewer |
| ---------------------- | ----- | ----- | ------ | ------ |
| Delete organization    | yes   | no    | no     | no     |
| Transfer ownership     | yes   | no    | no     | no     |
| Add/remove members     | yes   | yes   | no     | no     |
| Create tasks/knowledge | yes   | yes   | yes    | no     |
| Read tasks/knowledge   | yes   | yes   | yes    | yes    |

Run:

```bash
moon run api:test -- tests/test_auth_context.py tests/test_auth_dependencies.py
```

### 3. Tenancy and Row-Level Isolation

`test_auth_tenancy.py` and `test_auth_rls.py` verify that org context is mandatory and that one org
cannot read another's data. Each org maps to its own SurrealDB namespace (`org_<uuid_hex>`), so a
missing or wrong `group_id` must fail rather than leak.

Coverage:

- Cross-org reads denied
- Queries without org context rejected
- Namespace routing matches the authenticated org

Run:

```bash
moon run api:test -- tests/test_auth_tenancy.py tests/test_auth_rls.py
```

### 4. API Key Scopes

`test_auth_api_keys.py` covers the key lifecycle; `test_auth_api_key_scopes_rest.py` asserts scope
enforcement on REST endpoints. Scopes include `mcp`, `api:read`, `api:write`, and the memory scopes.
A read-scoped key must be rejected on write endpoints.

Run:

```bash
moon run api:test -- tests/test_auth_api_key_scopes_rest.py
```

### 5. Token Verification

JWT sessions (`test_auth_jwt.py`) back the web UI; MCP bearer tokens are verified by
`test_auth_mcp_token_verifier.py`. Both cover issue, verify, expiry, and tampering.

Run:

```bash
moon run api:test -- tests/test_auth_jwt.py tests/test_auth_mcp_token_verifier.py
```

### 6. End-to-End Tests

The `apps/e2e` package holds cross-surface tests, organized by surface:

- `apps/e2e/tests/api/` covers REST endpoint and health checks
- `apps/e2e/tests/browser/` covers Playwright UI smoke flows
- `apps/e2e/tests/cli/` covers CLI runner checks

Run:

```bash
moon run e2e:test
```

UI-level role assertions (which controls a role can see) belong in `apps/e2e/tests/browser/`. When
adding role-gated UI, extend the browser suite rather than the API auth modules.

## Test Fixtures

`apps/api/tests/conftest.py` provides the shared fixtures and factories. Inspect it directly for
current signatures before writing new tests, since fixture names evolve. Typical usage builds a
user, attaches it to an org with a role, and issues an authenticated client or context for that
combination. Cross-org tests must create genuinely separate orgs rather than reusing a fixture.

## Running Tests

```bash
# Whole auth suite by name filter
moon run api:test -- -k auth

# A single module
moon run api:test -- tests/test_auth_authorization.py

# Coverage scoped to the auth package
moon run api:test -- -k auth --cov=sibyl.auth --cov-report=html

# End-to-end suite
moon run e2e:test
```

## CI Integration

The repository CI runs the package suites through moon. A focused permission job looks like:

```yaml
permission-tests:
  runs-on: ubuntu-latest
  steps:
    - uses: actions/checkout@v4
    - name: Start SurrealDB
      run: docker compose up -d surrealdb
    - name: Run auth tests
      run: moon run api:test -- -k auth -v
```

## Common Gotchas

1. **Org context is mandatory.** Graph operations need a `group_id`; a missing one routes to the
   wrong namespace or fails. Never assume a default org in tests.
2. **Project roles are not visibility.** `ProjectRole` (owner/maintainer/contributor/viewer) governs
   what a member can do; `ProjectVisibility` (private/project/org) governs who is a member at all.
3. **Cross-org tests need separate orgs.** Reusing a single org fixture cannot prove isolation.
4. **OWNER is cross-cutting.** The super-admin OWNER role bypasses many org-role checks; assert it
   explicitly rather than folding it into org-admin cases.
5. **Update matrices when adding permissions.** New gated operations should extend the role tables
   in this document and the relevant `test_auth_*.py` module together.

## Future Enhancements

- Dedicated WebSocket permission-invalidation tests (role change broadcasts, cache timing)
- Team-grant tests (team membership contributing project access)
- Project-visibility matrix tests across private, project, and org scopes
- Permission audit-logging coverage
- A global-admin role tier above the organization
