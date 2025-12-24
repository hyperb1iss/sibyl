# Sibyl Development Guide

## Project Overview

**Sibyl** is a Graph-RAG Knowledge Oracle - an MCP server that provides AI agents access to development wisdom through a Graphiti-powered knowledge graph stored in FalkorDB.

**Stack:**
- Backend: Python 3.11+ / FastMCP / FastAPI / Graphiti / FalkorDB
- Frontend: Next.js 16 / React 19 / React Query / Tailwind 4
- Package Managers: `uv` (Python), `pnpm` (TypeScript)

---

## Sibyl Integration

**This project uses Sibyl as its own knowledge repository.**

### ALWAYS Use Skills

**Use `/sibyl-knowledge` and `/sibyl-project-manager` skills** for ALL Sibyl operations. These skills know the correct patterns and handle authentication properly.

- `/sibyl-knowledge` - Search, explore, add knowledge, manage tasks
- `/sibyl-project-manager` - Project audits, task triage, sprint planning

**Never call Sibyl MCP tools or CLI directly** without going through a skill first. The skills ensure proper org context and prevent the "wrong graph" problem.

### Research → Do → Reflect Cycle

Every significant task follows this cycle:

**1. RESEARCH** (before coding)
```
/sibyl-knowledge search "topic"
/sibyl-knowledge explore patterns
```
Find existing patterns, gotchas, past solutions. Don't reinvent wheels.

**2. DO** (while coding)
```
/sibyl-knowledge task start <id>
# ... implement with task context ...
```
Work is tracked. Status updates flow through skills.

**3. REFLECT** (after completing)
```
/sibyl-knowledge task complete <id> --learnings "What I learned"
/sibyl-knowledge add "Pattern Title" "What, why, how, caveats"
```
Capture insights. The graph gets smarter every session.

### When to Use Sibyl

**Always use for:**
- Multi-file features or refactors
- Debugging non-obvious issues
- Architectural decisions
- Work spanning multiple sessions

**Skip for:**
- Quick fixes, typos, simple tweaks
- Single-file changes with clear scope
- Tasks completable in < 5 minutes

---

## Architecture

### Server Architecture

```
Sibyl Combined App (Starlette, port 3334)
├── /api/*  → FastAPI REST (CRUD, search, admin)
├── /mcp    → MCP streamable-http (4 tools for agents)
└── Lifespan → Background queue + MCP session manager
```

### Backend Layers

| Layer | Location | Purpose |
|-------|----------|---------|
| MCP Server | `server.py` | 4-tool surface (search/explore/add/manage) |
| Tools | `tools/*.py` | Tool implementations |
| Graph | `graph/*.py` | FalkorDB client, entity/relationship managers |
| Models | `models/*.py` | Pydantic entities, tasks, sources |
| API | `api/*.py` | FastAPI REST routes |
| CLI | `cli/*.py` | Typer commands with Rich output |

### Frontend Structure

```
web/src/
├── app/           # Next.js 16 app router (SSR + client hydration)
├── components/    # 30+ components (ui/, layout/, domain-specific)
├── lib/           # API clients, hooks, constants
│   ├── api.ts         # Client-side fetch wrapper
│   ├── api-server.ts  # Server-side with caching
│   ├── hooks.ts       # 21 React Query hooks
│   └── websocket.ts   # Real-time updates
└── app/globals.css    # SilkCircuit design system
```

---

## Key Patterns

### FalkorDB Write Serialization

**Critical:** FalkorDB crashes with concurrent writes on a single connection.

```python
# GraphClient uses semaphore for all writes
async with self._write_semaphore:
    await self.client.driver.execute_query(...)

# Environment variable controls Graphiti concurrency
SEMAPHORE_LIMIT=10  # Default was 20, reduced for stability
```

### Entity Creation Dual Path

```python
# Path 1: LLM-powered extraction (slower, richer)
await entity_manager.create(entity)  # Uses Graphiti add_episode

# Path 2: Direct insertion (faster, structured data)
await entity_manager.create_direct(entity)  # Direct Cypher MERGE
```

### Node Labels

Graphiti creates two types of nodes:
- `Episodic` - Created by `add_episode()`, has `entity_type` property
- `Entity` - Extracted entities, may not have `entity_type`

**Queries must handle both:**
```cypher
MATCH (n)
WHERE (n:Episodic OR n:Entity) AND n.entity_type = $type
```

