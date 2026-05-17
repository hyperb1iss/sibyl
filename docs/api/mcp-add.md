# MCP Tool: add

Add new knowledge to the Sibyl knowledge graph. Supports episodes, patterns, procedures, tasks,
epics, projects, and domain-general memories with automatic relationship discovery.

## Overview

The `add` tool creates entities in the knowledge graph with:

- Embedding generation for semantic search
- Auto-discovery of related entities (RELATED_TO edges, similarity >= 0.75)
- Relationship creation from `related_to` and `depends_on`
- Auto-tagging based on content analysis (for tasks)
- Conflict detection against semantically similar existing knowledge

The MCP `add` tool runs against the SurrealDB-native graph runtime. There is no Graphiti or FalkorDB
processing stage in the default memory loop.

## Input Schema

```typescript
interface AddInput {
  // Required
  title: string; // Short title (max 200 chars)
  content: string; // Full content (max 50,000 chars)

  // Entity Configuration
  entity_type?: string; // Default: "episode"
  category?: string; // Domain category
  languages?: string[]; // Programming languages
  tags?: string[]; // Searchable tags
  related_to?: string[]; // Entity IDs to link (RELATED_TO)
  metadata?: Record<string, any>; // Additional structured data

  // Task-Specific
  project?: string; // Project ID (REQUIRED for tasks and epics)
  priority?: string; // critical, high, medium, low, someday
  assignees?: string[]; // Assignee names
  due_date?: string; // ISO date format
  technologies?: string[]; // Technologies involved
  depends_on?: string[]; // Task IDs for dependencies (DEPENDS_ON)

  // Project-Specific
  repository_url?: string; // Repository URL

  // Conflict Detection
  check_conflicts?: boolean; // Check for duplicates (default: true)
  skip_conflicts?: boolean; // Skip detection for latency-sensitive captures (default: false)
  conflict_threshold?: number; // Similarity score required to flag a conflict (default: 0.85)
}
```

The MCP `add` tool does not accept `sync` or `epic` arguments. To attach a task to an epic, create
the task and then set its epic through the REST tasks endpoint or `manage("update_task", ...)`. To
create an entity and wait for it to be queryable, use the REST `POST /api/entities?sync=true`
endpoint.

### Entity Types

| Type        | Description                                       | Requirements           |
| ----------- | ------------------------------------------------- | ---------------------- |
| `episode`   | Temporal knowledge (default)                      | None                   |
| `pattern`   | Coding pattern or best practice                   | None                   |
| `procedure` | Repeatable workflow or runbook                    | None                   |
| `decision`  | Chosen direction with rationale                   | None                   |
| `plan`      | Strategy, sequencing, milestones, or project plan | None                   |
| `idea`      | Brainstormed concept or unresolved option         | None                   |
| `claim`     | Atomic fact or assertion with provenance          | None                   |
| `artifact`  | File, object, document, asset, or work product    | None                   |
| `session`   | Conversation or work-session checkpoint           | None                   |
| `domain`    | Any modeled problem space                         | None                   |
| `task`      | Work item with workflow state machine             | **Requires `project`** |
| `epic`      | Feature initiative grouping tasks                 | **Requires `project`** |
| `project`   | Container for related tasks                       | None                   |

For capturing durable memory with verbatim raw provenance, prefer the
[`remember`](./mcp-remember.md) tool over `add`.

### Priority Values

```
critical, high, medium (default), low, someday
```

## Response Schema

```typescript
interface AddResponse {
  success: boolean;
  id: string | null; // Created entity ID
  message: string;
  timestamp: string; // ISO timestamp
  conflicts: ConflictWarning[]; // Duplicate detection warnings
}

interface ConflictWarning {
  id: string;
  name: string;
  score: number;
}
```

## Entity Creation Examples

### Record a Learning (Episode)

