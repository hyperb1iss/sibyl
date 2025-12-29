# Sibyl API Server

FastAPI + MCP server providing the backend for Sibyl's knowledge graph operations.

## Overview

This package contains the server-side components:

- **MCP Server** — 4-tool surface for AI agents (search, explore, add, manage)
- **REST API** — Full CRUD for entities, tasks, projects, sources
- **Auth System** — JWT sessions, OAuth (GitHub), API keys, organizations
- **Background Jobs** — arq workers for async processing
- **CLI** — Server administration and direct database commands
- **Crawler** — Documentation ingestion pipeline

## Architecture

```
Sibyl Combined App (Starlette, port 3334)
├── /api/*    → FastAPI REST endpoints
├── /mcp      → MCP streamable-http (4 tools)
├── /ws       → WebSocket for real-time updates
└── Lifespan  → Background queue + session management
```

## Structure

```
src/sibyl/
├── main.py           # App factory, combined Starlette app
├── server.py         # MCP tool registration
├── config.py         # Server configuration (extends sibyl_core)
│
├── api/              # FastAPI REST
│   ├── app.py        # FastAPI app with routes
│   ├── routes/       # Route modules (entities, tasks, auth, etc.)
│   ├── schemas.py    # Request/response models
│   ├── websocket.py  # Real-time updates
│   └── rate_limit.py # SlowAPI rate limiting
│
├── auth/             # Authentication & authorization
│   ├── jwt.py        # Token creation/validation
│   ├── sessions.py   # Session management
│   ├── users.py      # User CRUD
│   ├── organizations.py  # Multi-tenancy
│   ├── api_keys.py   # Scoped API keys
│   ├── mcp_auth.py   # MCP-specific auth
│   └── middleware.py # Auth middleware
│
├── cli/              # Server CLI commands
│   ├── main.py       # Typer app entry
│   ├── db.py         # Database operations
│   ├── generate.py   # Test data generation
│   └── ...           # Other commands
│
├── crawler/          # Documentation ingestion
│   ├── pipeline.py   # Crawl orchestration
│   ├── chunker.py    # Content splitting
│   ├── embedder.py   # Vector embeddings
│   └── tagger.py     # Entity extraction
│
├── db/               # PostgreSQL (SQLModel + Alembic)
│   ├── connection.py # Async session factory
│   └── models.py     # SQLModel entities
│
├── jobs/             # Background processing
│   ├── worker.py     # arq worker settings
│   └── queue.py      # Job definitions
│
├── ingestion/        # Local file ingestion
│   ├── pipeline.py   # Ingestion orchestration
│   ├── parser.py     # File parsing
│   └── extractor.py  # Metadata extraction
│
└── generator/        # Test data generation
    ├── llm.py        # LLM-powered generation
    └── scenarios.py  # Scenario templates
```

## The 4-Tool MCP API

### search

Find entities by semantic similarity.

```python
search(
    query="OAuth implementation patterns",
    types=["pattern", "episode"],  # Filter by entity type
    status="doing",                # Filter tasks by status
    source="react-docs",           # Filter by source
    limit=20                       # Max results
)
```

### explore

Navigate the graph structure.

```python
# List entities
explore(mode="list", types=["project"])

# Find related entities
explore(mode="related", entity_id="pattern_abc")

# Get task dependencies
explore(mode="dependencies", entity_id="task_xyz")

# View communities
explore(mode="communities")
```

### add

Create new knowledge.

```python
# Add an episode (learning)
add(
    name="Redis connection pooling insight",
    content="Pool size must be >= concurrent requests...",
    category="debugging",
    technologies=["redis", "python"]
)

# Create a task
add(
    name="Implement OAuth flow",
    content="Add Google and GitHub OAuth...",
    entity_type="task",
    project="proj_auth",
    priority="high"
)
```

### manage

Lifecycle operations and administration.

```python
# Task workflow
manage("start_task", entity_id="task_abc")
manage("complete_task", entity_id="task_abc",
       data={"learnings": "Key insight..."})
manage("block_task", entity_id="task_abc",
       data={"reason": "Waiting on API access"})

# Crawling
manage("crawl", data={"url": "https://docs.example.com", "depth": 3})

# Admin
manage("health")
manage("stats")
```

## REST API

### Core Endpoints

| Endpoint                   | Method | Description                |
| -------------------------- | ------ | -------------------------- |
| `/api/entities`            | GET    | List entities with filters |
| `/api/entities/{id}`       | GET    | Get entity by ID           |
| `/api/entities`            | POST   | Create entity              |
| `/api/entities/{id}`       | PATCH  | Update entity              |
| `/api/entities/{id}`       | DELETE | Delete entity              |
| `/api/search`              | POST   | Semantic search            |
| `/api/tasks`               | GET    | List tasks with filters    |
| `/api/tasks/{id}/start`    | POST   | Start task                 |
| `/api/tasks/{id}/complete` | POST   | Complete task              |
| `/api/projects`            | GET    | List projects              |
| `/api/sources`             | GET    | List documentation sources |
| `/api/sources/{id}/crawl`  | POST   | Trigger crawl              |

### Auth Endpoints

