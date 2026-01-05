# Sibyl API Reference

Sibyl provides a dual-interface API: a 4-tool MCP interface for AI agents and a full REST API for
applications and integrations.

## Architecture Overview

```
Sibyl Combined App (Starlette, port 3334)
|-- /api/*    --> FastAPI REST endpoints
|-- /mcp      --> MCP streamable-http (4 tools)
|-- /ws       --> WebSocket for real-time updates
'-- Lifespan  --> Background queue + session management
```

## Base URL

| Environment       | Base URL                      |
| ----------------- | ----------------------------- |
| Local Development | `http://localhost:3334`       |
| Production        | `https://api.your-domain.com` |

## API Interfaces

### MCP Tools (for AI Agents)

The MCP interface exposes 4 consolidated tools that cover all Sibyl operations:

| Tool      | Purpose                                              | Documentation                      |
| --------- | ---------------------------------------------------- | ---------------------------------- |
| `search`  | Semantic search across knowledge graph and documents | [mcp-search.md](./mcp-search.md)   |
| `explore` | Navigate and browse graph structure                  | [mcp-explore.md](./mcp-explore.md) |
| `add`     | Create new knowledge entities                        | [mcp-add.md](./mcp-add.md)         |
| `manage`  | Lifecycle operations and administration              | [mcp-manage.md](./mcp-manage.md)   |

**MCP Endpoint:** `POST /mcp` (streamable-http transport)

### REST API (for Applications)

Full CRUD operations with OpenAPI documentation:

| Category | Endpoints                           | Documentation                          |
| -------- | ----------------------------------- | -------------------------------------- |
| Entities | `/api/entities/*`                   | [rest-entities.md](./rest-entities.md) |
| Tasks    | `/api/tasks/*`                      | [rest-tasks.md](./rest-tasks.md)       |
| Projects | `/api/entities?entity_type=project` | [rest-entities.md](./rest-entities.md) |
| Search   | `/api/search`                       | [rest-search.md](./rest-search.md)     |

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

Sibyl is multi-tenant by design. Each organization gets:

- Isolated FalkorDB graph (named by org UUID)
- Separate PostgreSQL data (crawled documents, users)
- Scoped API access

**Organization Context:**

- JWT tokens include `org` claim with organization ID
- API keys are scoped to specific organizations
- All queries automatically filter by organization

## Rate Limiting

REST endpoints are rate-limited using SlowAPI:

| Tier             | Limit               |
| ---------------- | ------------------- |
| Default          | 100 requests/minute |
| Search           | 30 requests/minute  |
| Write operations | 60 requests/minute  |

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
- [REST Entities API](./rest-entities.md) - CRUD operations
- [JWT Authentication](./auth-jwt.md) - Set up authentication
