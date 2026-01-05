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

- **Organization Roles**: Admin, Member - inherited across all projects
- **Project Roles**: Owner, Admin, Writer, Reader - scoped to specific projects
- **Row-Level Security**: PostgreSQL RLS enforces data isolation

## Role Hierarchy

### Organization Roles

| Role     | Description                                  |
| -------- | -------------------------------------------- |
| `admin`  | Full organization access, can manage members |
| `member` | Standard member, access based on projects    |

Organization admins implicitly have `owner` role on all projects.

### Project Roles

| Role     | Permissions                                      |
| -------- | ------------------------------------------------ |
| `owner`  | Full access, can delete project and manage roles |
| `admin`  | Full access, can manage project members          |
| `writer` | Create, update, delete entities within project   |
| `reader` | Read-only access to project resources            |

**Role Inheritance:**

```
owner > admin > writer > reader
```

Higher roles include all lower role permissions.

## Access Control

### Project Access Check

Every request is validated against effective role:

```python
# Authorization flow
1. Resolve user from JWT/API key
2. Check organization membership
3. Calculate effective project role:
   - Org admin? → owner
   - Direct project role? → that role
   - Team membership? → highest team role
   - Public project? → reader
4. Compare against required permission
5. Allow or deny with structured 403
```

### Effective Role Calculation

Effective role is the maximum of:

1. **Org admin** → Always `owner`
2. **Direct assignment** → Role in `project_members` table
3. **Team membership** → Highest role from team memberships
4. **Public access** → `reader` if project is public

```python
from sibyl.auth.authorization import get_effective_project_role

role = await get_effective_project_role(session, user_id, project_id)
# Returns: "owner" | "admin" | "writer" | "reader" | None
```

### Permission Dependencies

| Action                  | Required Role |
| ----------------------- | ------------- |
| Read project            | `reader`      |
| Create entities         | `writer`      |
| Update entities         | `writer`      |
| Delete entities         | `writer`      |
| Manage project settings | `admin`       |
| Manage project members  | `admin`       |
| Delete project          | `owner`       |
| Transfer ownership      | `owner`       |

## API Authorization

### Dependency Functions

```python
from sibyl.auth.dependencies import (
    require_project_reader,
    require_project_writer,
    require_project_admin,
    require_project_owner,
)

@router.get("/projects/{project_id}/entities")
async def list_entities(
    project_id: str,
    _: None = Depends(require_project_reader),  # Validates access
):
    ...

@router.post("/projects/{project_id}/entities")
async def create_entity(
    project_id: str,
    _: None = Depends(require_project_writer),  # Write access required
):
    ...
```

### Error Response (403 Forbidden)

When authorization fails, a structured error is returned:

```json
{
  "error": "forbidden",
  "code": "PROJECT_ACCESS_DENIED",
  "message": "Insufficient permissions for project",
  "details": {
    "project_id": "proj_abc123",
    "required_role": "writer",
    "actual_role": "reader"
  }
}
```

**Error Codes:**

| Code                    | Description                        |
| ----------------------- | ---------------------------------- |
| `PROJECT_ACCESS_DENIED` | User lacks required project role   |
| `PROJECT_NOT_FOUND`     | Project doesn't exist or no access |
| `ORG_ACCESS_DENIED`     | User not in organization           |

## Row-Level Security (RLS)

PostgreSQL RLS provides database-level isolation.

### How It Works

1. API sets session variables before each query:

   ```sql
   SET LOCAL app.user_id = 'user-uuid';
   SET LOCAL app.org_id = 'org-uuid';
   ```

2. RLS policies filter rows automatically:

   ```sql
   -- Example policy on projects table
   CREATE POLICY org_isolation ON projects
     USING (organization_id::text = current_setting('app.org_id', true));
   ```

3. Queries only return allowed rows—no application filtering needed.

### Protected Tables

**Organization-scoped** (filtered by `app.org_id`):

- `organizations`, `organization_members`
- `projects`, `project_members`
- `teams`, `team_members`
- `api_keys`, `invitations`
- `audit_logs`

**User-scoped** (filtered by `app.user_id`):

- `user_sessions`
- `login_history`
- `oauth_connections`

### RLS in Queries

RLS is transparent to application code:

```python
# Without RLS, you'd need:
await session.execute(
    select(Project).where(Project.organization_id == org_id)
)

# With RLS, just query—policies handle filtering:
await session.execute(select(Project))  # Only returns user's org projects
```

### Bypassing RLS

For migrations and admin operations, use a session without RLS:

```python
from sibyl.db.connection import get_session

async with get_session() as session:
    # No RLS context set—sees all data
    # Use only for migrations/admin
    ...
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
User A → Team Alpha (writer) → Project X
       → Team Beta (admin)  → Project X

Result: User A has admin on Project X
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
  "role": "writer"
}
```

All team members inherit this role for the project.

## Security Considerations

### Defense in Depth

1. **Authentication** - JWT/API key validates identity
2. **Authorization** - Role checks validate permissions
3. **RLS** - Database enforces data isolation

Even if application code has bugs, RLS prevents cross-org data access.

### Audit Logging

Permission changes are logged:

```json
{
  "action": "project_member_added",
  "actor_id": "admin-user-uuid",
  "target_id": "new-member-uuid",
  "project_id": "proj-uuid",
  "role": "writer",
  "timestamp": "2025-01-04T12:00:00Z"
}
```

### Principle of Least Privilege

- Default to `reader` for new project members
- Require explicit elevation to `writer`/`admin`
- Only project creators get `owner`

## CLI Authentication

The CLI stores credentials securely:

- **Location:** `~/.sibyl/auth.json`
- **File permissions:** `0600` (user read/write only)
- **Directory permissions:** `0700` (user only)
- **Atomic writes:** Prevents credential file corruption

```bash
# Login
sibyl auth login

# Check current user
sibyl auth whoami

# Logout
sibyl auth logout
```

## Related

- [auth-jwt.md](./auth-jwt.md) - JWT session authentication
- [auth-api-keys.md](./auth-api-keys.md) - API key authentication
- [index.md](./index.md) - API overview
