# REST API: Memory

Raw memory capture, context packs, reflection, and memory administration. These endpoints back the
Sibyl memory workspace and the agent memory loop.

## Overview

Sibyl's memory model has three layers exposed over REST:

- **Raw memory** under `/api/memory/raw` stores verbatim content with provenance and scope.
- **Context packs** under `/api/context/pack` compile goal-shaped retrieval for agents.
- **Reflection** under `/api/context/reflect` and `/api/memory/reflection/*` extracts and promotes
  durable memory candidates.

Memory-space administration, audit, inspection, sharing, and source-import status round out the
surface.

**Base URLs:** `/api/memory` and `/api/context`

## Authentication

All endpoints require authentication via:

- JWT access token (cookie or Authorization header)
- API key with `api:read` for reads, `api:write` for writes

## Role Requirements

| Operation group                                     | Required Roles               |
| --------------------------------------------------- | ---------------------------- |
| Read (recall, context pack, import status)          | Owner, Admin, Member, Viewer |
| Write (remember, reflect, share preview, promotion) | Owner, Admin, Member         |
| Admin (memory spaces, audit, inspect, corrections)  | Owner, Admin                 |

## Memory Scopes

Raw memory and reflection candidates carry a memory scope:

```
private, delegated, project, team, organization, shared, public
```

Project, team, and shared scopes require a `scope_key` and an access check against the caller's
permissions.

## Raw Memory Endpoints

### Remember Raw Memory

```http
POST /api/memory/raw
```

Stores verbatim memory before extraction or graph reflection.

**Request Body:**

```json
{
  "title": "SurrealDB write concurrency",
  "raw_content": "The SurrealDB driver serializes websocket queries through a per-client asyncio lock.",
  "source_id": null,
  "memory_scope": "project",
  "scope_key": "proj_abc123",
  "diary": false,
  "agent_id": null,
  "project_id": "proj_abc123",
  "tags": ["surrealdb", "concurrency"],
  "metadata": {},
  "provenance": {},
  "capture_surface": "api"
}
```

**Request Schema:**

| Field             | Type     | Required | Default   | Description                          |
| ----------------- | -------- | -------- | --------- | ------------------------------------ |
| `raw_content`     | string   | Yes      | -         | Verbatim memory (1-500,000 chars)    |
| `title`           | string   | No       | ""        | Human title (max 300 chars)          |
| `source_id`       | string   | No       | -         | Stable provenance ID                 |
| `memory_scope`    | string   | No       | `private` | Retrieval scope                      |
| `scope_key`       | string   | No       | -         | Project/team/shared scope key        |
| `diary`           | boolean  | No       | false     | Store as a private agent diary entry |
| `agent_id`        | string   | No       | -         | Agent identity for diary entries     |
| `project_id`      | string   | No       | -         | Associated project                   |
| `tags`            | string[] | No       | `[]`      | Searchable tags                      |
| `metadata`        | object   | No       | `{}`      | Auxiliary metadata                   |
| `provenance`      | object   | No       | `{}`      | Source provenance                    |
| `capture_surface` | string   | No       | `api`     | Capture surface label                |

**Response:**

```json
{
  "id": "raw_abc123",
  "organization_id": "org_uuid",
  "source_id": "api:manual",
  "principal_id": "user_uuid",
  "memory_scope": "project",
  "scope_key": "proj_abc123",
  "title": "SurrealDB write concurrency",
  "raw_content": "The SurrealDB driver serializes websocket queries...",
  "tags": ["surrealdb", "concurrency"],
  "metadata": {},
  "provenance": {},
  "capture_surface": "api",
  "captured_at": "2026-05-16T10:00:00Z",
  "created_at": "2026-05-16T10:00:00Z",
  "score": 0.0,
  "policy_reason": "allowed"
}
```

### Recall Raw Memory

```http
POST /api/memory/raw/recall
```

Recalls verbatim memories through scoped retrieval.