### Async Patterns

```python
# CLI commands use @run_async decorator
@app.command()
def my_command():
    @run_async
    async def _impl():
        client = await get_graph_client()
        # async work...
    _impl()

# Resilience with retry
@retry(config=GRAPH_RETRY)  # 3 attempts, exponential backoff
async def graph_operation():
    ...
```

### React Query + WebSocket

```typescript
// Server-side fetch with cache tags
const data = await serverFetch<Stats>('/admin/stats', {
  next: { revalidate: 60, tags: ['stats'] }
});

// Client hydration with React Query
const { data } = useStats(initialData);

// WebSocket invalidates on changes
wsClient.on('entity_created', () => {
  queryClient.invalidateQueries({ queryKey: queryKeys.entities.all });
});
```

---

## SilkCircuit Design System

### Color Palette

```css
/* Core Neon Palette */
--sc-purple: #e135ff;    /* Keywords, primary actions, importance */
--sc-cyan: #80ffea;      /* Functions, highlights, interactions */
--sc-coral: #ff6ac1;     /* Data, secondary, hashes */
--sc-yellow: #f1fa8c;    /* Warnings, attention, timestamps */
--sc-green: #50fa7b;     /* Success, confirmations */
--sc-red: #ff6363;       /* Errors, danger */

/* Background Hierarchy */
--sc-bg-dark: #0a0812;      /* Main background */
--sc-bg-base: #12101a;      /* Cards, elevated */
--sc-bg-highlight: #1a162a; /* Hover states */
--sc-bg-elevated: #221e30;  /* Modals, dropdowns */
```

### CLI Colors (Python)

```python
from sibyl.cli.common import (
    ELECTRIC_PURPLE,  # "#e135ff"
    NEON_CYAN,        # "#80ffea"
    CORAL,            # "#ff6ac1"
    ELECTRIC_YELLOW,  # "#f1fa8c"
    SUCCESS_GREEN,    # "#50fa7b"
    ERROR_RED,        # "#ff6363"
)

# Message helpers
success("Done!")      # Green checkmark
error("Failed!")      # Red X
warn("Caution!")      # Yellow warning
info("Note:")         # Cyan arrow
```

### Semantic Usage

| Element | Color |
|---------|-------|
| Titles, headers | ELECTRIC_PURPLE |
| Borders, tables | NEON_CYAN |
| IDs, hashes | CORAL |
| Status: doing | ELECTRIC_PURPLE |
| Status: todo | NEON_CYAN |
| Status: done | SUCCESS_GREEN |
| Status: blocked | ERROR_RED |
| Warnings | ELECTRIC_YELLOW |

---

## Data Models

### Entity Hierarchy

```
Entity (base)
├── Knowledge: Pattern, Rule, Template, Tool, Language, Topic, Episode
├── Tasks: Task, Project, Team, Milestone, ErrorPattern
└── Docs: Source, Document, Community
```

### Task Model

```python
class Task(Entity):
    project_id: str      # Required - tasks must belong to a project
    status: TaskStatus   # backlog→todo→doing→blocked/review→done→archived
    priority: TaskPriority  # critical, high, medium, low, someday
    complexity: TaskComplexity  # trivial, simple, medium, complex, epic
    task_order: int      # Higher = more important
    feature: str | None  # Feature area grouping
    learnings: str       # Captured after completion
```

### Relationships (22 types)

**Knowledge:** APPLIES_TO, REQUIRES, CONFLICTS_WITH, SUPERSEDES, ENABLES, BREAKS
**Tasks:** BELONGS_TO, DEPENDS_ON, BLOCKS, ASSIGNED_TO, REFERENCES, ENCOUNTERED
**Docs:** CRAWLED_FROM, CHILD_OF, MENTIONS

---

## Development Commands

### Python Backend

```bash
just lint          # ruff check + pyright
just fix           # ruff fix + format
just test          # pytest
just serve         # Start server on :3334

sibyl stats          # Graph statistics
sibyl task list      # List tasks
sibyl db backup      # Backup graph
```

### Frontend

```bash
cd web
pnpm dev           # Start on :3337
pnpm build         # Production build
pnpm lint          # Biome lint
```

**Dev Server:** Runs on port **3337** (Next.js 16)

