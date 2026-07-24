# REST API: Search

Unified semantic search across knowledge graph and crawled documentation.

## Overview

The search router provides three endpoints:

- `POST /api/search` - semantic search across the knowledge graph, crawled documents, and raw
  memories
- `POST /api/search/explore` - graph navigation without semantic search
- `POST /api/search/temporal` - bi-temporal edge history queries

Search supports embedding-based ranking, unified graph and document results, filtering by entity
type, language, and status, and pagination.

**Base URL:** `/api/search`

## Authentication

All endpoints require authentication via:

- JWT access token (cookie or Authorization header)
- API key with `api:read` scope

## Role Requirements

| Operation | Required Roles               |
| --------- | ---------------------------- |
| Search    | Owner, Admin, Member, Viewer |
| Explore   | Owner, Admin, Member, Viewer |
| Temporal  | Owner, Admin, Member, Viewer |

## Endpoints

### Semantic Search

```http
POST /api/search
```

Search both knowledge graph entities and crawled documentation.

**Request Body:**

```json
{
  "query": "OAuth implementation patterns",
  "types": ["pattern", "rule", "document"],
  "language": "python",
  "category": "authentication",
  "status": null,
  "project": null,
  "source": null,
  "source_id": null,
  "source_name": "api-docs",
  "assignee": null,
  "since": null,
  "limit": 10,
  "offset": 0,
  "include_content": true,
  "include_documents": true,
  "include_graph": true,
  "use_enhanced": true,
  "boost_recent": true
}
```

**Request Schema:**

| Field                           | Type     | Required | Default   | Description                                     |
| ------------------------------- | -------- | -------- | --------- | ----------------------------------------------- |
| `query`                         | string   | Yes      | -         | Search query (min 1 char)                       |
| `types`                         | string[] | No       | -         | Entity types to search                          |
| `language`                      | string   | No       | -         | Programming language filter                     |
| `category`                      | string   | No       | -         | Category filter                                 |
| `status`                        | string   | No       | -         | Task status filter                              |
| `project`                       | string   | No       | -         | Project ID filter                               |
| `source`                        | string   | No       | -         | Source name alias                               |
| `source_id`                     | string   | No       | -         | Document/raw-memory source ID                   |
| `source_name`                   | string   | No       | -         | Document source name (partial)                  |
| `assignee`                      | string   | No       | -         | Task assignee filter                            |
| `since`                         | string   | No       | -         | Created after date (ISO or relative)            |
| `reference_time`                | string   | No       | -         | As-of timestamp for relative temporal ranking   |
| `as_of`                         | string   | No       | -         | Point-in-time validity filter (graph results)   |
| `limit`                         | integer  | No       | 10        | Results per page (1-50)                         |
| `offset`                        | integer  | No       | 0         | Pagination offset                               |
| `include_content`               | boolean  | No       | true      | Include full content                            |
| `content_max_chars`             | integer  | No       | 500       | Maximum content characters per result (0-50000) |
| `include_documents`             | boolean  | No       | true      | Search documents                                |
| `include_graph`                 | boolean  | No       | true      | Search graph entities                           |
| `include_raw_memory`            | boolean  | No       | true      | Include raw memories in search                  |
| `memory_scope`                  | string   | No       | `private` | Raw memory scope to search                      |
| `scope_key`                     | string   | No       | -         | Raw memory scope key                            |
| `participants`                  | string[] | No       | `[]`      | Participant IDs for imported raw memories       |
| `labels`                        | string[] | No       | `[]`      | Adapter labels/tags for imported raw memories   |
| `thread_id`                     | string   | No       | -         | Imported raw-memory thread ID                   |
| `occurred_after`                | datetime | No       | -         | Earliest source occurrence for raw memories     |
| `occurred_before`               | datetime | No       | -         | Latest source occurrence for raw memories       |
| `use_enhanced`                  | boolean  | No       | true      | Use hybrid retrieval                            |
| `boost_recent`                  | boolean  | No       | true      | Boost recent results                            |
| `include_retrieval_diagnostics` | boolean  | No       | false     | Include candidate ranking diagnostics           |
| `record_exposure`               | boolean  | No       | true      | Record returned results as memory exposures     |

`memory_scope` accepts `private`, `delegated`, `project`, `team`, `organization`, `shared`, or
`public`. The raw-memory filters (`participants`, `labels`, `thread_id`,
`occurred_after`/`occurred_before`) apply to imported raw memories only.

**Example Request:**

