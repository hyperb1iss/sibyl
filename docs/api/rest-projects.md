# REST API: Projects

Project management via the unified entities endpoint.

## Overview

Projects are managed through the `/api/entities` endpoint with `entity_type: "project"`. Projects
serve as containers for epics and tasks.

**Note:** Projects use the general entities API. See [rest-entities.md](./rest-entities.md) for full
endpoint documentation.

## Project Model

```typescript
interface Project {
  id: string;
  entity_type: "project";
  name: string; // Project title
  description: string; // Project description
  content: string; // Extended content/documentation
  category?: string; // Optional category
  languages: string[]; // Programming languages (tech stack)
  tags: string[]; // Searchable tags
  metadata: {
    status: ProjectStatus;
    repository_url?: string;
    tech_stack?: string[];
    created_at: string;
    updated_at: string;
  };
}
```

### Project Status Values

| Status      | Description                |
| ----------- | -------------------------- |
| `active`    | Currently active (default) |
| `on_hold`   | Paused                     |
| `completed` | Finished                   |
| `archived`  | Archived                   |

## Endpoints

### List Projects

```http
GET /api/entities?entity_type=project
```

**Query Parameters:**

| Parameter     | Type    | Default      | Description                |
| ------------- | ------- | ------------ | -------------------------- |
| `entity_type` | string  | -            | Set to `project`           |
| `search`      | string  | -            | Search in name/description |
| `page`        | integer | 1            | Page number                |
| `page_size`   | integer | 50           | Items per page             |
| `sort_by`     | string  | `updated_at` | Sort field                 |
| `sort_order`  | string  | `desc`       | Sort direction             |

**Example Request:**

```bash
curl -X GET "https://api.example.com/api/entities?entity_type=project" \
  -H "Authorization: Bearer $TOKEN"
```

**Response:**

```json
{
  "entities": [
    {
      "id": "proj_abc123",
      "entity_type": "project",
      "name": "Auth System",
      "description": "Authentication and authorization services",
      "content": "Complete auth system including JWT, OAuth, RBAC...",
      "category": "backend",
      "languages": ["python", "typescript"],
      "tags": ["security", "api"],
      "metadata": {
        "status": "active",
        "repository_url": "https://github.com/org/auth-system",
        "tech_stack": ["fastapi", "postgresql", "redis"]
      },
      "created_at": "2024-12-01T10:00:00Z",
      "updated_at": "2024-12-30T15:30:00Z"
    }
  ],
  "total": 5,
  "page": 1,
  "page_size": 50,
  "has_more": false
}
```

### Get Project

```http
GET /api/entities/{project_id}
```

**Example Request:**

```bash
curl -X GET "https://api.example.com/api/entities/proj_abc123" \
  -H "Authorization: Bearer $TOKEN"
```

### Create Project

```http
POST /api/entities
```

Projects are always created synchronously (blocking) to ensure they exist before tasks are added.

**Request Body:**

```json
{
  "name": "E-Commerce API",
  "description": "Backend services for e-commerce platform",
  "content": "## E-Commerce API\n\nCore backend services including:\n- Product catalog\n- Shopping cart\n- Order management\n- Payment processing",
  "entity_type": "project",
  "category": "backend",
  "languages": ["python", "go"],
  "tags": ["api", "e-commerce"],
  "metadata": {
    "repository_url": "https://github.com/org/ecommerce-api",
    "tech_stack": ["fastapi", "postgresql", "stripe"]
  }
}
```

**Response (201 Created):**

```json
{
  "id": "proj_xyz789",
  "entity_type": "project",
  "name": "E-Commerce API",
  "description": "Backend services for e-commerce platform",
  "content": "## E-Commerce API\n\nCore backend services...",
  "category": "backend",
  "languages": ["python", "go"],
  "tags": ["api", "e-commerce"],
  "metadata": {
    "status": "active",
    "repository_url": "https://github.com/org/ecommerce-api"
  },
  "created_at": "2024-12-30T10:00:00Z",
  "updated_at": "2024-12-30T10:00:00Z"
}
```

### Update Project

```http
PATCH /api/entities/{project_id}
```

**Request Body:**

```json
{
  "description": "Updated description",
  "tags": ["api", "e-commerce", "microservices"],
  "metadata": {
    "status": "active",
    "team_size": 5
  }
}
```

### Archive Project

To archive a project, update its status:

```http
PATCH /api/entities/{project_id}
```

**Request Body:**

```json
{
  "metadata": {
    "status": "archived"
  }
}
```

### Delete Project

```http
DELETE /api/entities/{project_id}
```

**Warning:** Deleting a project does not cascade to tasks/epics. Orphaned tasks may need cleanup.

## Project Tasks via Explore

Use the explore endpoint to list tasks within a project:

### List Project Tasks

```http
POST /api/search/explore
```

**Request Body:**

```json
{
  "mode": "list",
  "types": ["task"],
  "project": "proj_abc123",
  "status": "todo,doing"
}
```

### List Project Epics

```http
POST /api/search/explore
```

**Request Body:**

```json
{
  "mode": "list",
  "types": ["epic"],
  "project": "proj_abc123"
}
```

## Project Metrics

Get project-level metrics:

```http
GET /api/metrics/projects/{project_id}
```

**Response:**

```json
{
  "metrics": {
    "project_id": "proj_abc123",
    "project_name": "Auth System",
    "total_tasks": 42,
    "status_distribution": {
      "backlog": 5,
      "todo": 12,
      "doing": 8,
      "blocked": 2,
      "review": 3,
      "done": 12
    },
    "priority_distribution": {
      "critical": 2,
      "high": 10,
      "medium": 20,
      "low": 8,
      "someday": 2
    },
    "completion_rate": 28.6,
    "assignees": [{ "name": "alice", "total": 15, "completed": 5, "in_progress": 3 }],
    "tasks_created_last_7d": 8,
    "tasks_completed_last_7d": 5,
    "velocity_trend": [
      { "date": "2024-12-24", "value": 2 },
      { "date": "2024-12-25", "value": 1 }
    ]
  }
}
```

## MCP Equivalent

### Create Project via MCP

```json
{
  "name": "add",
  "arguments": {
    "title": "E-Commerce API",
    "content": "Backend services for e-commerce platform",
    "entity_type": "project",
    "repository_url": "https://github.com/org/ecommerce-api",
    "technologies": ["python", "go"]
  }
}
```

### List Projects via MCP

```json
{
  "name": "explore",
  "arguments": {
    "mode": "list",
    "types": ["project"]
  }
}
```

## Audit Logging

Project operations are logged for compliance:

- `project.create` - Project creation
- `project.update` - Project updates
- `project.delete` - Project deletion

Audit logs include:

- User ID
- Organization ID
- Timestamp
- Project details
- Request metadata

## Best Practices

### Project Naming

Use clear, descriptive names:

```
"Auth System"           // Good
"auth"                  // Too vague
"Project 1"             // Not descriptive
```

### Repository Links

Always include repository URL when available:

```json
{
  "metadata": {
    "repository_url": "https://github.com/org/project"
  }
}
```

### Tech Stack Documentation

Document the technology stack:

```json
{
  "languages": ["python", "typescript"],
  "metadata": {
    "tech_stack": ["fastapi", "react", "postgresql", "redis"]
  }
}
```

## Related

- [rest-entities.md](./rest-entities.md) - General entity operations
- [rest-tasks.md](./rest-tasks.md) - Task management
- [mcp-add.md](./mcp-add.md) - Create projects via MCP
- [mcp-explore.md](./mcp-explore.md) - Browse projects via MCP
