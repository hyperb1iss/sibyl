# Sibyl API Server

`sibyld` is the FastAPI + FastMCP server behind Sibyl's knowledge graph, agent memory
loop, task workflow, search, synthesis, and real-time updates.

## Quick Reference

```bash
# Start server
moon run api:serve        # or: uv run sibyld serve

# Start worker (Redis coordination only)
moon run api:worker       # or: uv run sibyld worker

# Quality checks
moon run api:test         # Run tests
moon run api:lint         # Lint
moon run api:typecheck    # Type check
```

## What's Here

- **MCP Server:** eleven tools for search, context packs, exploration, capture,
  memory, synthesis, and management
- **REST API:** 26 routers covering entities, tasks, projects, memory, synthesis,
  sources, auth, settings, and admin
- **Auth System:** JWT sessions, GitHub OAuth, API keys with scopes, MCP OAuth
  clients, RBAC
- **Background Jobs:** in-process local runtime or Redis-backed `arq` workers,
  including the nightly reflection dream-cycle
- **WebSocket:** Real-time updates for entities and tasks

## Architecture

```
Sibyl API (port 3334)
├── /api/*              → FastAPI REST endpoints
├── /api/openapi.json   → OpenAPI schema
├── /mcp                → MCP server (streamable-http, 11 tools)
├── /api/ws             → WebSocket for real-time updates
└── Lifespan            → Background jobs + coordination broker
```

## Key Directories

| Directory       | Purpose                                                          |
| --------------- | ---------------------------------------------------------------- |
| `api/routes/`   | REST endpoints (26 routers: tasks, entities, memory, synthesis, crawler, auth, admin, and more) |
| `ai/`           | DB-backed LLM settings, model validation routes, runtime invalidation |
| `auth/`         | JWT, sessions, API keys, RBAC, MCP OAuth clients                 |
| `persistence/`  | SurrealDB-native runtimes for auth, content, graph, and backups  |
| `crawler/`      | Documentation crawl and ingestion pipeline                       |
| `ingestion/`    | Source import pipeline (mailbox and other adapters)              |
| `jobs/`         | Background jobs (reflection dream-cycle, crawl, backups)         |
| `coordination/` | Local and Redis brokers for jobs, locks, and pub/sub             |
| `email/`        | Transactional email delivery                                     |
| `generator/`    | Synthetic test-data generation                                   |

## Configuration

**Required:**

```bash
SIBYL_JWT_SECRET=...              # Auth (auto-generated in dev)
SIBYL_ANTHROPIC_API_KEY=...       # Required when LLM provider=anthropic
# SIBYL_OPENAI_API_KEY=sk-...     # Required when LLM provider=openai
# SIBYL_GEMINI_API_KEY=...        # Required when LLM provider=gemini

# Embeddings: choose OpenAI or Gemini
SIBYL_EMBEDDING_PROVIDER=openai   # openai | gemini
SIBYL_OPENAI_API_KEY=sk-...       # Required when embedding provider=openai
# SIBYL_GEMINI_API_KEY=...        # Required when embedding provider=gemini
```

**Optional:**

```bash
SIBYL_STORE=surreal                   # default; legacy is migration/source-side only
SIBYL_COORDINATION_BACKEND=auto       # auto | local | redis
SIBYL_SURREAL_URL=ws://127.0.0.1:8000/rpc
SIBYL_SURREAL_USERNAME=root
SIBYL_SURREAL_PASSWORD=root
SIBYL_REDIS_HOST=127.0.0.1            # only needed for Redis coordination
SIBYL_REDIS_PORT=6381
SIBYL_LLM_PROVIDER=anthropic          # anthropic | openai
SIBYL_LLM_MODEL=claude-haiku-4-5
SIBYL_LLM_CRAWLER_MODEL=claude-haiku-4-5
SIBYL_LLM_SYNTHESIS_MODEL=claude-sonnet-4-6
SIBYL_LLM_TEMPERATURE=0
SIBYL_LLM_TIMEOUT_SECONDS=60
SIBYL_EMBEDDING_MODEL=text-embedding-3-small
SIBYL_EMBEDDING_DIMENSIONS=1536
SIBYL_GRAPH_EMBEDDING_PROVIDER=openai
SIBYL_GRAPH_EMBEDDING_MODEL=text-embedding-3-small
SIBYL_GRAPH_EMBEDDING_DIMENSIONS=1024
```

PostgreSQL settings are only for historical archive rehearsal commands that explicitly
restore a retained `postgres.sql` payload against an operator-managed database. They are
not part of default Surreal runtime startup.

Gemini keys can also be supplied through `GEMINI_API_KEY` or `GOOGLE_API_KEY`. Changing
embedding provider, model, or dimensions changes vector spaces; re-crawl sources and
rebuild graph indexes before mixing old and new search results.

LLM settings are instance-wide. Environment variables win over database settings field
by field; env-backed fields return `409 LOCKED_BY_ENV` on write. Database settings are
managed under:

```text
GET  /api/settings/ai/llm
PUT  /api/settings/ai/llm/{surface}
POST /api/settings/ai/llm/{surface}/test
POST /api/settings/ai/keys/{provider}/test
POST /api/settings/ai/models/{model_alias}/test
GET  /api/settings/ai/registry?kind=llm
```

Crawler extraction and synthesis generation call `sibyl_core.ai` rather than provider
SDKs directly. Custom model IDs are accepted as database settings with an
`unverified_model` warning; validate them with the model test endpoint before using
them in production flows.

## CLI Commands

```bash
sibyld serve              # Start the HTTP server
sibyld serve -t stdio     # Start a stdio server (MCP subprocess mode)
sibyld worker             # Start the job worker (local mode exits cleanly)
sibyld up                 # Start data services + API
sibyld down               # Stop all services
sibyld db backup          # Back up the graph database
sibyld migrate import ... # Import a migration archive
sibyld generate realistic # Generate sample data
```

## Runtime Modes

For single-machine Surreal development, run `sibyld serve` or `sibyld up` with
`SIBYL_STORE=surreal`. The default `coordination_backend=auto` resolves to `local`, so
background jobs, pending state, locks, pub/sub, and schedules all stay in-process with
no Redis requirement.

Redis remains available for distributed or multi-process dev. Set
`SIBYL_COORDINATION_BACKEND=redis` when you want the `arq` worker model, then run
`sibyld worker` or `moon run api:worker` separately.

## Key Patterns

**Multi-tenancy:** Every operation requires org context.

```python
manager = EntityManager(client, group_id=str(org.id))
```

Write concurrency: the SurrealDB driver serializes WebSocket operations per client.
Clone graph drivers per organization rather than sharing one driver across org scopes.

**Request context:** Auth middleware injects user and org.

```python
from sibyl.auth.dependencies import get_current_user, get_current_organization
```

## Dependencies

Depends on `sibyl-core` for models, graph client, AI substrate, and tool
implementations.