```bash
curl -X POST "http://localhost:3334/api/search" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "query": "error handling async",
    "types": ["pattern", "episode"],
    "language": "python",
    "limit": 5
  }'
```

**Response:**

```json
{
  "results": [
    {
      "id": "pattern_abc123",
      "type": "pattern",
      "name": "Async Error Handling Pattern",
      "content": "When handling errors in async code, always use try/except blocks with specific exception types...",
      "score": 0.92,
      "source": null,
      "url": null,
      "result_origin": "graph",
      "metadata": {
        "category": "error-handling",
        "languages": ["python", "typescript"],
        "status": null,
        "priority": null
      }
    },
    {
      "id": "550e8400-e29b-12d3-a456-426614174000",
      "type": "document",
      "name": "Python Async Documentation",
      "content": "[Exception Handling > Async] When exceptions occur in coroutines...",
      "score": 0.85,
      "source": "python-docs",
      "url": "https://docs.python.org/3/library/asyncio-exceptions.html",
      "result_origin": "document",
      "metadata": {
        "document_id": "doc_uuid",
        "source_id": "source_uuid",
        "chunk_type": "paragraph",
        "heading_path": ["Exception Handling", "Async"],
        "hint": "Use 'sibyl show <id>' for full content"
      }
    }
  ],
  "total": 12,
  "query": "error handling async",
  "filters": {
    "types": ["pattern", "episode"],
    "language": "python"
  },
  "graph_count": 3,
  "document_count": 2,
  "raw_memory_count": 0,
  "limit": 5,
  "offset": 0,
  "has_more": true
}
```

Each result's `result_origin` is `graph`, `document`, or `raw_memory`. Raw memory results carry
`type: "raw_memory"` and count toward `raw_memory_count`.

### Graph Exploration

```http
POST /api/search/explore
```

Navigate graph structure without semantic search.

**Request Body:**

```json
{
  "mode": "list",
  "types": ["task"],
  "entity_id": null,
  "relationship_types": null,
  "depth": 1,
  "language": null,
  "category": null,
  "project": "proj_abc123",
  "epic": null,
  "no_epic": false,
  "status": "todo,doing",
  "priority": "high,critical",
  "complexity": null,
  "feature": null,
  "tags": null,
  "include_archived": false,
  "limit": 50,
  "offset": 0
}
```

**Request Schema:**

| Field                | Type     | Required | Default | Description                                   |
| -------------------- | -------- | -------- | ------- | --------------------------------------------- |
| `mode`               | string   | No       | `list`  | `list`, `related`, `traverse`, `dependencies` |
| `types`              | string[] | No       | -       | Entity types                                  |
| `entity_id`          | string   | No       | -       | Starting entity (for related/traverse)        |
| `relationship_types` | string[] | No       | -       | Filter relationships                          |
| `depth`              | integer  | No       | 1       | Traversal depth (1-3)                         |
| `language`           | string   | No       | -       | Language filter                               |
| `category`           | string   | No       | -       | Category filter                               |
| `project`            | string   | No       | -       | Project filter                                |
| `epic`               | string   | No       | -       | Epic filter                                   |
| `no_epic`            | boolean  | No       | false   | Tasks without epic                            |
| `status`             | string   | No       | -       | Status filter (comma-separated)               |
| `priority`           | string   | No       | -       | Priority filter (comma-separated)             |
| `complexity`         | string   | No       | -       | Complexity filter                             |
| `feature`            | string   | No       | -       | Feature filter                                |
| `tags`               | string   | No       | -       | Tags filter (comma-separated)                 |
| `include_archived`   | boolean  | No       | false   | Include archived                              |
| `limit`              | integer  | No       | 50      | Results limit (1-200)                         |
| `offset`             | integer  | No       | 0       | Pagination offset                             |

**Example Request:**

```bash
curl -X POST "http://localhost:3334/api/search/explore" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "mode": "list",
    "types": ["task"],
    "project": "proj_abc123",
    "status": "todo,doing",
    "limit": 20
  }'
```

**Response:**

```json
{
  "mode": "list",
  "entities": [
    {
      "id": "task_xyz789",
      "type": "task",
      "name": "Implement OAuth callback",
      "description": "Handle OAuth callback from GitHub...",
      "metadata": {
        "status": "doing",
        "priority": "high",
        "project_id": "proj_abc123",
        "assignees": ["alice"]
      }
    }
  ],
  "total": 8,
  "filters": {
    "types": ["task"],
    "project": "proj_abc123",
    "status": "todo,doing"
  },
  "limit": 20,
  "offset": 0,
  "has_more": false,
  "actual_total": 8
}
```

