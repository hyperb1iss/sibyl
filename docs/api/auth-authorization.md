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
- **Org Isolation**: each organization has its own SurrealDB namespace, so cross-org data access is
  impossible at the storage layer

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

When authorization fails, a structured error is returned:

```json
{
  "error": "forbidden",
  "code": "PROJECT_ACCESS_DENIED",
  "message": "Insufficient permissions for project",
  "details": {
    "project_id": "proj_abc123",
    "required_role": "project_contributor",
    "actual_role": "project_viewer"
  }
}
```

**Error Codes:**

| Code                    | Description                        |
| ----------------------- | ---------------------------------- |
| `PROJECT_ACCESS_DENIED` | User lacks required project role   |
| `PROJECT_NOT_FOUND`     | Project doesn't exist or no access |
| `ORG_ACCESS_DENIED`     | User not in organization           |

## Organization Isolation

Sibyl's default runtime is SurrealDB-native. Organization isolation is enforced by the storage layer
through a namespace per organization.

### Namespace-Per-Org

Each organization gets its own SurrealDB namespace, named `org_<uuid_hex>`. Graph, content, and auth
records for an organization live entirely within that namespace.

- Every authenticated request resolves an organization first, then operates inside that
  organization's namespace.
- A query issued in one namespace cannot see another organization's data. Cross-org leakage is not
  possible at the storage layer.
- The SurrealDB driver is cloned per organization (`driver.clone(group_id)`) so a single client
  instance is never shared across namespaces.

### Application Scope

Application code always carries organization context. Graph operations require an explicit
`group_id`, and there is no implicit default:

```python
from sibyl_core.graph import EntityManager

manager = EntityManager(client, group_id=str(org.id))
```

Forgetting the organization scope routes a query to the wrong namespace or fails outright, rather
than silently crossing tenants.

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
  "role": "writer"
}
```

**Required Role:** `admin`

### Update Member Role

```http
PATCH /api/projects/{project_id}/members/{member_id}
```

**Request:**

```json
{
  "role": "admin"
}
```

**Required Role:** `admin` (cannot demote/remove owners without being owner)

### Remove Member

```http
DELETE /api/projects/{project_id}/members/{member_id}
```

**Required Role:** `admin`

### List Members

```http
GET /api/projects/{project_id}/members
```

**Required Role:** `reader`

## Teams

Teams provide group-based access control.

### Team Membership

Users inherit the highest role from their team memberships:

```
User A -> Team Alpha (project_contributor) -> Project X
       -> Team Beta  (project_maintainer)  -> Project X

Result: User A has project_maintainer on Project X
```

### Creating Teams

```http
POST /api/organizations/{org_id}/teams
```

**Request:**

```json
{
  "name": "Engineering",
  "description": "Core engineering team"
}
```

### Team Project Access

```http
POST /api/teams/{team_id}/projects
```

**Request:**

```json
{
  "project_id": "proj-uuid",
  "role": "project_contributor"
}
```

All team members inherit this role for the project.

## Security Considerations

### Defense in Depth

1. **Authentication** - JWT or API key validates identity
2. **Authorization** - Role checks validate permissions
3. **Namespace isolation** - SurrealDB enforces per-org data isolation at the storage layer

Even if application code has a bug, the namespace boundary prevents cross-org data access.

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
