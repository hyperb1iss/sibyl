# Sibyl API Reference

Sibyl provides a dual-interface API: an eleven-tool MCP interface for assistant clients and
automation, plus a full REST API for applications and integrations. Both surfaces are served by the
same daemon (`sibyld`) and share one SurrealDB-native runtime for graph, content, and auth.

## Architecture Overview

```
Sibyl Combined App (Starlette, port 3334)
|-- /api/*    --> FastAPI REST endpoints (29 routers)
|-- /mcp      --> MCP streamable-http transport (11 tools, 2 resources)
|-- /ws       --> WebSocket for real-time updates
'-- Lifespan  --> Coordination runtime + session management
```

The coordination runtime is in-process by default. Set `SIBYL_COORDINATION_BACKEND=redis` for
multi-process or distributed worker deployments.

## Base URL

| Environment       | Base URL                      |
| ----------------- | ----------------------------- |
| Local Development | `http://localhost:3334`       |
| Production        | `https://api.your-domain.com` |

## API Interfaces

### MCP Tools (for Assistant Clients)

The MCP interface exposes eleven tools that cover discovery, context, capture, synthesis, lifecycle
operations, and introspection. Tools are registered in `apps/api/src/sibyl/server.py`.

| Tool               | Purpose                                                      | Documentation                          |
| ------------------ | ------------------------------------------------------------ | -------------------------------------- |
| `search`           | Semantic search across knowledge graph and documents         | [mcp-search.md](./mcp-search.md)       |
| `context`          | Compile a structured context pack for an agent goal          | [mcp-context.md](./mcp-context.md)     |
| `synthesis_plan`   | Plan a source-grounded synthesis outline                     | [mcp-synthesis.md](./mcp-synthesis.md) |
| `synthesis_draft`  | Draft, verify, and optionally remember a synthesis artifact  | [mcp-synthesis.md](./mcp-synthesis.md) |
| `synthesis_verify` | Verify citation, freshness, hidden-context, and gap coverage | [mcp-synthesis.md](./mcp-synthesis.md) |
| `explore`          | Navigate and browse graph structure                          | [mcp-explore.md](./mcp-explore.md)     |
| `add`              | Create new knowledge entities                                | [mcp-add.md](./mcp-add.md)             |
| `remember`         | Capture durable memory with verbatim raw provenance          | [mcp-remember.md](./mcp-remember.md)   |
| `reflect`          | Reflect raw notes into reviewable memory candidates          | [mcp-reflect.md](./mcp-reflect.md)     |
| `manage`           | Task, epic, source, and analysis lifecycle operations        | [mcp-manage.md](./mcp-manage.md)       |
| `logs`             | Recent server logs (OWNER role)                              | [mcp-logs.md](./mcp-logs.md)           |

**MCP Endpoint:** `POST /mcp` (streamable-http transport)

The MCP server also exposes two resources: `sibyl://health` (connectivity and entity counts) and
`sibyl://stats` (knowledge graph statistics).

### REST API (for Applications)

The REST API spans 29 routers. The pages below cover the most commonly used surfaces; the full
contract is in the OpenAPI schema.

| Category  | Endpoints                            | Documentation                            |
| --------- | ------------------------------------ | ---------------------------------------- |
| Entities  | `/api/entities/*`                    | [rest-entities.md](./rest-entities.md)   |
| Tasks     | `/api/tasks/*`                       | [rest-tasks.md](./rest-tasks.md)         |
| Projects  | `/api/entities?entity_type=project`  | [rest-projects.md](./rest-projects.md)   |
| Search    | `/api/search`, `/api/search/explore` | [rest-search.md](./rest-search.md)       |
| Memory    | `/api/memory/*`, `/api/context/*`    | [rest-memory.md](./rest-memory.md)       |
| Synthesis | `/api/synthesis/*`                   | [rest-synthesis.md](./rest-synthesis.md) |

Additional routers not covered by dedicated pages include `auth`, `users`, `epics`, `graph`,
`crawler`, `ingestion`, `rag`, `resolve`, `jobs`, `backups`, `settings`, `ai_settings`, `metrics`,
`telemetry`, `admin`, `logs`, `orgs`, `org_members`, `org_invitations`, `invitations`,
`project_members`, `session`, and `setup`. All are described in the OpenAPI schema.

A few prefixes are worth calling out because they do not match the router name. The `crawler` router
is mounted under `/sources` (for example `/api/sources/...`), not `/crawler`. The `org_invitations`
router serves org-scoped invitations under `/api/orgs/{slug}/invitations`, while the separate
`invitations` router serves invitation acceptance under `/api/invitations`. The `ingestion` router
(prefix `/ingestion`) backs the `sibyl ingest` CLI with paths under `/api/ingestion/imports`,
`/api/ingestion/documents`, and `/api/ingestion/collections`.

**OpenAPI Spec:** Available at `/api/docs` (Swagger UI) and `/api/openapi.json`

