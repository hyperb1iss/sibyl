# MCP Tool: manage

State-changing operations for Sibyl entities. Handles task workflow, epic management, source
operations, and analysis through a single action-dispatched tool.

## Overview

The `manage` tool handles state-changing operations organized by category:

| Category          | Actions                                                                                                                 |
| ----------------- | ----------------------------------------------------------------------------------------------------------------------- |
| Task Workflow     | `start_task`, `block_task`, `unblock_task`, `submit_review`, `complete_task`, `archive_task`, `update_task`, `add_note` |
| Epic Workflow     | `start_epic`, `complete_epic`, `archive_epic`, `update_epic`                                                            |
| Source Operations | `crawl`, `sync`, `refresh`, `link_graph`, `link_graph_status`                                                           |
| Analysis          | `estimate`, `prioritize`, `detect_cycles`, `suggest`                                                                    |

For web applications, the REST task endpoints (`/api/tasks/{id}/*`) offer the same task workflow
with finer-grained routes and authorization. The `manage` tool is the agent-facing equivalent and
remains the only interface for source and analysis actions.

Server health and graph statistics are not `manage` actions. They are exposed as the
`sibyl://health` and `sibyl://stats` MCP resources instead.

## Input Schema

```typescript
interface ManageInput {
  action: string; // Action to perform
  entity_id?: string; // Target entity ID (required for most actions)
  data?: Record<string, any>; // Action-specific data
}
```

## Task Workflow Actions

### start_task

Move task to "doing" status.

```json
{
  "name": "manage",
  "arguments": {
    "action": "start_task",
    "entity_id": "task_abc123",
    "data": {
      "assignee": "alice"
    }
  }
}
```

**Response:**

```json
{
  "success": true,
  "action": "start_task",
  "entity_id": "task_abc123",
  "message": "Task started",
  "data": {
    "status": "doing",
    "branch_name": "task/abc123-implement-oauth"
  }
}
```

### block_task

Mark task as blocked with reason.

```json
{
  "name": "manage",
  "arguments": {
    "action": "block_task",
    "entity_id": "task_abc123",
    "data": {
      "reason": "Waiting on API credentials from vendor"
    }
  }
}
```

### unblock_task

Resume a blocked task.

```json
{
  "name": "manage",
  "arguments": {
    "action": "unblock_task",
    "entity_id": "task_abc123"
  }
}
```

### submit_review

Submit task for code review.

```json
{
  "name": "manage",
  "arguments": {
    "action": "submit_review",
    "entity_id": "task_abc123",
    "data": {
      "pr_url": "https://github.com/org/repo/pull/42",
      "commit_shas": ["abc123", "def456"]
    }
  }
}
```

### complete_task

Mark task as done with optional learnings.

```json
{
  "name": "manage",
  "arguments": {
    "action": "complete_task",
    "entity_id": "task_abc123",
    "data": {
      "learnings": "OAuth state parameter must be cryptographically random. Used secrets.token_urlsafe(32).",
      "actual_hours": 4.5
    }
  }
}
```

**Response:**

```json
{
  "success": true,
  "action": "complete_task",
  "entity_id": "task_abc123",
  "message": "Task completed with learnings captured",
  "data": {
    "status": "done",
    "learnings": "OAuth state parameter must be cryptographically random..."
  }
}
```

### archive_task

Archive without completing.

```json
{
  "name": "manage",
  "arguments": {
    "action": "archive_task",
    "entity_id": "task_abc123",
    "data": {
      "reason": "Requirement changed, no longer needed"
    }
  }
}
```

### update_task

Update task fields directly.

```json
{
  "name": "manage",
  "arguments": {
    "action": "update_task",
    "entity_id": "task_abc123",
    "data": {
      "priority": "high",
      "assignees": ["alice", "bob"],
      "estimated_hours": 8,
      "sync": true
    }
  }
}
```

**Allowed Fields:**

```
title, description, status, priority, complexity, feature, sprint,
assignees, due_date, estimated_hours, actual_hours, domain,
technologies, branch_name, pr_url, task_order
```

### add_note

Add a note to a task.

```json
{
  "name": "manage",
  "arguments": {
    "action": "add_note",
    "entity_id": "task_abc123",
    "data": {
      "content": "Discussed with team, will use PKCE flow instead of implicit",
      "author_type": "agent",
      "author_name": "claude"
    }
  }
}
```

