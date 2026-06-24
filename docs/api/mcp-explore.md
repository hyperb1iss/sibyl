# MCP Tool: explore

Navigate and browse the Sibyl knowledge graph structure without semantic search. Ideal for listing
entities, graph traversal, and understanding relationships.

## Overview

The `explore` tool provides four modes:

| Mode           | Purpose                                     |
| -------------- | ------------------------------------------- |
| `list`         | Browse entities by type with filters        |
| `related`      | Find directly connected entities            |
| `traverse`     | Multi-hop graph traversal                   |
| `dependencies` | Task dependency chains in topological order |

## Input Schema

```typescript
interface ExploreInput {
  // Mode Selection
  mode: "list" | "related" | "traverse" | "dependencies"; // default "list"

  // Entity Filtering (for list mode)
  types?: string[]; // Entity types to include
  language?: string; // Programming language filter
  category?: string; // Category/domain filter

  // Task-Specific Filters (for list mode with tasks)
  project?: string; // Project ID filter
  status?: string; // Status filter (comma-separated)

  // Graph Traversal (for related/traverse/dependencies)
  entity_id?: string; // Starting entity ID
  relationship_types?: string[]; // Filter edge types
  depth?: number; // Traversal depth (1-3, default 1)

  // Pagination
  limit?: number; // 1-200, default 50
}
```

The MCP `explore` tool exposes the parameters above. Richer task filters (`epic`, `no_epic`,
`priority`, `complexity`, `feature`, `tags`, `include_archived`) and an `offset` argument are
available on the REST `POST /api/search/explore` endpoint, not the MCP tool. See
[rest-search.md](./rest-search.md).

### Entity Types

```
pattern, rule, template, topic, episode, procedure, task, project, epic, source, document
```

### Relationship Types

```
APPLIES_TO, REQUIRES, CONFLICTS_WITH, SUPERSEDES, DOCUMENTED_IN,
ENABLES, BREAKS, PART_OF, RELATED_TO, DERIVED_FROM
```

### Task Status Values

```
backlog, todo, doing, blocked, review, done, archived
```

## Response Schema

```typescript
interface ExploreResponse {
  mode: string;
  entities: EntitySummary[] | RelatedEntity[];
  total: number; // Count in this response
  filters: Record<string, any>;
  limit: number;
  offset: number;
  has_more: boolean;
  actual_total?: number; // Total matching (for pagination)
}

interface EntitySummary {
  id: string;
  type: string;
  name: string;
  description: string; // Truncated to 200 chars
  metadata: Record<string, any>;
}

interface RelatedEntity {
  id: string;
  type: string;
  name: string;
  relationship: string;
  direction: "outgoing" | "incoming";
  distance: number;
}
```

## Mode: list

Browse entities by type with optional filters.

### List All Projects

```json
{
  "name": "explore",
  "arguments": {
    "mode": "list",
    "types": ["project"]
  }
}
```

**Response:**

```json
{
  "mode": "list",
  "entities": [
    {
      "id": "proj_abc123",
      "type": "project",
      "name": "Auth System",
      "description": "Authentication and authorization services",
      "metadata": {
        "status": "active",
        "repository_url": "github.com/org/auth"
      }
    }
  ],
  "total": 3,
  "has_more": false,
  "actual_total": 3
}
```

### List Epics in Project

```json
{
  "name": "explore",
  "arguments": {
    "mode": "list",
    "types": ["epic"],
    "project": "proj_abc123"
  }
}
```

### List Tasks by Status

```json
{
  "name": "explore",
  "arguments": {
    "mode": "list",
    "types": ["task"],
    "project": "proj_abc123",
    "status": "todo,doing"
  }
}
```

### List Patterns by Language

```json
{
  "name": "explore",
  "arguments": {
    "mode": "list",
    "types": ["pattern"],
    "language": "typescript",
    "limit": 20
  }
}
```

For richer task filtering by priority, complexity, epic, or tags, use the REST
`POST /api/search/explore` endpoint described in [rest-search.md](./rest-search.md).

## Mode: related