**Request Body:**

```json
{
  "query": "surreal concurrency",
  "memory_scope": "project",
  "scope_key": "proj_abc123",
  "diary": false,
  "agent_id": null,
  "project_id": "proj_abc123",
  "limit": 10
}
```

| Field          | Type    | Required | Default   | Description                |
| -------------- | ------- | -------- | --------- | -------------------------- |
| `query`        | string  | Yes      | -         | Full-text recall query     |
| `memory_scope` | string  | No       | `private` | Retrieval scope            |
| `scope_key`    | string  | No       | -         | Scope key                  |
| `diary`        | boolean | No       | false     | Recall agent diary entries |
| `agent_id`     | string  | No       | -         | Agent identity to recall   |
| `project_id`   | string  | No       | -         | Project diary filter       |
| `limit`        | integer | No       | 10        | Maximum memories (1-50)    |

**Response:**

```json
{
  "query": "surreal concurrency",
  "limit": 10,
  "memories": [{ "id": "raw_abc123", "title": "SurrealDB write concurrency", "score": 0.88 }],
  "policy_reason": "allowed"
}
```

## Context Pack Endpoints

### Compile Context Pack

```http
POST /api/context/pack
```

Compiles a structured context pack for an agent goal. This is the REST equivalent of the MCP
[`context`](./mcp-context.md) tool.

**Request Body:**

```json
{
  "goal": "Add refresh-token rotation to the auth service",
  "intent": "build",
  "layer": "recall",
  "domain": null,
  "project": "proj_abc123",
  "agent_id": null,
  "limit": 24,
  "include_related": true,
  "related_limit": 3
}
```

| Field             | Type    | Required | Default  | Description                           |
| ----------------- | ------- | -------- | -------- | ------------------------------------- |
| `goal`            | string  | Yes      | -        | Agent goal or user task               |
| `intent`          | string  | No       | `build`  | How the agent will act                |
| `layer`           | string  | No       | `recall` | `wake`, `recall`, or `deep_search`    |
| `domain`          | string  | No       | -        | Domain to bias retrieval              |
| `project`         | string  | No       | -        | Project ID to scope context           |
| `agent_id`        | string  | No       | -        | Agent diary identity to include       |
| `limit`           | integer | No       | 24       | Maximum context items (1-50)          |
| `include_related` | boolean | No       | true     | Include one-hop related graph context |
| `related_limit`   | integer | No       | 3        | Related items per context item (0-5)  |

The response is a context pack with `sections`, `total_items`, `usage_hint`, and a rendered
`markdown` field. See [mcp-context.md](./mcp-context.md) for the full pack schema.

### Reflect into Memory Candidates

```http
POST /api/context/reflect
```

Reflects raw notes into durable memory candidates. This is the REST equivalent of the MCP
[`reflect`](./mcp-reflect.md) tool.

**Request Body:**

```json
{
  "content": "Session notes: settled on Surreal-native storage, dropped the Postgres sidecar.",
  "source_title": "Storage architecture session",
  "intent": "decide",
  "domain": null,
  "project": "proj_abc123",
  "related_to": null,
  "task_ids": null,
  "active_task": true,
  "persist": false,
  "persist_source": true,
  "persist_review": false,
  "limit": 12
}
```

| Field            | Type     | Required | Default              | Description                                    |
| ---------------- | -------- | -------- | -------------------- | ---------------------------------------------- |
| `content`        | string   | Yes      | -                    | Raw session notes                              |
| `source_title`   | string   | No       | `Session reflection` | Source/session title                           |
| `intent`         | string   | No       | `general`            | Reflection intent                              |
| `domain`         | string   | No       | -                    | Domain for candidates                          |
| `project`        | string   | No       | -                    | Project ID to scope candidates                 |
| `related_to`     | string[] | No       | -                    | Entity IDs to link persisted candidates        |
| `task_ids`       | string[] | No       | -                    | Task IDs to link persisted output              |
| `active_task`    | boolean  | No       | true                 | Link persisted output to the active doing task |
| `persist`        | boolean  | No       | false                | Persist candidates into the graph              |
| `persist_source` | boolean  | No       | true                 | Store the raw source notes                     |
| `persist_review` | boolean  | No       | false                | Store output in the raw review queue           |
| `limit`          | integer  | No       | 12                   | Maximum candidates (1-25)                      |

