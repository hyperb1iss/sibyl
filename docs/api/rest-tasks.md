# REST API: Tasks

Task lifecycle management with workflow state machine and notes support.

## Overview

The tasks endpoint provides:

- Task CRUD operations
- Workflow state transitions (start, block, review, complete)
- Task notes for collaboration
- Proper state machine validation

**Base URL:** `/api/tasks`

## Authentication

All endpoints require authentication via:

- JWT access token (cookie or Authorization header)
- API key with `api:write` scope

## Role Requirements

All task operations require: Owner, Admin, or Member role.

## Task Model

```typescript
interface Task {
  id: string;
  title: string;
  description: string;
  status: TaskStatus;
  priority: TaskPriority;
  complexity: TaskComplexity;
  project_id: string;
  epic_id?: string;
  assignees: string[];
  feature?: string;
  tags: string[];
  technologies: string[];
  branch_name?: string;
  pr_url?: string;
  sprint?: string;
  due_date?: string;
  estimated_hours?: number;
  actual_hours?: number;
  domain?: string;
  task_order?: number;
  created_at: string;
  updated_at: string;
}
```

### Status Values

| Status     | Description           |
| ---------- | --------------------- |
| `backlog`  | Not yet prioritized   |
| `todo`     | Ready to work on      |
| `doing`    | Currently in progress |
| `blocked`  | Waiting on dependency |
| `review`   | In code review        |
| `done`     | Completed             |
| `archived` | Archived (terminal)   |

### Priority Values

| Priority   | Description               |
| ---------- | ------------------------- |
| `critical` | Immediate attention       |
| `high`     | Important, do soon        |
| `medium`   | Normal priority (default) |
| `low`      | Nice to have              |
| `someday`  | Backlog item              |

### Complexity Values

| Complexity | Description         |
| ---------- | ------------------- |
| `trivial`  | Minutes of work     |
| `simple`   | Less than an hour   |
| `medium`   | Few hours (default) |
| `complex`  | Multiple days       |
| `epic`     | Needs breakdown     |

## Endpoints

### Create Task

```http
POST /api/tasks
```

**Request Body:**

```json
{
  "title": "Implement OAuth callback handler",
  "description": "Handle OAuth callback from GitHub with PKCE validation",
  "project_id": "proj_abc123",
  "priority": "high",
  "complexity": "medium",
  "status": "todo",
  "assignees": ["alice"],
  "epic_id": "epic_oauth",
  "feature": "authentication",
  "tags": ["backend", "security"],
  "technologies": ["python", "fastapi"],
  "depends_on": ["task_oauth_setup"]
}
```

**Request Schema:**

| Field          | Type     | Required | Default  | Description       |
| -------------- | -------- | -------- | -------- | ----------------- |
| `title`        | string   | Yes      | -        | Task title        |
| `description`  | string   | No       | ""       | Task description  |
| `project_id`   | string   | Yes      | -        | Parent project ID |
| `priority`     | string   | No       | `medium` | Task priority     |
| `complexity`   | string   | No       | `medium` | Task complexity   |
| `status`       | string   | No       | `todo`   | Initial status    |
| `assignees`    | string[] | No       | []       | Assignee names    |
| `epic_id`      | string   | No       | -        | Parent epic ID    |
| `feature`      | string   | No       | -        | Feature area      |
| `tags`         | string[] | No       | []       | Tags              |
| `technologies` | string[] | No       | []       | Technologies      |
| `depends_on`   | string[] | No       | []       | Task dependencies |

**Response:**

```json
{
  "success": true,
  "action": "create",
  "task_id": "task_xyz789",
  "message": "Task created successfully",
  "data": {
    "project_id": "proj_abc123"
  }
}
```

### Start Task

```http
POST /api/tasks/{task_id}/start
```

Moves task from `todo` to `doing` status.

**Request Body (optional):**

```json
{
  "assignee": "alice"
}
```

**Response:**

```json
{
  "success": true,
  "action": "start_task",
  "task_id": "task_xyz789",
  "message": "Task started",
  "data": {
    "status": "doing",
    "branch_name": "task/xyz789-implement-oauth"
  }
}
```

