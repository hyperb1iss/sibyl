# Sibyl API Server

FastAPI + MCP server providing the backend for Sibyl's knowledge graph, task workflows, search, and real-time updates.

## Quick Reference

```bash
# Start server
moon run api:serve        # or: uv run sibyld serve

# Start worker (for background jobs)
moon run api:worker       # or: uv run sibyld worker

# Quality checks
moon run api:test         # Run tests
moon run api:lint         # Lint
moon run api:typecheck    # Type check
```

## What's Here

- **MCP Server** — 4-tool API for search, exploration, capture, and task management
- **REST API** — Full CRUD for entities, tasks, projects, and sources
- **Auth System** — JWT, OAuth (GitHub), API keys, RBAC
- **Background Jobs** — arq workers for crawling and maintenance
- **WebSocket** — Real-time updates for entities and tasks

## Architecture

```
Sibyl Combined App (port 3334)
├── /api/*    → FastAPI REST endpoints
├── /mcp      → MCP streamable-http (4 tools)
├── /ws       → WebSocket for real-time updates
└── Lifespan  → Background queue + session management
```

## Key Directories

| Directory | Purpose |
|-----------|---------|
| `api/routes/` | REST endpoints (tasks, entities, auth, sources, admin) |
| `auth/` | JWT, sessions, API keys, RBAC, RLS |
| `crawler/` | Documentation ingestion pipeline |
| `jobs/` | Background job definitions |
| `db/` | SQLModel + Alembic migrations |

## Configuration

**Required:**
```bash
SIBYL_OPENAI_API_KEY=sk-...       # Embeddings
SIBYL_JWT_SECRET=...              # Auth
```

**Optional:**
```bash
SIBYL_ANTHROPIC_API_KEY=...       # Optional model-powered extraction
SIBYL_DATABASE_URL=...            # PostgreSQL
SIBYL_FALKORDB_HOST=...           # Graph DB
SIBYL_REDIS_URL=...               # Queue + pub/sub
```

## CLI Commands

```bash
sibyld serve              # Start HTTP server
sibyld serve -t stdio     # Start stdio server (for MCP subprocess)
sibyld worker             # Start background worker
sibyld up                 # Start all services (Supabase-style)
sibyld down               # Stop all services
sibyld db migrate         # Run migrations
sibyld db nuke            # Delete all data (dangerous!)
sibyld generate quick     # Generate sample data
```

## Key Patterns

**Multi-tenancy:** Every operation requires org context
```python
manager = EntityManager(client, group_id=str(org.id))
```

**Write concurrency:** FalkorDB writes use semaphore
```python
async with client.write_lock:
    await client.execute_write_org(org_id, query, **params)
```

**Request context:** Auth middleware injects user/org
```python
from sibyl.auth.context import get_current_user, get_current_org
```

## Dependencies

Depends on `sibyl-core` for models, graph client, and tool implementations.
