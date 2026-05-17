# Sibyl API Reference

Sibyl provides a dual-interface API: an eleven-tool MCP interface for assistant clients and
automation, plus a full REST API for applications and integrations. Both surfaces are served by the
same daemon (`sibyld`) and share one SurrealDB-native runtime for graph, content, and auth.

## Architecture Overview

```
Sibyl Combined App (Starlette, port 3334)
|-- /api/*    --> FastAPI REST endpoints (26 routers)
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

The MCP interface exposes eleven tools that cover discovery, context, capture, synthesis, and
administration. Tools are registered in `apps/api/src/sibyl/server.py`.

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
| `manage`           | Lifecycle operations and administration                      | [mcp-manage.md](./mcp-manage.md)       |
| `logs`             | Recent server logs (OWNER role)                              | [mcp-logs.md](./mcp-logs.md)           |

**MCP Endpoint:** `POST /mcp` (streamable-http transport)

The MCP server also exposes two resources: `sibyl://health` (connectivity and entity counts) and
`sibyl://stats` (knowledge graph statistics).

### REST API (for Applications)

The REST API spans 26 routers. The pages below cover the most commonly used surfaces; the full
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
`crawler`, `rag`, `resolve`, `jobs`, `backups`, `settings`, `metrics`, `admin`, `logs`, `orgs`,
`org_members`, `org_invitations`, `project_members`, `session`, and `setup`. All are described in
the OpenAPI schema.

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
  https://api.example.com/api/entities

# Using API key
curl -H "Authorization: Bearer sk_live_abc123..." \
  https://api.example.com/api/entities
```

**For MCP:**

```bash
# API key with mcp scope
curl -X POST https://api.example.com/mcp \
  -H "Authorization: Bearer sk_live_abc123..." \
  -H "Content-Type: application/json" \
  -d '{"method": "tools/call", "params": {"name": "search", "arguments": {"query": "OAuth patterns"}}}'
```

## Multi-Tenancy

Sibyl is multi-tenant by design. Each organization gets a dedicated SurrealDB namespace
(`org_<uuid_hex>`) that holds its graph, content, and auth records. Forgetting org context routes
queries to the wrong namespace, so every authenticated request resolves an organization first.

- Isolated SurrealDB namespace per organization
- Scoped content, auth, and graph records
- Scoped API and MCP access

**Organization Context:**

- JWT tokens include `org` claim with organization ID
- API keys are scoped to specific organizations
- All queries automatically filter by organization

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
const ws = new WebSocket("wss://api.example.com/ws?token=YOUR_TOKEN");

ws.onmessage = (event) => {
  const data = JSON.parse(event.data);
  // Event types: entity_created, entity_updated, entity_deleted,
  // crawl_started, crawl_progress, crawl_complete, etc.
};
```

## Error Responses

All errors follow a consistent format:

```json
{
  "detail": "Human-readable error message"
}
```

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
