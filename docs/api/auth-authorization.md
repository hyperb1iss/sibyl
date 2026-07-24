# Authorization: Roles & Permissions

Access control for organizations, projects, and resources in Sibyl.

## Overview

Sibyl uses a hierarchical authorization model:

```
Organization (org-level roles)
    └── Projects (project-level roles)
            └── Resources (entities, tasks, documents)
```

**Key Concepts:**

- **Organization Roles**: `owner`, `admin`, `member`, `viewer` - inherited across all projects
- **Project Roles**: `project_owner`, `project_maintainer`, `project_contributor`,
  `project_viewer` - scoped to specific projects
- **Org Isolation**: graph memory is namespace-isolated per organization; content and auth resources
  are org-scoped in shared namespaces with table permissions and policy checks

## Role Hierarchy

### Organization Roles

| Role     | Description                                               |
| -------- | --------------------------------------------------------- |
| `owner`  | Super admin. Full org access, owner-only boundaries, logs |
| `admin`  | Full organization access, can manage members              |
| `member` | Standard member, project access based on assignments      |
| `viewer` | Read-only member                                          |

Organization owners and admins have full project access across the organization.

### Project Roles

| Role                  | Permissions                                          |
| --------------------- | ---------------------------------------------------- |
| `project_owner`       | Full access, can delete the project and manage roles |
| `project_maintainer`  | Full access, can manage project members              |
| `project_contributor` | Create, update, delete entities within the project   |
| `project_viewer`      | Read-only access to project resources                |

**Role Inheritance:**

```
project_owner > project_maintainer > project_contributor > project_viewer
```

Higher roles include all lower role permissions.

## Access Control

### Project Access Check

Every request is validated against an effective role:

```
1. Resolve user from JWT or API key
2. Check organization membership
3. Calculate the effective project role:
   - Org owner or admin? -> project_owner
   - Direct project role? -> that role
   - Team membership? -> highest team role
   - Public project? -> project_viewer
4. Compare against the required role
5. Allow, or deny with a structured 403
```

### Effective Role Calculation

The effective project role is the maximum of:

1. **Org owner or admin** - resolves to `project_owner`
2. **Direct assignment** - the role recorded for the user on the project
3. **Team membership** - the highest role from the user's team memberships
4. **Public access** - `project_viewer` if the project is public

The resolved role is then compared against the role the route requires.

### Permission Dependencies

| Action                  | Minimum Role          |
| ----------------------- | --------------------- |
| Read project            | `project_viewer`      |
| Create entities         | `project_contributor` |
| Update entities         | `project_contributor` |
| Delete entities         | `project_contributor` |
| Manage project settings | `project_maintainer`  |
| Manage project members  | `project_maintainer`  |
| Delete project          | `project_owner`       |
| Transfer ownership      | `project_owner`       |

## API Authorization

### Dependency Functions

Organization-level access is gated with `require_org_role`. Project-level access uses
`require_project_role` and its convenience shortcuts `require_project_read`,
`require_project_write`, and `require_project_admin`.

```python
from sibyl.auth.dependencies import require_org_role
from sibyl.auth.authorization import (
    require_project_read,
    require_project_write,
)
from sibyl_core.auth import OrganizationRole

@router.get("/projects/{project_id}/entities")
async def list_entities(
    project_id: str,
    _project = Depends(require_project_read()),  # Requires project_viewer or higher
):
    ...

@router.post("/projects/{project_id}/entities")
async def create_entity(
    project_id: str,
    _project = Depends(require_project_write()),  # Requires project_contributor or higher
):
    ...

@router.get("/admin/system")
async def admin_only(
    _: None = Depends(require_org_role(OrganizationRole.OWNER, OrganizationRole.ADMIN)),
):
    ...
```

`require_project_read` admits `project_viewer` and above, `require_project_write` admits
`project_contributor` and above, and `require_project_admin` admits `project_maintainer` and above.

### Error Response (403 Forbidden)

