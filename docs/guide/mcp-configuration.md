---
title: MCP Configuration
description: Configuring Sibyl's MCP server
---

# MCP Configuration

This guide covers advanced configuration of Sibyl's MCP (Model Context Protocol) server.

## Server Architecture

Sibyl's MCP server is built on FastMCP and integrates with the main Starlette application:

```
Sibyl Combined App (Starlette, port 3334)
├── /api/*    -> FastAPI REST endpoints
├── /mcp      -> MCP streamable-http endpoint
├── /ws       -> WebSocket for real-time updates
└── Lifespan  -> Background queue + session management
```

## Transport Modes

### HTTP Mode (Default)

Sibyl runs as an HTTP server, accepting MCP requests at `/mcp`:

```bash
uv run sibyld serve
# Server listening on http://localhost:3334
# MCP endpoint: http://localhost:3334/mcp
```

Configuration:

```json
{
  "mcpServers": {
    "sibyl": {
      "type": "http",
      "url": "http://localhost:3334/mcp"
    }
  }
}
```

### Stdio Mode

Run Sibyl as a subprocess communicating via stdin/stdout:

```bash
uv run sibyld serve -t stdio
```

Configuration:

```json
{
  "mcpServers": {
    "sibyl": {
      "command": "uv",
      "args": ["--directory", "/path/to/sibyl/apps/api", "run", "sibyld", "serve", "-t", "stdio"],
      "env": {
        "SIBYL_OPENAI_API_KEY": "sk-...",
        "SIBYL_JWT_SECRET": "your-secret",
        "SIBYL_FALKORDB_HOST": "localhost",
        "SIBYL_FALKORDB_PORT": "6380"
      }
    }
  }
}
```

## Authentication Modes

### Auto Mode (Default)

Authentication is enabled when `SIBYL_JWT_SECRET` is set:

```bash
SIBYL_MCP_AUTH_MODE=auto  # Default
SIBYL_JWT_SECRET=your-secret  # If set, auth is enabled
```

### Forced On

Always require authentication:

```bash
SIBYL_MCP_AUTH_MODE=on
```

### Forced Off (Development Only)

Disable authentication:

```bash
SIBYL_MCP_AUTH_MODE=off
```

::: danger Production Warning Never disable authentication in production. Use `auto` or `on` mode.
:::

## OAuth Flow

When auth is enabled, Sibyl implements OAuth 2.0:

### OAuth Endpoints

| Endpoint        | Purpose                          |
| --------------- | -------------------------------- |
| `/_oauth/login` | Login form                       |
| `/_oauth/org`   | Organization selection           |
| `/mcp`          | Token-authenticated MCP endpoint |

### Token Acquisition

1. User visits `/_oauth/login`
2. Enters credentials
3. Selects organization
4. Receives access token
5. Token used for MCP requests

### API Key Authentication

For programmatic access, use API keys:

```bash
# Create API key
sibyl auth api-key create --name "MCP Client" --scopes mcp

# Use in configuration
{
  "mcpServers": {
    "sibyl": {
      "type": "http",
      "url": "http://localhost:3334/mcp",
      "headers": {
        "Authorization": "Bearer sk_xxx..."
      }
    }
  }
}
```

## Environment Variables

### Core Settings

| Variable            | Default     | Description                      |
| ------------------- | ----------- | -------------------------------- |
| `SIBYL_SERVER_HOST` | `localhost` | Host to bind to                  |
| `SIBYL_SERVER_PORT` | `3334`      | Port to listen on                |
| `SIBYL_SERVER_URL`  | -           | Public URL (for OAuth callbacks) |
| `SIBYL_SERVER_NAME` | `sibyl`     | Server name in MCP responses     |

### Authentication Settings

| Variable                            | Default | Description                            |
| ----------------------------------- | ------- | -------------------------------------- |
| `SIBYL_JWT_SECRET`                  | -       | JWT signing secret (required for auth) |
| `SIBYL_ACCESS_TOKEN_EXPIRE_MINUTES` | `60`    | Access token TTL in minutes            |
| `SIBYL_MCP_AUTH_MODE`               | `auto`  | Auth mode: auto, on, off               |

### GitHub OAuth (Optional)

| Variable                     | Description                |
| ---------------------------- | -------------------------- |
| `SIBYL_GITHUB_CLIENT_ID`     | GitHub OAuth app client ID |
| `SIBYL_GITHUB_CLIENT_SECRET` | GitHub OAuth app secret    |

### Database Settings