**Response:**

```json
{
  "success": true,
  "action": "add_note",
  "entity_id": "note_xyz789",
  "message": "Note added to task",
  "data": {
    "note_id": "note_xyz789",
    "task_id": "task_abc123",
    "author_type": "agent",
    "created_at": "2024-12-30T10:30:00Z"
  }
}
```

## Epic Workflow Actions

### start_epic

Move epic to in_progress status.

```json
{
  "name": "manage",
  "arguments": {
    "action": "start_epic",
    "entity_id": "epic_abc123"
  }
}
```

### complete_epic

Mark epic as completed.

```json
{
  "name": "manage",
  "arguments": {
    "action": "complete_epic",
    "entity_id": "epic_abc123",
    "data": {
      "learnings": "OAuth integration took longer than expected due to provider inconsistencies"
    }
  }
}
```

### archive_epic

Archive epic.

```json
{
  "name": "manage",
  "arguments": {
    "action": "archive_epic",
    "entity_id": "epic_abc123",
    "data": {
      "reason": "Project scope changed"
    }
  }
}
```

### update_epic

Update epic fields.

```json
{
  "name": "manage",
  "arguments": {
    "action": "update_epic",
    "entity_id": "epic_abc123",
    "data": {
      "priority": "critical",
      "target_date": "2024-03-01"
    }
  }
}
```

**Allowed Fields:**

```
title, description, status, priority, start_date, target_date,
assignees, tags, learnings
```

## Source Operations

### crawl

Trigger crawl of a URL.

```json
{
  "name": "manage",
  "arguments": {
    "action": "crawl",
    "data": {
      "url": "https://docs.example.com",
      "depth": 3,
      "patterns": ["/api/*", "/guides/*"],
      "exclude": ["/changelog/*"]
    }
  }
}
```

**Response:**

```json
{
  "success": true,
  "action": "crawl",
  "entity_id": "source_abc123",
  "message": "Crawl queued for https://docs.example.com",
  "data": {
    "source_id": "source_abc123",
    "url": "https://docs.example.com",
    "depth": 3,
    "status": "queued"
  }
}
```

### sync

Re-crawl an existing source.

```json
{
  "name": "manage",
  "arguments": {
    "action": "sync",
    "entity_id": "source_abc123"
  }
}
```

### refresh

Sync all sources.

```json
{
  "name": "manage",
  "arguments": {
    "action": "refresh"
  }
}
```

### link_graph

Link document chunks to knowledge graph entities.

```json
{
  "name": "manage",
  "arguments": {
    "action": "link_graph",
    "entity_id": "source_abc123",
    "data": {
      "max_chunks": 500
    }
  }
}
```

**Response:**

```json
{
  "success": true,
  "action": "link_graph",
  "entity_id": "source_abc123",
  "message": "Linked 42 entities from 150 chunks",
  "data": {
    "chunks_processed": 150,
    "entities_extracted": 58,
    "entities_linked": 42,
    "new_entities_created": 16,
    "errors": 0
  }
}
```

### link_graph_status

Get pending linking status.

```json
{
  "name": "manage",
  "arguments": {
    "action": "link_graph_status"
  }
}
```

**Response:**

```json
{
  "success": true,
  "action": "link_graph_status",
  "message": "350 chunks pending linking",
  "data": {
    "total_chunks": 1200,
    "chunks_with_entities": 850,
    "chunks_pending": 350,
    "sources": [
      { "name": "next-docs", "pending": 200 },
      { "name": "react-docs", "pending": 150 }
    ]
  }
}
```

## Analysis Actions

### estimate

Estimate task effort based on similar completed tasks.

```json
{
  "name": "manage",
  "arguments": {
    "action": "estimate",
    "entity_id": "task_abc123"
  }
}
```

**Response:**

```json
{
  "success": true,
  "action": "estimate",
  "entity_id": "task_abc123",
  "message": "Estimated 6 hours",
  "data": {
    "estimated_hours": 6,
    "confidence": 0.75,
    "based_on_tasks": 8,
    "similar_tasks": ["task_xyz", "task_pqr"],
    "reason": "Based on similar OAuth integration tasks"
  }
}
```