### Temporal Query

```http
POST /api/search/temporal
```

Query the bi-temporal history of graph edges for point-in-time questions, timeline exploration, and
conflict detection.

**Request Body:**

```json
{
  "mode": "history",
  "entity_id": "topic_auth",
  "as_of": "2026-03-15",
  "include_expired": false,
  "limit": 50
}
```

**Request Schema:**

| Field             | Type    | Required | Default   | Description                           |
| ----------------- | ------- | -------- | --------- | ------------------------------------- |
| `mode`            | string  | No       | `history` | `history`, `timeline`, or `conflicts` |
| `entity_id`       | string  | No       | -         | Entity to scope the temporal query    |
| `as_of`           | string  | No       | -         | Point-in-time date for `history` mode |
| `include_expired` | boolean | No       | false     | Include expired edges                 |
| `limit`           | integer | No       | 50        | Maximum edges to return               |

**Modes:**

| Mode        | Answers                                               |
| ----------- | ----------------------------------------------------- |
| `history`   | Edges as they existed at `as_of`                      |
| `timeline`  | All versions of edges over time (knowledge evolution) |
| `conflicts` | Invalidated or superseded facts                       |

**Response:**

```json
{
  "mode": "history",
  "entity_id": "topic_auth",
  "edges": [
    {
      "id": "edge_abc",
      "name": "REQUIRES",
      "source_id": "topic_auth",
      "source_name": "Authentication",
      "target_id": "rule_jwt",
      "target_name": "JWT Validation Rules",
      "created_at": "2026-01-10T10:00:00Z",
      "expired_at": null,
      "valid_at": "2026-01-10T10:00:00Z",
      "invalid_at": null,
      "fact": "Authentication requires JWT validation",
      "is_current": true
    }
  ],
  "total": 1,
  "as_of": "2026-03-15T00:00:00Z",
  "message": "1 edge as of 2026-03-15"
}
```

## Search Modes

### Entity Type Filtering

Include `document` in types to search crawled documentation:

```json
{
  "query": "Next.js routing",
  "types": ["document"]
}
```

Exclude documents to search only graph:

```json
{
  "query": "authentication pattern",
  "include_documents": false
}
```

### Temporal Filtering

Filter by creation date:

```json
{
  "query": "recent discoveries",
  "since": "2024-12-01"
}
```

Relative dates supported:

```json
{
  "query": "recent discoveries",
  "since": "7d"
}
```

### Task Search

Search tasks within a project:

```json
{
  "query": "authentication",
  "types": ["task"],
  "project": "proj_abc123",
  "status": "todo,doing"
}
```

## Explore Modes

### list

Browse entities by type:

```json
{
  "mode": "list",
  "types": ["project"]
}
```

### related

Find connected entities:

```json
{
  "mode": "related",
  "entity_id": "pattern_abc123"
}
```

### traverse

Multi-hop traversal:

```json
{
  "mode": "traverse",
  "entity_id": "proj_abc123",
  "depth": 2
}
```

### dependencies

Task dependency chain:

```json
{
  "mode": "dependencies",
  "entity_id": "task_xyz789"
}
```

## Result Scoring

Results are ranked by:

1. **Semantic Similarity** - Embedding cosine distance
2. **Graph Context** - Related entity relevance (when `use_enhanced`)
3. **Temporal Boost** - Recent content ranked higher (when `boost_recent`)

Score range: 0.0 to 1.0

## Pagination

Use `limit` and `offset` for pagination:

```json
{
  "query": "patterns",
  "limit": 10,
  "offset": 20
}
```

Check `has_more` in response to determine if more results exist.

## Error Responses

| Status | Cause                             |
| ------ | --------------------------------- |
| 400    | Invalid query or parameters       |
| 401    | Missing or invalid authentication |
| 403    | Insufficient permissions          |
| 422    | Request body validation failed    |
| 500    | Search failed                     |

## Performance Tips

1. **Use type filters** - Reduce search scope with `types`
2. **Set reasonable limits** - Keep `limit` under 20 for interactive use
3. **Use source filters** - Filter docs by `source_name` when known
4. **Disable enhanced mode** - Set `use_enhanced: false` for faster results

## Related

- [mcp-search.md](./mcp-search.md) - MCP equivalent
- [mcp-explore.md](./mcp-explore.md) - Graph exploration via MCP
- [rest-entities.md](./rest-entities.md) - Direct entity access
