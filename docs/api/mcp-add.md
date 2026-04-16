# MCP Tool: add

Add new knowledge to the Sibyl knowledge graph. Supports episodes (learnings), patterns, tasks,
epics, and projects with automatic relationship discovery.

## Overview

The `add` tool creates entities in the knowledge graph with:

- Automatic embedding generation
- Auto-discovery of related entities
- Relationship creation (BELONGS_TO, DEPENDS_ON, RELATED_TO)
- Auto-tagging based on content analysis

## Input Schema

```typescript
interface AddInput {
  // Required
  title: string; // Short title (max 200 chars)
  content: string; // Full content (max 50k chars)

  // Entity Configuration
  entity_type?: string; // Default: "episode"
  category?: string; // Domain category
  languages?: string[]; // Programming languages
  tags?: string[]; // Searchable tags
  related_to?: string[]; // Entity IDs to link (RELATED_TO)
  metadata?: Record<string, any>; // Additional structured data

  // Task/Epic-Specific
  project?: string; // Project ID (REQUIRED for tasks/epics)
  epic?: string; // Epic ID for tasks
  priority?: string; // critical, high, medium, low, someday
  assignees?: string[]; // Assignee names
  due_date?: string; // ISO date format
  technologies?: string[]; // Technologies involved
  depends_on?: string[]; // Task IDs for dependencies

  // Project-Specific
  repository_url?: string; // Repository URL

  // Conflict Detection
  check_conflicts?: boolean; // Check for duplicates (default: true)
  conflict_threshold?: number; // Similarity threshold 0-1 (default: 0.70)

  // Execution Mode
  sync?: boolean; // Wait for processing (default: false)
}
```

### Entity Types

| Type      | Description                     | Requirements           |
| --------- | ------------------------------- | ---------------------- |
| `episode`   | Temporal learning (default)     | None                   |
| `pattern`   | Coding pattern or best practice | None                   |
| `procedure` | Step-by-step procedure          | None                   |
| `task`      | Work item with workflow         | **Requires `project`** |
| `epic`      | Feature initiative              | **Requires `project`** |
| `project`   | Container for epics/tasks       | None                   |

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
    "epic": "epic_oauth",
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

When `sync: true`, Sibyl automatically discovers and links to related entities:

```json
{
  "name": "add",
  "arguments": {
    "title": "JWT validation middleware",
    "content": "...",
    "entity_type": "pattern",
    "category": "authentication",
    "sync": true
  }
}
```

**Response:**

```json
{
  "success": true,
  "id": "pattern_xyz789",
  "message": "Added: JWT validation middleware (linked: 3)",
  "timestamp": "2024-12-30T10:30:00Z"
}
```

Auto-linking searches for:

- Patterns with similarity >= 0.75
- Rules related to the same domain
- Templates with matching technologies

## Sync vs Async Mode

### Async Mode (Default)

```json
{
  "name": "add",
  "arguments": {
    "title": "...",
    "content": "...",
    "sync": false
  }
}
```

- Returns immediately with entity ID
- Processing happens in background via arq worker
- Entity may not be immediately queryable

### Sync Mode

```json
{
  "name": "add",
  "arguments": {
    "title": "...",
    "content": "...",
    "sync": true
  }
}
```

- Waits for Graphiti processing to complete
- Entity is immediately available
- Auto-linking is performed synchronously
- **Use for tasks that need immediate workflow operations**

## Relationships Created

### Task Relationships

| Relationship | Target  | Condition                  |
| ------------ | ------- | -------------------------- |
| `BELONGS_TO` | Project | Always (required)          |
| `BELONGS_TO` | Epic    | When `epic` provided       |
| `DEPENDS_ON` | Task    | When `depends_on` provided |

### Epic Relationships

| Relationship | Target  | Condition         |
| ------------ | ------- | ----------------- |
| `BELONGS_TO` | Project | Always (required) |

### Auto-Discovered

| Relationship | Source     | Target                              |
| ------------ | ---------- | ----------------------------------- |
| `RELATED_TO` | New entity | Semantically similar patterns/rules |

## Validation

| Field      | Constraint                           |
| ---------- | ------------------------------------ |
| `title`    | Required, max 200 characters         |
| `content`  | Required, max 50,000 characters      |
| `project`  | Required for `task` and `epic` types |
| `priority` | Must be valid priority value         |
| `due_date` | Must be valid ISO date               |

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
1. explore(mode="list", types=["project"])
2. explore(mode="list", types=["epic"], project="<project_id>")
3. add(entity_type="task", project="<project_id>", epic="<epic_id>", ...)
```

### Capture Learning

```
1. add(title="...", content="<detailed learning>", category="debugging")
2. manage(action="start_task", entity_id="<task_id>")  // Continue work
```

## Related

- [mcp-explore.md](./mcp-explore.md) - Find projects and epics
- [mcp-manage.md](./mcp-manage.md) - Task workflow operations
- [mcp-search.md](./mcp-search.md) - Find related knowledge