### Block Task

```http
POST /api/tasks/{task_id}/block
```

Marks task as blocked with a reason.

**Request Body:**

```json
{
  "reason": "Waiting on API credentials from vendor"
}
```

**Response:**

```json
{
  "success": true,
  "action": "block_task",
  "task_id": "task_xyz789",
  "message": "Task blocked: Waiting on API credentials from vendor",
  "data": {
    "status": "blocked",
    "reason": "Waiting on API credentials from vendor"
  }
}
```

### Unblock Task

```http
POST /api/tasks/{task_id}/unblock
```

Resumes a blocked task (moves to `doing`).

**Response:**

```json
{
  "success": true,
  "action": "unblock_task",
  "task_id": "task_xyz789",
  "message": "Task unblocked, resuming work",
  "data": {
    "status": "doing"
  }
}
```

### Submit for Review

```http
POST /api/tasks/{task_id}/review
```

Submits task for code review.

**Request Body (optional):**

```json
{
  "pr_url": "https://github.com/org/repo/pull/42",
  "commit_shas": ["abc123", "def456"]
}
```

**Response:**

```json
{
  "success": true,
  "action": "submit_review",
  "task_id": "task_xyz789",
  "message": "Task submitted for review",
  "data": {
    "status": "review",
    "pr_url": "https://github.com/org/repo/pull/42"
  }
}
```

### Complete Task

```http
POST /api/tasks/{task_id}/complete
```

Marks task as done with optional learnings.

**Request Body (optional):**

```json
{
  "actual_hours": 4.5,
  "learnings": "OAuth state parameter must be cryptographically random. Used secrets.token_urlsafe(32) for 256-bit entropy."
}
```

**Response:**

```json
{
  "success": true,
  "action": "complete_task",
  "task_id": "task_xyz789",
  "message": "Task completed with learnings captured",
  "data": {
    "status": "done",
    "learnings": "OAuth state parameter must be..."
  }
}
```

When learnings are provided, Sibyl automatically creates an episode entity and enqueues it for
processing in the background.

### Archive Task

```http
POST /api/tasks/{task_id}/archive
```

Archives task without completing.

**Request Body (optional):**

```json
{
  "reason": "Requirement changed, no longer needed"
}
```

**Response:**

```json
{
  "success": true,
  "action": "archive_task",
  "task_id": "task_xyz789",
  "message": "Task archived",
  "data": {
    "status": "archived"
  }
}
```

### Update Task

```http
PATCH /api/tasks/{task_id}
```

Updates task fields directly. Bypasses workflow state machine.

**Request Body:**

```json
{
  "status": "todo",
  "priority": "critical",
  "complexity": "complex",
  "title": "Updated task title",
  "description": "Updated description",
  "assignees": ["alice", "bob"],
  "epic_id": "epic_new",
  "feature": "new-feature",
  "tags": ["urgent"],
  "technologies": ["python", "redis"]
}
```

**Request Schema (all fields optional):**

| Field          | Type     | Description      |
| -------------- | -------- | ---------------- |
| `status`       | string   | New status       |
| `priority`     | string   | New priority     |
| `complexity`   | string   | New complexity   |
| `title`        | string   | New title        |
| `description`  | string   | New description  |
| `assignees`    | string[] | New assignees    |
| `epic_id`      | string   | New epic         |
| `feature`      | string   | New feature area |
| `tags`         | string[] | New tags         |
| `technologies` | string[] | New technologies |

**Response:**

```json
{
  "success": true,
  "action": "update_task",
  "task_id": "task_xyz789",
  "message": "Task updated: priority, assignees",
  "data": {
    "priority": "critical",
    "assignees": ["alice", "bob"]
  }
}
```

## Task Notes

### Create Note

```http
POST /api/tasks/{task_id}/notes
```

Adds a note to a task.

**Request Body:**

```json
{
  "content": "Discussed with team, will use PKCE flow instead of implicit",
  "author_type": "user",
  "author_name": "alice"
}
```

**Request Schema:**