| Endpoint                     | Method | Description           |
| ---------------------------- | ------ | --------------------- |
| `/api/auth/local/signup`     | POST   | Create local account  |
| `/api/auth/local/login`      | POST   | Login with email/pass |
| `/api/auth/github/authorize` | GET    | Start GitHub OAuth    |
| `/api/auth/github/callback`  | GET    | GitHub OAuth callback |
| `/api/auth/logout`           | POST   | End session           |
| `/api/auth/me`               | GET    | Current user info     |
| `/api/auth/api-keys`         | GET    | List API keys         |
| `/api/auth/api-keys`         | POST   | Create API key        |

### Admin Endpoints

| Endpoint            | Method | Description          |
| ------------------- | ------ | -------------------- |
| `/api/admin/stats`  | GET    | System statistics    |
| `/api/admin/health` | GET    | Health check         |
| `/api/admin/backup` | POST   | Trigger graph backup |

## Authentication

### JWT Sessions

Web clients authenticate via JWT stored in `sibyl_access_token` HTTP-only cookie.

```bash
# Required
SIBYL_JWT_SECRET=your-secret-key

# Optional
SIBYL_JWT_EXPIRY_HOURS=24
```

### OAuth (GitHub)

```bash
SIBYL_GITHUB_CLIENT_ID=...
SIBYL_GITHUB_CLIENT_SECRET=...
SIBYL_SERVER_URL=https://api.example.com     # For callbacks
SIBYL_FRONTEND_URL=https://app.example.com   # Post-login redirect
```

### API Keys

Scoped keys for programmatic access:

- `mcp` — Required for `/mcp` endpoint
- `api:read` — GET/HEAD/OPTIONS on `/api/*`
- `api:write` — All `/api/*` methods (implies read)

```bash
# Create via CLI
sibyl auth api-key create --name "CI/CD" --scopes mcp,api:read

# Or via API
curl -X POST /api/auth/api-keys \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"name": "CI/CD", "scopes": ["mcp", "api:read"]}'
```

### MCP Auth

```bash
# auto (default): enforce when SIBYL_JWT_SECRET is set
# on: always require auth
# off: disable auth (dev only)
SIBYL_MCP_AUTH_MODE=auto
```

## Configuration

### Required

```bash
SIBYL_OPENAI_API_KEY=sk-...       # For embeddings
SIBYL_JWT_SECRET=...              # For auth
```

### Database

```bash
# FalkorDB (graph)
SIBYL_FALKORDB_HOST=localhost
SIBYL_FALKORDB_PORT=6380
SIBYL_FALKORDB_PASSWORD=conventions

# PostgreSQL (relational)
SIBYL_DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/sibyl
```

### Optional

```bash
SIBYL_LOG_LEVEL=INFO
SIBYL_EMBEDDING_MODEL=text-embedding-3-small
SIBYL_ANTHROPIC_API_KEY=...       # For LLM operations
SIBYL_SERVER_URL=...              # Public URL
SIBYL_FRONTEND_URL=...            # Frontend URL
```

## Development

### moonrepo Tasks

```bash
moon run api:serve        # Start server
moon run api:worker       # Start background worker
moon run api:test         # Run tests
moon run api:lint         # Lint code
moon run api:typecheck    # Type check
moon run api:db-migrate   # Run migrations
```

### Direct Commands

```bash
# From apps/api/
uv run sibyl-serve                     # Start server (HTTP mode)
uv run sibyl-serve -t stdio            # Start server (stdio mode)
uv run arq sibyl.jobs.WorkerSettings   # Start worker

# Testing
uv run pytest
uv run pytest -k "test_auth"           # Filter tests
uv run pytest --cov=src                # With coverage

# Linting
uv run ruff check src tests
uv run ruff format src tests
uv run pyright
```

### Database Migrations

```bash
# Create migration
moon run api:db-revision -- -m "Add new column"

# Apply migrations
moon run api:db-migrate
```

## CLI Commands

The server CLI provides administration commands:

```bash
# Server
sibyl-serve                   # Start HTTP server
sibyl-serve -t stdio          # Start stdio server

# Database
sibyl db backup               # Backup graph
sibyl db restore <file>       # Restore from backup
sibyl db nuke                 # Delete all data (dangerous!)

# Test data
sibyl generate quick          # Generate sample data
sibyl generate stress         # Generate load test data

# Health
sibyl health                  # Check system health
sibyl stats                   # Show statistics
```

## Multi-Tenancy

Each organization gets its own isolated FalkorDB graph:

```python
# Graph named by org UUID
graph_name = str(org.id)  # e.g., "550e8400-e29b-41d4-a716-446655440000"

# All operations require org context
manager = EntityManager(client, group_id=str(org.id))
```

**Never query without org scope** — it will hit the wrong graph or break isolation.

## Key Patterns

### Write Concurrency

All FalkorDB writes use a semaphore to prevent corruption:

```python
async with client.write_lock:
    await client.execute_write_org(org_id, query, **params)
```

### Request Context

Auth middleware injects context available throughout request:

```python
from sibyl.auth.context import get_current_user, get_current_org

user = await get_current_user()
org = await get_current_org()
```

### Background Jobs

Long-running tasks use arq:

```python
from sibyl.jobs import queue

await queue.enqueue("crawl_source", source_id=source.id)
```

## Dependencies

This package depends on:

- `sibyl-core` — Models, graph client, tool implementations

It provides:

- Server runtime (FastAPI + MCP)
- Database layer (PostgreSQL + Alembic)
- Full auth system
- Background job processing