Find entities directly connected to a specific entity.

### Find Related Knowledge

```json
{
  "name": "explore",
  "arguments": {
    "mode": "related",
    "entity_id": "pattern_oauth"
  }
}
```

**Response:**

```json
{
  "mode": "related",
  "entities": [
    {
      "id": "rule_jwt_validation",
      "type": "rule",
      "name": "JWT Validation Rules",
      "relationship": "RELATED_TO",
      "direction": "incoming",
      "distance": 1
    },
    {
      "id": "template_auth",
      "type": "template",
      "name": "Auth Middleware Template",
      "relationship": "DOCUMENTED_IN",
      "direction": "outgoing",
      "distance": 1
    }
  ],
  "total": 2
}
```

### Filter by Relationship Type

```json
{
  "name": "explore",
  "arguments": {
    "mode": "related",
    "entity_id": "pattern_oauth",
    "relationship_types": ["REQUIRES", "CONFLICTS_WITH"]
  }
}
```

## Mode: traverse

Multi-hop graph traversal from a starting entity.

### Explore Project Graph

```json
{
  "name": "explore",
  "arguments": {
    "mode": "traverse",
    "entity_id": "proj_abc123",
    "depth": 2
  }
}
```

### Traverse with Relationship Filter

```json
{
  "name": "explore",
  "arguments": {
    "mode": "traverse",
    "entity_id": "topic_auth",
    "depth": 2,
    "relationship_types": ["PART_OF", "RELATED_TO"]
  }
}
```

## Mode: dependencies

Get task dependency chains in topological order (dependencies before dependents).

### Get Task Dependencies

```json
{
  "name": "explore",
  "arguments": {
    "mode": "dependencies",
    "entity_id": "task_123"
  }
}
```

**Response:**

```json
{
  "mode": "dependencies",
  "entities": [
    {
      "id": "task_100",
      "type": "task",
      "name": "Set up database",
      "description": "Initialize PostgreSQL schema",
      "metadata": {
        "status": "done",
        "depth": 2,
        "is_root": false
      }
    },
    {
      "id": "task_110",
      "type": "task",
      "name": "Create user model",
      "description": "Define user entity and migrations",
      "metadata": {
        "status": "done",
        "depth": 1,
        "is_root": false
      }
    },
    {
      "id": "task_123",
      "type": "task",
      "name": "Implement auth flow",
      "description": "Add login and registration",
      "metadata": {
        "status": "doing",
        "depth": 0,
        "is_root": true
      }
    }
  ],
  "total": 3,
  "filters": {}
}
```

### Circular Dependency Detection

If cycles are detected, they're reported in the response:

```json
{
  "mode": "dependencies",
  "entities": [...],
  "filters": {
    "circular_dependencies": [
      {"from": "task_123", "to": "task_100"}
    ],
    "warning": "Circular dependencies detected"
  }
}
```

## Workflow Patterns

### Project-First Task Management

Always start with project discovery:

```
1. explore(mode="list", types=["project"])
2. explore(mode="list", types=["epic"], project="<project_id>")
3. explore(mode="list", types=["task"], epic="<epic_id>", status="todo")
```

### Knowledge Discovery

```
1. search("topic") --> Find relevant patterns
2. explore(mode="related", entity_id="<pattern_id>") --> Find related rules
3. explore(mode="traverse", entity_id="<rule_id>", depth=2) --> Explore context
```

## Error Handling

| Error                      | Cause                                        | Resolution                      |
| -------------------------- | -------------------------------------------- | ------------------------------- |
| `entity_id required`       | Missing ID for related/traverse/dependencies | Provide valid entity_id         |
| `organization_id required` | No org context                               | Ensure valid JWT with org claim |
| `Entity not found`         | Invalid entity_id                            | Verify entity exists            |

## Related

- [mcp-search.md](./mcp-search.md) - Semantic search with explore
- [mcp-add.md](./mcp-add.md) - Create new entities
- [mcp-manage.md](./mcp-manage.md) - Task lifecycle operations
- [rest-search.md](./rest-search.md) - REST explore with richer task filters