The response is a reflection pack with `candidates`, `total_candidates`, `persisted_count`, and a
rendered `markdown` field. See [mcp-reflect.md](./mcp-reflect.md) for the candidate schema.

## Reflection Promotion Endpoints

| Method | Path                                     | Purpose                                                 |
| ------ | ---------------------------------------- | ------------------------------------------------------- |
| POST   | `/api/memory/reflection/promote`         | Promote a reviewed candidate into native Surreal memory |
| POST   | `/api/memory/reflection/promote/preview` | Preview a promotion without writing                     |
| POST   | `/api/memory/reflection/review/auto`     | Auto-review and promote a single safe candidate         |
| POST   | `/api/memory/reflection/review/drain`    | Bulk auto-review pending candidates                     |

These endpoints drive the reflection dream-cycle: candidates flow into the review queue, then are
promoted automatically (when confident and within policy) or by an operator. Promotion endpoints
require Member-or-higher role.

## Memory Space Administration

Memory spaces are persisted scope records for owner/admin inspection. All space endpoints require
Owner or Admin role.

| Method | Path                                            | Purpose                               |
| ------ | ----------------------------------------------- | ------------------------------------- |
| GET    | `/api/memory/spaces`                            | List persisted memory spaces          |
| POST   | `/api/memory/spaces`                            | Create a memory-space record          |
| GET    | `/api/memory/spaces/{space_id}`                 | Inspect a space and its memberships   |
| PATCH  | `/api/memory/spaces/{space_id}`                 | Update space metadata or state        |
| POST   | `/api/memory/spaces/{space_id}/members`         | Grant a principal membership          |
| POST   | `/api/memory/spaces/{space_id}/members/preview` | Preview what a principal could recall |

## Inspection, Audit, and Sharing

| Method | Path                                                  | Purpose                                         |
| ------ | ----------------------------------------------------- | ----------------------------------------------- |
| GET    | `/api/memory/audit`                                   | List memory audit events (filterable)           |
| GET    | `/api/memory/inspect/{source_id}`                     | Inspect a raw memory source and derived records |
| POST   | `/api/memory/inspect/{source_id}/corrections/preview` | Preview a correction or lifecycle action        |
| POST   | `/api/memory/inspect/{source_id}/corrections`         | Apply a correction or lifecycle action          |
| POST   | `/api/memory/share/preview`                           | Preview memory sharing without enabling a share |
| GET    | `/api/memory/source-imports/{import_id}`              | Get source-safe import progress                 |

Memory corrections support actions such as `delete` and `hide` for lifecycle management. Audit and
inspection endpoints require Owner or Admin role; `source-imports` status is readable by any member.

## Error Responses

| Status | Cause                                                             |
| ------ | ----------------------------------------------------------------- |
| 400    | Invalid request, or `invalid_memory_space_id`                     |
| 401    | Missing or invalid authentication (`Not authenticated`)           |
| 403    | Insufficient role, project access denied, or memory policy denial |
| 404    | Source, import, or reflection candidate not found                 |
| 422    | Request body validation failed                                    |
| 500    | Memory operation failed                                           |

## Related

- [mcp-context.md](./mcp-context.md) - MCP context tool
- [mcp-remember.md](./mcp-remember.md) - MCP remember tool
- [mcp-reflect.md](./mcp-reflect.md) - MCP reflect tool
- [rest-synthesis.md](./rest-synthesis.md) - Synthesis endpoints