| Variable                  | Default       | Description                  |
| ------------------------- | ------------- | ---------------------------- |
| `SIBYL_FALKORDB_HOST`     | `localhost`   | FalkorDB host                |
| `SIBYL_FALKORDB_PORT`     | `6380`        | FalkorDB port                |
| `SIBYL_FALKORDB_PASSWORD` | `sibyl_dev`  | FalkorDB password            |
| `SIBYL_DATABASE_URL`      | -             | PostgreSQL connection string |

### Embedding Settings

| Variable                | Default                  | Description               |
| ----------------------- | ------------------------ | ------------------------- |
| `SIBYL_OPENAI_API_KEY`  | -                        | OpenAI API key (required) |
| `SIBYL_EMBEDDING_MODEL` | `text-embedding-3-small` | Embedding model           |

## Server Implementation

### Tool Registration

Tools are registered in `server.py`:

```python
mcp = FastMCP(
    settings.server_name,
    host=host,
    port=port,
    stateless_http=False,  # Maintain session state
    auth=auth_settings,
)

@mcp.tool()
async def search(...) -> dict:
    """Semantic search across knowledge graph."""
    org_id = await _require_org_id()
    return await _search(..., organization_id=org_id)
```

### Organization Context

Every tool extracts organization from the authenticated context:

```python
async def _require_org_id() -> str:
    """Require organization ID from MCP context."""
    org_id = await _get_org_id_from_context()
    if not org_id:
        raise ValueError("Organization context required")
    return org_id
```

### Resource Registration

MCP resources provide read-only data:

```python
@mcp.resource("sibyl://health")
async def health_resource() -> str:
    """Server health status."""
    health = await get_health()
    return json.dumps(health)

@mcp.resource("sibyl://stats")
async def stats_resource() -> str:
    """Graph statistics."""
    stats = await get_stats()
    return json.dumps(stats)
```

## API Scopes

API keys can have different scopes:

| Scope       | Permission                 |
| ----------- | -------------------------- |
| `mcp`       | Access MCP endpoint        |
| `api:read`  | REST GET/HEAD/OPTIONS      |
| `api:write` | REST writes (implies read) |

Create scoped keys:

```bash
# MCP only
sibyl auth api-key create --name "MCP" --scopes mcp

# MCP + read API
sibyl auth api-key create --name "CI/CD" --scopes mcp,api:read

# Full access
sibyl auth api-key create --name "Admin" --scopes mcp,api:write
```

## Load Balancing

For production deployments with multiple instances:

### Sticky Sessions

MCP sessions maintain state. Use sticky sessions if load balancing:

```nginx
upstream sibyl {
    ip_hash;  # Sticky sessions
    server sibyl1:3334;
    server sibyl2:3334;
}
```

### Shared State

For stateless scaling:

1. Use Redis for session storage
2. Ensure all instances share FalkorDB connection
3. Configure shared PostgreSQL

## Monitoring

### Health Check

```bash
curl http://localhost:3334/api/health
```

### MCP Status

Access via MCP resource:

```
sibyl://health
sibyl://stats
```

### Metrics Endpoint

```bash
curl http://localhost:3334/api/metrics
```

## Troubleshooting

### Connection Refused

1. Check server is running
2. Verify port isn't blocked
3. Check firewall settings

### Authentication Failed

1. Verify `SIBYL_JWT_SECRET` is set
2. Check API key is valid
3. Ensure token hasn't expired

### Organization Not Found

1. Check user belongs to organization
2. Verify org_id in token claims
3. Check organization exists

### Slow Responses

1. Check FalkorDB connection
2. Verify embedding API connectivity
3. Review query complexity

## Example Configurations

### Local Development

```json
{
  "mcpServers": {
    "sibyl": {
      "type": "http",
      "url": "http://localhost:3334/mcp"
    }
  }
}
```

### Production with Auth

```json
{
  "mcpServers": {
    "sibyl": {
      "type": "http",
      "url": "https://sibyl.example.com/mcp",
      "headers": {
        "Authorization": "Bearer sk_..."
      }
    }
  }
}
```

### Subprocess Mode

```json
{
  "mcpServers": {
    "sibyl": {
      "command": "uv",
      "args": ["--directory", "/opt/sibyl/apps/api", "run", "sibyld", "serve", "-t", "stdio"],
      "env": {
        "SIBYL_OPENAI_API_KEY": "sk-...",
        "SIBYL_JWT_SECRET": "...",
        "SIBYL_FALKORDB_HOST": "falkordb.internal",
        "SIBYL_DATABASE_URL": "postgresql+asyncpg://..."
      }
    }
  }
}
```

## Next Steps

- [Claude Code Integration](./claude-code.md) - Using MCP with Claude
- [Skills Development](./skills.md) - Create custom skills
- [Multi-Tenancy](./multi-tenancy.md) - Organization scoping
