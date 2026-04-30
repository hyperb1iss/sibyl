# REST API: Entities

Full CRUD operations for all entity types in the Sibyl knowledge graph.

## Overview

The entities endpoint provides a unified interface for all entity types:

- Patterns, rules, templates, topics
- Episodes (learnings)
- Tasks, epics, projects
- Documents (crawled content)

**Base URL:** `/api/entities`

## Authentication

All endpoints require authentication via:

- JWT access token (cookie or Authorization header)
- API key with `api:read` and/or `api:write` scopes

## Role Requirements

| Operation                   | Required Roles               |
| --------------------------- | ---------------------------- |
| Read (GET)                  | Owner, Admin, Member, Viewer |
| Write (POST, PATCH, DELETE) | Owner, Admin, Member         |

## Endpoints

### List Entities

```http
GET /api/entities
```

**Query Parameters:**

| Parameter     | Type    | Default      | Description                                                   |
| ------------- | ------- | ------------ | ------------------------------------------------------------- |
| `entity_type` | string  | -            | Filter by entity type                                         |
| `language`    | string  | -            | Filter by programming language                                |
| `category`    | string  | -            | Filter by category                                            |
| `search`      | string  | -            | Search in name and description                                |
| `page`        | integer | 1            | Page number (1-indexed)                                       |
| `page_size`   | integer | 50           | Items per page (1-200)                                        |
| `sort_by`     | string  | `updated_at` | Sort field: `name`, `created_at`, `updated_at`, `entity_type` |
| `sort_order`  | string  | `desc`       | Sort direction: `asc`, `desc`                                 |

**Example Request:**

```bash
curl -X GET "https://api.example.com/api/entities?entity_type=pattern&language=python&page_size=20" \
  -H "Authorization: Bearer $TOKEN"
```

**Response:**

```json
{
  "entities": [
    {
      "id": "pattern_abc123",
      "entity_type": "pattern",
      "name": "Error Handling Pattern",
      "description": "Centralized error handling for async operations",
      "content": "Full pattern content...",
      "category": "error-handling",
      "languages": ["python", "typescript"],
      "tags": ["async", "resilience"],
      "metadata": {},
      "source_file": null,
      "created_at": "2024-12-01T10:00:00Z",
      "updated_at": "2024-12-15T14:30:00Z"
    }
  ],
  "total": 45,
  "page": 1,
  "page_size": 20,
  "has_more": true
}
```

### Get Entity

```http
GET /api/entities/{entity_id}
```

Retrieves a single entity by ID. Transparently handles both:

- Graph entities (stored in FalkorDB)
- Document chunks (stored in PostgreSQL)

**Path Parameters:**

| Parameter   | Type   | Description                      |
| ----------- | ------ | -------------------------------- |
| `entity_id` | string | Entity ID or document chunk UUID |

**Example Request:**

```bash
curl -X GET "https://api.example.com/api/entities/pattern_abc123" \
  -H "Authorization: Bearer $TOKEN"
```

**Response:**

```json
{
  "id": "pattern_abc123",
  "entity_type": "pattern",
  "name": "Error Handling Pattern",
  "description": "Centralized error handling for async operations",
  "content": "# Error Handling Pattern\n\nUse try/except with structured error types...",
  "category": "error-handling",
  "languages": ["python", "typescript"],
  "tags": ["async", "resilience"],
  "metadata": {
    "severity": "high",
    "use_cases": ["API endpoints", "background jobs"]
  },
  "source_file": null,
  "created_at": "2024-12-01T10:00:00Z",
  "updated_at": "2024-12-15T14:30:00Z"
}
```

**Document Chunk Response:**

When fetching a document chunk, additional metadata is included:

```json
{
  "id": "550e8400-e29b-12d3-a456-426614174000",
  "entity_type": "document",
  "name": "Next.js Documentation",
  "description": "Getting Started > Installation",
  "content": "## Installation\n\nTo install Next.js...",
  "category": "heading",
  "languages": ["javascript"],
  "tags": [],
  "metadata": {
    "source_id": "source_uuid",
    "source_name": "next-docs",
    "source_url": "https://nextjs.org/docs",
    "document_id": "doc_uuid",
    "document_url": "https://nextjs.org/docs/getting-started",
    "chunk_index": 3,
    "chunk_type": "heading",
    "heading_path": ["Getting Started", "Installation"],
    "result_origin": "document"
  },
  "source_file": "https://nextjs.org/docs/getting-started",
  "created_at": "2024-12-20T10:00:00Z",
  "updated_at": "2024-12-20T10:00:00Z"
}
```

### Create Entity

```http
POST /api/entities
```

Creates a new entity in the knowledge graph.

**Query Parameters:**

| Parameter | Type    | Default | Description                   |
| --------- | ------- | ------- | ----------------------------- |
| `sync`    | boolean | false   | Wait for creation to complete |

**Request Body:**

```json
{
  "name": "Connection Pool Pattern",
  "description": "Database connection pooling best practices",
  "content": "# Connection Pool Pattern\n\nAlways use connection pooling...",
  "entity_type": "pattern",
  "category": "database",
  "languages": ["python", "go"],
  "tags": ["performance", "database"],
  "metadata": {
    "project_id": "proj_abc123",
    "priority": "high"
  }
}
```

**Request Schema:**

| Field         | Type     | Required | Max Length | Description                |
| ------------- | -------- | -------- | ---------- | -------------------------- |
| `name`        | string   | Yes      | 200        | Entity name/title          |
| `description` | string   | No       | -          | Short description          |
| `content`     | string   | No       | 50,000     | Full content               |
| `entity_type` | string   | No       | -          | Default: `episode`         |
| `category`    | string   | No       | -          | Category for organization  |
| `languages`   | string[] | No       | -          | Programming languages      |
| `tags`        | string[] | No       | -          | Searchable tags            |
| `metadata`    | object   | No       | -          | Additional structured data |