```json
{
  "name": "add",
  "arguments": {
    "title": "Redis connection pooling insight",
    "content": "Discovered that pool size must be >= concurrent requests. When pool is exhausted, connections block until timeout. Solution: Set REDIS_POOL_SIZE to at least max_workers * 2.",
    "category": "debugging",
    "languages": ["python"],
    "technologies": ["redis", "asyncio"]
  }
}
```

**Response:**

```json
{
  "success": true,
  "id": "episode_abc123def",
  "message": "Queued: Redis connection pooling insight (processing in background)",
  "timestamp": "2024-12-30T10:30:00Z"
}
```

### Create a Pattern

````json
{
  "name": "add",
  "arguments": {
    "title": "Error Boundary Pattern",
    "content": "React error boundaries catch JavaScript errors in child components. Wrap critical UI sections to prevent full app crashes.\n\n```tsx\nclass ErrorBoundary extends Component {\n  state = { hasError: false };\n  static getDerivedStateFromError() { return { hasError: true }; }\n  render() {\n    if (this.state.hasError) return <FallbackUI />;\n    return this.props.children;\n  }\n}\n```",
    "entity_type": "pattern",
    "category": "error-handling",
    "languages": ["typescript", "react"],
    "tags": ["frontend", "resilience"]
  }
}
````

### Create a Project

```json
{
  "name": "add",
  "arguments": {
    "title": "Auth System",
    "content": "Backend authentication and authorization services including JWT, OAuth2, and RBAC.",
    "entity_type": "project",
    "repository_url": "https://github.com/org/auth-system",
    "technologies": ["python", "fastapi", "postgresql"],
    "tags": ["backend", "security"]
  }
}
```

### Create an Epic

```json
{
  "name": "add",
  "arguments": {
    "title": "OAuth Integration",
    "content": "Implement OAuth2 flows for GitHub, Google, and Microsoft identity providers. Includes token management, refresh logic, and account linking.",
    "entity_type": "epic",
    "project": "proj_abc123",
    "priority": "high",
    "assignees": ["alice", "bob"]
  }
}
```

### Create a Task

```json
{
  "name": "add",
  "arguments": {
    "title": "Implement GitHub OAuth callback",
    "content": "Handle the OAuth callback from GitHub:\n1. Validate state parameter\n2. Exchange code for access token\n3. Fetch user profile\n4. Create or link user account\n5. Issue session token",
    "entity_type": "task",
    "project": "proj_abc123",
    "priority": "high",
    "technologies": ["python", "fastapi"],
    "depends_on": ["task_oauth_setup"]
  }
}
```

### Create Task with Dependencies

```json
{
  "name": "add",
  "arguments": {
    "title": "Add user dashboard",
    "content": "Create the main user dashboard showing recent activity, tasks, and notifications.",
    "entity_type": "task",
    "project": "proj_abc123",
    "depends_on": ["task_user_model", "task_auth_flow", "task_notification_api"],
    "priority": "medium",
    "technologies": ["react", "typescript"]
  }
}
```

## Auto-Tagging

When creating tasks, Sibyl automatically generates tags based on:

1. **Title and description analysis** - Keywords mapped to domains
2. **Technologies provided** - Mapped to relevant domains
3. **Project context** - Existing tags from project's tasks
4. **Explicit tags** - User-provided tags

### Domain Keywords

| Domain        | Keywords                                               |
| ------------- | ------------------------------------------------------ |
| `frontend`    | ui, ux, component, react, vue, css, layout, responsive |
| `backend`     | api, server, endpoint, route, middleware, database     |
| `database`    | sql, postgres, mongodb, redis, migration, schema       |
| `devops`      | deploy, docker, kubernetes, ci, cd, terraform          |
| `testing`     | test, pytest, jest, e2e, integration, mock             |
| `security`    | auth, permission, role, encryption, vulnerability      |
| `performance` | optimize, cache, lazy, memoize, bundle, profil         |

### Task Type Detection