| Field         | Type   | Required | Default | Description       |
| ------------- | ------ | -------- | ------- | ----------------- |
| `content`     | string | Yes      | -       | Note content      |
| `author_type` | string | No       | `user`  | `user` or `agent` |
| `author_name` | string | No       | ""      | Author identifier |

**Response:**

```json
{
  "id": "note_abc123",
  "task_id": "task_xyz789",
  "content": "Discussed with team, will use PKCE flow instead of implicit",
  "author_type": "user",
  "author_name": "alice",
  "created_at": "2024-12-30T10:30:00Z"
}
```

### List Notes

```http
GET /api/tasks/{task_id}/notes
```

**Query Parameters:**

| Parameter | Type    | Default | Description             |
| --------- | ------- | ------- | ----------------------- |
| `limit`   | integer | 50      | Maximum notes to return |

**Response:**

```json
{
  "notes": [
    {
      "id": "note_abc123",
      "task_id": "task_xyz789",
      "content": "Discussed with team...",
      "author_type": "user",
      "author_name": "alice",
      "created_at": "2024-12-30T10:30:00Z"
    },
    {
      "id": "note_def456",
      "task_id": "task_xyz789",
      "content": "Found relevant pattern in knowledge base",
      "author_type": "agent",
      "author_name": "claude",
      "created_at": "2024-12-30T11:00:00Z"
    }
  ],
  "count": 2
}
```

## State Machine

### Valid Transitions

```
backlog --> todo --> doing --> blocked
                 |         |
                 v         v
              review --> done --> archived
```

| From      | Allowed To                          |
| --------- | ----------------------------------- |
| `backlog` | `todo`                              |
| `todo`    | `doing`, `archived`                 |
| `doing`   | `blocked`, `review`, `done`, `todo` |
| `blocked` | `doing`, `archived`                 |
| `review`  | `doing`, `done`, `archived`         |
| `done`    | `archived`                          |

Invalid transitions return a `400` with the standard error envelope:

```json
{
  "error": "invalid_request",
  "message": "Cannot transition from 'todo' to 'done'. Use workflow actions.",
  "request_id": "req_a1b2c3d4e5f6",
  "remediation": "Check the command arguments and try again."
}
```

## Concurrency Control

Update operations acquire a distributed lock. A lock conflict returns a `409` with error code
`conflict`:

```json
{
  "error": "conflict",
  "message": "Task is being updated by another process. Please retry.",
  "request_id": "req_a1b2c3d4e5f6",
  "remediation": "Refresh the resource state and retry the operation."
}
```

## WebSocket Events

Task operations broadcast real-time events:

| Event            | Data                                  |
| ---------------- | ------------------------------------- |
| `entity_created` | Task creation                         |
| `entity_updated` | Task update (includes `action` field) |
| `note_created`   | Note added                            |

## Error Responses

| Status | Cause                               |
| ------ | ----------------------------------- |
| 400    | Invalid state transition or request |
| 401    | Missing or invalid authentication   |
| 403    | Insufficient permissions            |
| 404    | Task not found                      |
| 409    | Concurrent modification conflict    |
| 422    | Request body validation failed      |
| 500    | Internal server error               |

## Examples

### Complete Workflow

```bash
# 1. Create task
curl -X POST "/api/tasks" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"title": "Implement feature", "project_id": "proj_123"}'

# 2. Start task
curl -X POST "/api/tasks/task_xyz/start" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"assignee": "alice"}'

# 3. Add note
curl -X POST "/api/tasks/task_xyz/notes" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"content": "Working on this now"}'

# 4. Submit for review
curl -X POST "/api/tasks/task_xyz/review" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"pr_url": "https://github.com/org/repo/pull/42"}'

# 5. Complete with learnings
curl -X POST "/api/tasks/task_xyz/complete" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"learnings": "Key insight from implementation..."}'
```

## Related

- [rest-entities.md](./rest-entities.md) - General entity operations
- [mcp-manage.md](./mcp-manage.md) - MCP equivalent operations
- [mcp-explore.md](./mcp-explore.md) - List and filter tasks