When authorization fails, the standard error envelope is returned with an `X-Request-ID` header. The
role values are surfaced under `details` as `expected` (the required role) and `actual` (the
caller's resolved role):

```json
{
  "error": "project_access_denied",
  "message": "Requires project_contributor access to project",
  "request_id": "req_a1b2c3d4e5f6",
  "remediation": "Check your project permissions or switch context.",
  "details": {
    "expected": "project_contributor",
    "actual": "project_viewer"
  }
}
```

**Error Codes:**

| Code                    | Description                        |
| ----------------------- | ---------------------------------- |
| `project_access_denied` | User lacks required project role   |
| `not_found`             | Project doesn't exist or no access |
| `forbidden`             | User not in organization           |

## Organization Isolation

Sibyl's default runtime is SurrealDB-native. Graph memory is physically isolated with a namespace
per organization. Content and auth records use shared namespaces, scoped by `organization_id`, table
permissions, and API policy checks.

### Namespace-Per-Org

Each organization gets its own SurrealDB graph namespace, named `org_<uuid_hex>`.

- Every authenticated request resolves an organization first. Graph operations route into that
  organization's namespace.
- A graph query issued in one namespace cannot see another organization's graph data. Cross-org
  graph leakage is not possible at the storage layer.
- The SurrealDB driver is cloned per organization (`driver.clone(group_id)`) so a single client
  instance is never shared across namespaces.

### Shared Runtime Namespaces

Content tables such as `raw_captures`, `document_chunks`, and import state live in the shared
`sibyl_content/content` namespace. Auth tables live in `sibyl_auth/auth`. These records are isolated
with explicit `organization_id` predicates, SurrealDB table permissions, and API authorization
checks. That is not the same as graph namespace isolation, so user-facing claims should describe
content and auth as org-scoped rather than physically namespace-isolated.

### Application Scope

Application code always carries organization context. Graph operations require an explicit
`group_id`, and there is no implicit default:

```python
from sibyl_core.services.graph import EntityManager

manager = EntityManager(client, group_id=str(org.id))
```

Forgetting the organization scope routes a graph query to the wrong namespace or fails outright.
Content and auth queries must include the resolved organization predicate so shared tables do not
cross tenants.

### PostgreSQL and Migration

PostgreSQL is retained only for migration and archive rehearsal, not for the default runtime. Where
PostgreSQL is used for rehearsal, row-level security policies provide org isolation within that
database. Migration and archive operations use explicit `sibyld migrate` commands:

```bash
sibyld migrate import migration-archive.tar.gz \
  --source-type legacy-archive \
  --target-mode postgres-rehearsal \
  --restore-database-dump \
  --yes
```

## Project Members API

### Add Member

```http
POST /api/projects/{project_id}/members
```

**Request:**

```json
{
  "user_id": "user-uuid",
  "role": "project_contributor"
}
```

The `role` defaults to `project_contributor` when omitted.

**Required Role:** `project_maintainer`

### Update Member Role

```http
PATCH /api/projects/{project_id}/members/{user_id}
```

**Request:**

```json
{
  "role": "project_maintainer"
}
```

**Required Role:** `project_maintainer` (cannot demote or remove owners without being an owner)

### Remove Member

```http
DELETE /api/projects/{project_id}/members/{user_id}
```

**Required Role:** `project_maintainer`

### List Members

```http
GET /api/projects/{project_id}/members
```

**Required Role:** `project_viewer`

## Teams

Teams are an org-level grouping that feeds the effective-role calculation. When a user belongs to
one or more teams with project access, the effective project role resolves to the highest role
granted across those memberships:

```
User A -> Team Alpha (project_contributor) -> Project X
       -> Team Beta  (project_maintainer)  -> Project X

Result: User A has project_maintainer on Project X
```

This inheritance is applied during access checks alongside direct project assignments and org-level
roles, as described in [Effective Role Calculation](#effective-role-calculation).

### Teams API

Teams are managed over REST under `/api/teams`. Every endpoint requires org `owner` or `admin` role.
Creating a team also creates its canonical team memory space; deleting a team removes the
control-plane record and its memory-space binding.

| Method | Path                                         | Purpose                              |
| ------ | -------------------------------------------- | ------------------------------------ |
| GET    | `/api/teams`                                 | List teams in the organization       |
| POST   | `/api/teams`                                 | Create a team (and its memory space) |
| GET    | `/api/teams/{team_id}`                       | Inspect a team, members, and links   |
| PATCH  | `/api/teams/{team_id}`                       | Update team metadata                 |
| DELETE | `/api/teams/{team_id}`                       | Delete a team                        |
| POST   | `/api/teams/{team_id}/members`               | Add or update a team member          |
| DELETE | `/api/teams/{team_id}/members/{user_id}`     | Remove a team member                 |
| POST   | `/api/teams/{team_id}/projects`              | Grant the team a project role        |
| DELETE | `/api/teams/{team_id}/projects/{project_id}` | Remove the team's project grant      |

Member roles use the organization role values; project links carry a project role (default
`project_contributor`).

## OIDC and Enterprise SSO

Organizations can authenticate through corporate OIDC providers instead of (or alongside) local auth
and GitHub OAuth. Each provider is bound to exactly one organization, and the IdP role claim is
authoritative for that organization's roles on every OIDC login - including demoting the last owner
if the claim says so. Configuration and role-mapping details live in the admin guides:

- [Installing Sibyl](../admin/installing.md) - OIDC provider setup and `SIBYL_OIDC` configuration
- [Inviting Users](../admin/inviting-users.md) - JIT provisioning, IdP role claims, deprovisioning

The OIDC login endpoints are described in [auth-jwt.md](./auth-jwt.md#oidc-enterprise-sso).

## Security Considerations

### Defense in Depth

1. **Authentication** - JWT or API key validates identity
2. **Authorization** - Role checks validate permissions
3. **Graph namespace isolation** - SurrealDB enforces per-org graph isolation at the storage layer
4. **Shared-table scoping** - content and auth queries carry organization predicates, table
   permissions, and API policy checks

Even if application code has a bug, the graph namespace boundary prevents cross-org graph access.
Shared content and auth paths must preserve scoped predicates and table permissions to maintain the
same tenant boundary.

### Audit Logging

Permission changes are logged:

```json
{
  "action": "project_member_added",
  "actor_id": "admin-user-uuid",
  "target_id": "new-member-uuid",
  "project_id": "proj-uuid",
  "role": "project_contributor",
  "timestamp": "2026-05-16T12:00:00Z"
}
```

### Principle of Least Privilege

- Default to `project_viewer` for new project members
- Require explicit elevation to `project_contributor` or `project_maintainer`
- Only project creators get `project_owner`

## CLI Authentication

The CLI stores credentials securely:

- **Location:** `~/.sibyl/auth.json`
- **File permissions:** `0600` (user read/write only)
- **Directory permissions:** `0700` (user only)
- **Atomic writes:** Prevents credential file corruption

```bash
# Login
sibyl auth login

# Check auth status
sibyl auth status

# Clear stored credentials
sibyl auth clear-token
```

## Related

- [auth-jwt.md](./auth-jwt.md) - JWT session authentication
- [auth-api-keys.md](./auth-api-keys.md) - API key authentication
- [index.md](./index.md) - API overview