| Type       | Keywords                                   |
| ---------- | ------------------------------------------ |
| `feature`  | add, implement, create, build, new         |
| `bug`      | fix, bug, issue, error, broken, crash      |
| `refactor` | refactor, clean, reorganize, simplify      |
| `chore`    | update, upgrade, bump, dependency, config  |
| `research` | research, investigate, explore, spike, poc |

## Auto-Linking

After the entity is created, Sibyl discovers and links semantically related entities. Auto-linking
searches for:

- Patterns, rules, and templates with similarity >= 0.75
- Matches against the entity title, content, technologies, and category

Auto-linked entities are connected with `RELATED_TO` edges. When the response message includes
`(linked: N)`, `N` relationships were created.

**Response with auto-links:**

```json
{
  "success": true,
  "id": "pattern_xyz789",
  "message": "Added: JWT validation middleware (linked: 3)",
  "timestamp": "2024-12-30T10:30:00Z"
}
```

## Processing Model

The MCP `add` tool queues entity creation on the active coordination runtime and returns
immediately. The coordination runtime is in-process by default, or a Redis worker when
`SIBYL_COORDINATION_BACKEND=redis` is set.

- The response returns right away with the entity ID.
- The entity becomes fully queryable once the worker finishes processing.
- A queued response carries a message like `Queued: <title> (processing in background)`.
- If the job queue is unavailable, `add` falls back to a synchronous write and the message reads
  `Added (sync fallback): <title>`.

To create an entity and block until it is queryable (for example, before immediate workflow
operations), use the REST endpoint `POST /api/entities?sync=true` documented in
[rest-entities.md](./rest-entities.md).

## Relationships Created

### From Request Parameters

| Relationship | Target     | Condition                   |
| ------------ | ---------- | --------------------------- |
| `RELATED_TO` | Any entity | For each ID in `related_to` |
| `DEPENDS_ON` | Task       | For each ID in `depends_on` |

Tasks and epics are bound to their project through the required `project` parameter.

### Auto-Discovered

| Relationship | Source     | Target                                        |
| ------------ | ---------- | --------------------------------------------- |
| `RELATED_TO` | New entity | Semantically similar patterns/rules/templates |

## Validation

| Field      | Constraint                           |
| ---------- | ------------------------------------ |
| `title`    | Required, max 200 characters         |
| `content`  | Required, max 50,000 characters      |
| `project`  | Required for `task` and `epic` types |
| `priority` | Must be a valid priority value       |
| `due_date` | Must be a valid ISO date             |

## Error Handling

| Error                      | Cause                | Resolution                                       |
| -------------------------- | -------------------- | ------------------------------------------------ |
| `Title cannot be empty`    | Missing title        | Provide title                                    |
| `Content cannot be empty`  | Missing content      | Provide content                                  |
| `Tasks require a project`  | Task without project | Use `explore(types=["project"])` to find project |
| `Epics require a project`  | Epic without project | Specify `project` parameter                      |
| `organization_id required` | No org context       | Ensure valid JWT with org claim                  |

## Workflow Patterns

### Create Task in Context

```
1. explore(mode="list", types=["project"])    // Find the project
2. add(entity_type="task", project="<project_id>", ...)
3. manage(action="start_task", entity_id="<task_id>")
```

### Capture Learning

```
1. add(title="...", content="<detailed learning>", category="debugging")
```

For durable memory that needs verbatim raw provenance and review, use
[`remember`](./mcp-remember.md) or [`reflect`](./mcp-reflect.md) instead.

## Related

- [mcp-explore.md](./mcp-explore.md) - Find projects and epics
- [mcp-manage.md](./mcp-manage.md) - Task workflow operations
- [mcp-remember.md](./mcp-remember.md) - Capture durable memory with raw provenance
- [mcp-search.md](./mcp-search.md) - Find related knowledge
- [rest-entities.md](./rest-entities.md) - REST entity creation with sync mode