## Authentication & Authorization

Sibyl supports multiple authentication methods and role-based access control:

| Topic          | Description                     | Documentation                                    |
| -------------- | ------------------------------- | ------------------------------------------------ |
| JWT Sessions   | Web clients, browser-based apps | [auth-jwt.md](./auth-jwt.md)                     |
| API Keys       | Programmatic access, CI/CD      | [auth-api-keys.md](./auth-api-keys.md)           |
| OAuth (GitHub) | Social login                    | [auth-jwt.md](./auth-jwt.md)                     |
| Authorization  | Roles, permissions, RLS         | [auth-authorization.md](./auth-authorization.md) |

### Quick Start

**For REST API:**

```bash
# Using JWT token (from login)
curl -H "Authorization: Bearer $ACCESS_TOKEN" \
  http://localhost:3334/api/entities

# Using API key
curl -H "Authorization: Bearer sk_live_abc123..." \
  http://localhost:3334/api/entities
```

**For MCP:**

```bash
# API key with mcp scope
curl -X POST http://localhost:3334/mcp \
  -H "Authorization: Bearer sk_live_abc123..." \
  -H "Content-Type: application/json" \
  -d '{"method": "tools/call", "params": {"name": "search", "arguments": {"query": "OAuth patterns"}}}'
```

## Multi-Tenancy

Sibyl is multi-tenant by design, with separate isolation models for graph memory and shared runtime
tables. Each organization gets a dedicated graph namespace (`org_<uuid_hex>`). Content and auth
records live in shared SurrealDB namespaces and are scoped by `organization_id`, table permissions,
and the API policy layer.

- Isolated SurrealDB graph namespace per organization
- Org-scoped content and auth records in shared namespaces
- Scoped API and MCP access

**Organization Context:**

- JWT tokens include `org` claim with organization ID
- API keys are scoped to specific organizations
- Graph queries route to the resolved organization namespace
- Content and auth queries carry explicit organization predicates

## Rate Limiting

REST endpoints are rate-limited using SlowAPI:

| Tier    | Limit               |
| ------- | ------------------- |
| Default | 100 requests/minute |
| Search  | 30 requests/minute  |
| Auth    | 5 requests/minute   |
| Crawl   | 10 requests/minute  |

Rate limit headers are included in responses:

```
X-RateLimit-Limit: 100
X-RateLimit-Remaining: 95
X-RateLimit-Reset: 1704067200
```

## WebSocket Events

Real-time updates are available via WebSocket at `/ws`:

```javascript
const ws = new WebSocket("ws://localhost:3334/ws?token=YOUR_TOKEN");

ws.onmessage = (event) => {
  const data = JSON.parse(event.data);
  // Event types: entity_created, entity_updated, entity_deleted,
  // crawl_started, crawl_progress, crawl_complete, etc.
};
```

## Error Responses

All errors follow a consistent envelope. A global exception handler returns a structured JSON body
and echoes the correlation ID in an `X-Request-ID` response header:

```json
{
  "error": "not_found",
  "message": "The requested resource was not found.",
  "request_id": "req_a1b2c3d4e5f6",
  "remediation": "Check the ID or prefix and try again.",
  "details": {
    "field": "task_id"
  }
}
```

The `error` field is a stable machine-readable code (for example `authentication_required`,
`forbidden`, `not_found`, `conflict`, `validation_error`, `rate_limited`, `internal_error`).
`remediation` is a short hint for resolving the error, and `details` is optional and only present
when the handler has safe, structured context to share.

| Status Code | Meaning                                           |
| ----------- | ------------------------------------------------- |
| 400         | Bad Request - Invalid parameters                  |
| 401         | Unauthorized - Missing or invalid credentials     |
| 403         | Forbidden - Insufficient permissions              |
| 404         | Not Found - Resource doesn't exist                |
| 409         | Conflict - Resource locked or concurrent update   |
| 422         | Validation Error - Request body validation failed |
| 429         | Too Many Requests - Rate limit exceeded           |
| 500         | Internal Server Error                             |

## Configuration

### Required Environment Variables

```bash
SIBYL_OPENAI_API_KEY=sk-...       # For embeddings
SIBYL_JWT_SECRET=...              # For authentication
```

### Optional Configuration

```bash
SIBYL_LOG_LEVEL=INFO
SIBYL_EMBEDDING_MODEL=text-embedding-3-small
SIBYL_MCP_AUTH_MODE=auto  # auto, on, or off
```

## Next Steps

- [MCP Search Tool](./mcp-search.md) - Start with semantic search
- [MCP Context Tool](./mcp-context.md) - Compile context packs for agents
- [MCP Synthesis Tools](./mcp-synthesis.md) - Source-grounded artifacts
- [REST Entities API](./rest-entities.md) - CRUD operations
- [JWT Authentication](./auth-jwt.md) - Set up authentication
- [Authorization](./auth-authorization.md) - Roles, scopes, and isolation