**Example Request:**

```bash
curl -X POST "https://api.example.com/api/entities?sync=true" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Connection Pool Pattern",
    "content": "Always use connection pooling...",
    "entity_type": "pattern",
    "category": "database",
    "languages": ["python"]
  }'
```

**Response (201 Created):**

```json
{
  "id": "pattern_xyz789",
  "entity_type": "pattern",
  "name": "Connection Pool Pattern",
  "description": "",
  "content": "Always use connection pooling...",
  "category": "database",
  "languages": ["python"],
  "tags": [],
  "metadata": {},
  "source_file": null,
  "created_at": "2024-12-30T10:00:00Z",
  "updated_at": "2024-12-30T10:00:00Z"
}
```

**Async Response (sync=false):**

When `sync=false`, the response returns immediately with a pending status:

```json
{
  "id": "pattern_xyz789",
  "entity_type": "pattern",
  "name": "Connection Pool Pattern",
  "description": "",
  "content": "Always use connection pooling...",
  "category": "database",
  "languages": ["python"],
  "tags": [],
  "metadata": {},
  "source_file": null,
  "created_at": null,
  "updated_at": null
}
```

The entity is processed in the background and a WebSocket event (`entity_pending`) is broadcast.

### Update Entity

```http
PATCH /api/entities/{entity_id}
```

Updates an existing entity. Only provided fields are updated.

**Path Parameters:**

| Parameter   | Type   | Description         |
| ----------- | ------ | ------------------- |
| `entity_id` | string | Entity ID to update |

**Request Body:**

```json
{
  "name": "Updated Pattern Name",
  "tags": ["new-tag", "another-tag"],
  "metadata": {
    "reviewed": true
  }
}
```

**Request Schema (all fields optional):**

| Field         | Type     | Description                   |
| ------------- | -------- | ----------------------------- |
| `name`        | string   | New name/title                |
| `description` | string   | New description               |
| `content`     | string   | New content                   |
| `category`    | string   | New category                  |
| `languages`   | string[] | New languages list            |
| `tags`        | string[] | New tags list                 |
| `metadata`    | object   | Merged with existing metadata |

**Example Request:**

```bash
curl -X PATCH "https://api.example.com/api/entities/pattern_abc123" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "tags": ["updated", "reviewed"],
    "metadata": {"status": "approved"}
  }'
```

**Response:**

```json
{
  "id": "pattern_abc123",
  "entity_type": "pattern",
  "name": "Error Handling Pattern",
  "description": "...",
  "content": "...",
  "category": "error-handling",
  "languages": ["python"],
  "tags": ["updated", "reviewed"],
  "metadata": { "status": "approved" },
  "source_file": null,
  "created_at": "2024-12-01T10:00:00Z",
  "updated_at": "2024-12-30T10:30:00Z"
}
```

### Delete Entity

```http
DELETE /api/entities/{entity_id}
```

Deletes an entity from the knowledge graph.

**Path Parameters:**

| Parameter   | Type   | Description         |
| ----------- | ------ | ------------------- |
| `entity_id` | string | Entity ID to delete |

**Example Request:**

```bash
curl -X DELETE "https://api.example.com/api/entities/pattern_abc123" \
  -H "Authorization: Bearer $TOKEN"
```

**Response:** `204 No Content`

## Entity Types

| Type         | Description          | Use Case                               |
| ------------ | -------------------- | -------------------------------------- |
| `episode`    | Temporal learning    | Insights, discoveries, debugging notes |
| `pattern`    | Coding pattern       | Best practices, design patterns        |
| `procedure`  | Step-by-step guide   | Runbooks, workflows, how-tos           |
| `guide`      | Guide                | Project-level standards                |
| `note`       | Freeform note        | Quick captures, reminders              |
| `rule`       | Rule                 | Guidelines, standards                  |
| `template`   | Code template        | Boilerplate, scaffolding               |
| `topic`      | Knowledge topic      | Conceptual groupings                   |
| `task`       | Work item            | See [rest-tasks.md](./rest-tasks.md)   |
| `epic`       | Feature initiative   | Groups related tasks                   |
| `project`    | Container            | Groups epics and tasks                 |
| `source`     | Documentation source | Crawled documentation                  |
| `document`   | Document chunk       | Individual doc sections                |

## Concurrency Control

Update and delete operations acquire a distributed lock to prevent concurrent modifications:

```
409 Conflict
{
  "detail": "Entity is being updated by another process. Please retry."
}
```

Locks automatically expire after 30 seconds. Use exponential backoff on 409 responses.

## WebSocket Events

Entity operations broadcast real-time events:

| Event            | Trigger                                 |
| ---------------- | --------------------------------------- |
| `entity_pending` | Async creation started                  |
| `entity_created` | Entity created (sync or async complete) |
| `entity_updated` | Entity updated                          |
| `entity_deleted` | Entity deleted                          |

## Error Responses

| Status | Cause                             |
| ------ | --------------------------------- |
| 400    | Validation error in request body  |
| 401    | Missing or invalid authentication |
| 403    | Insufficient permissions          |
| 404    | Entity not found                  |
| 409    | Concurrent modification conflict  |
| 422    | Request body validation failed    |
| 500    | Internal server error             |

## Related

- [rest-tasks.md](./rest-tasks.md) - Task-specific endpoints
- [rest-search.md](./rest-search.md) - Semantic search endpoint
- [mcp-add.md](./mcp-add.md) - MCP equivalent for creation