**Browser Automation:** Use the `next-devtools` MCP for UI testing and automation:
```typescript
// Evaluate in browser context
mcp__next-devtools__browser_eval({ code: "document.title", port: 3337 })

// Initialize Next.js DevTools context
mcp__next-devtools__init({ project_path: "/Users/bliss/dev/sibyl/web" })
```

### Docker

```bash
docker compose up -d                    # Start FalkorDB

# List available graphs (each org has its own graph, named by org UUID)
docker exec sibyl-falkordb redis-cli -a conventions GRAPH.LIST

# Query a specific org's graph (replace <org-uuid> with actual org ID)
docker exec sibyl-falkordb redis-cli -a conventions GRAPH.QUERY <org-uuid> "MATCH (n) RETURN count(n)"
```

---

## Testing

### Test Harness

```python
from tests.harness import (
    ToolTestContext,      # Patches all tool dependencies
    MockEntityManager,    # In-memory entity store
    create_test_entity,   # Factory for test entities
)

async def test_search():
    ctx = ToolTestContext()
    ctx.entity_manager.set_search_results([(entity, 0.9)])

    async with ctx.patch():
        result = await search("query")
        assert result.total >= 0
```

### Running Tests

```bash
just test                           # All tests
just test tests/test_models.py      # Specific file
just test -k "test_task"            # Pattern match
just test -m integration            # Integration tests only
```

---

## Common Gotchas

### FalkorDB

- **Port 6380** (not 6379) to avoid Redis conflicts
- **Multi-tenant graphs**: Each org has its own graph, named by org UUID
- **Graph corruption** can cause crashes - nuke with `GRAPH.DELETE <org-uuid>`
- **Connection drops** under load - ensure SEMAPHORE_LIMIT is set
- **Organization required**: All graph operations require org context - no defaults

### Graphiti

- `add_episode()` creates `Episodic` nodes, not `Entity` nodes
- `EntityNode.get_by_uuids()` only finds `Entity` labeled nodes
- Always query both labels: `WHERE (n:Episodic OR n:Entity)`

### Next.js

- Server components are default - add `'use client'` only when needed
- API rewrites: `/api/*` proxies to backend `:3334`
- React Query needs hydration boundary for SSR data
- **Middleware is `proxy.ts`** (not `middleware.ts`) - Next.js 16 renamed it

### Environment

- Python env vars use `SIBYL_` prefix
- Graphiti expects `OPENAI_API_KEY` (set by GraphClient from SIBYL_OPENAI_API_KEY)
- Load `.env` BEFORE importing graphiti to ensure SEMAPHORE_LIMIT is set

---

## File Reference

### Critical Backend Files

| File | Purpose |
|------|---------|
| `main.py` | Server entry, combined app factory |
| `server.py` | MCP tool registration |
| `graph/client.py` | FalkorDB connection + write lock |
| `graph/entities.py` | Entity CRUD + search |
| `tools/core.py` | search/explore/add implementations |
| `tools/manage.py` | Task workflow actions |
| `config.py` | Settings from environment |

### Critical Frontend Files

| File | Purpose |
|------|---------|
| `app/layout.tsx` | Root layout + providers |
| `lib/api.ts` | Client-side API |
| `lib/hooks.ts` | React Query hooks |
| `lib/websocket.ts` | Real-time updates |
| `lib/constants.ts` | Colors, entity configs |
| `app/globals.css` | SilkCircuit design tokens |

---

## Task Workflow

When working on Sibyl itself:

1. **Check current tasks:** `sibyl task list --doing`
2. **Start a task:** `sibyl task start <id>`
3. **Search for context:** Query Sibyl for relevant patterns
4. **Implement** following patterns in this guide
5. **Complete with learnings:** `sibyl task complete <id> --learnings "..."`
6. **Capture new knowledge:** Add episodes for gotchas discovered

---

## Quick Reference

```bash
# Start everything
sibyl up &              # Starts FalkorDB + API server
cd web && pnpm dev

# Check status
sibyl stats
sibyl health

# Common operations
sibyl search "authentication"
sibyl task list --todo
sibyl entity list --type pattern

# Debug FalkorDB (list graphs, then query by org UUID)
docker exec sibyl-falkordb redis-cli -a conventions GRAPH.LIST
docker exec sibyl-falkordb redis-cli -a conventions GRAPH.QUERY <org-uuid> "MATCH (n) RETURN labels(n), count(*)"
```