### prioritize

Get smart task ordering for a project.

```json
{
  "name": "manage",
  "arguments": {
    "action": "prioritize",
    "entity_id": "proj_abc123"
  }
}
```

**Response:**

```json
{
  "success": true,
  "action": "prioritize",
  "entity_id": "proj_abc123",
  "message": "Prioritized 15 tasks",
  "data": {
    "tasks": [
      { "id": "task_1", "name": "Set up database", "priority": "critical", "status": "todo" },
      { "id": "task_2", "name": "Create models", "priority": "high", "status": "todo" },
      { "id": "task_3", "name": "Add API routes", "priority": "high", "status": "doing" }
    ]
  }
}
```

### detect_cycles

Find circular dependencies.

```json
{
  "name": "manage",
  "arguments": {
    "action": "detect_cycles",
    "entity_id": "proj_abc123"
  }
}
```

**Response:**

```json
{
  "success": true,
  "action": "detect_cycles",
  "entity_id": "proj_abc123",
  "message": "No circular dependencies detected",
  "data": {
    "cycles": [],
    "has_cycles": false
  }
}
```

### suggest

Suggest relevant knowledge for a task.

```json
{
  "name": "manage",
  "arguments": {
    "action": "suggest",
    "entity_id": "task_abc123"
  }
}
```

**Response:**

```json
{
  "success": true,
  "action": "suggest",
  "entity_id": "task_abc123",
  "message": "Knowledge suggestions retrieved",
  "data": {
    "patterns": [{ "id": "pattern_oauth", "name": "OAuth 2.0 PKCE Flow", "score": 0.92 }],
    "rules": [{ "id": "rule_token", "name": "Token Rotation Rules", "score": 0.85 }],
    "procedures": [],
    "templates": [],
    "past_learnings": [{ "id": "episode_xyz", "name": "OAuth state bug fix", "score": 0.78 }],
    "error_patterns": []
  }
}
```

## Server Health and Statistics

The `manage` tool does not expose server health, graph statistics, or index rebuilds as actions.
Server health and graph statistics are available through the `sibyl://health` and `sibyl://stats`
MCP resources, which return the same data without an action call.

## Response Schema

```typescript
interface ManageResponse {
  success: boolean;
  action: string;
  entity_id?: string;
  message: string;
  data: Record<string, any>;
  timestamp: string;
  policy_reason?: string; // Memory policy decision for project-scoped actions
}
```

## Task State Machine

```
backlog --> todo --> doing --> blocked
                 |         |
                 v         v
              review --> done --> archived
```

### Valid Transitions

| From      | To                                  |
| --------- | ----------------------------------- |
| `backlog` | `todo`                              |
| `todo`    | `doing`, `archived`                 |
| `doing`   | `blocked`, `review`, `done`, `todo` |
| `blocked` | `doing`, `archived`                 |
| `review`  | `doing`, `done`, `archived`         |
| `done`    | `archived`                          |

## Error Handling

| Error                      | Cause                    | Resolution            |
| -------------------------- | ------------------------ | --------------------- |
| `Unknown action`           | Invalid action name      | Check action spelling |
| `entity_id required`       | Missing entity ID        | Provide entity_id     |
| `organization_id required` | No org context           | Ensure valid JWT      |
| `InvalidTransitionError`   | Invalid state transition | Check current status  |
| `Task not found`           | Invalid entity_id        | Verify entity exists  |

## Workflow Patterns

### Complete Task Cycle

```
1. add(entity_type="task", project="...", ...)
2. manage(action="start_task", entity_id="...")
3. manage(action="add_note", entity_id="...", data={content: "..."})
4. manage(action="submit_review", entity_id="...", data={pr_url: "..."})
5. manage(action="complete_task", entity_id="...", data={learnings: "..."})
```

### Knowledge Capture on Completion

When completing tasks with learnings, Sibyl automatically creates an episode entity linking the
learning to the completed task.

## Related

- [rest-tasks.md](./rest-tasks.md) - REST task endpoints (finer-grained for web apps)
- [mcp-add.md](./mcp-add.md) - Create entities
- [mcp-explore.md](./mcp-explore.md) - Navigate graph
- [mcp-logs.md](./mcp-logs.md) - Server logs for debugging
