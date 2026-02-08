# Sibyl Development Guide

## Project Overview

**Sibyl** is a Collective Intelligence Runtime - an MCP server providing AI agents shared memory,
task orchestration, and collaborative knowledge through a Graphiti-powered knowledge graph.

**See package READMEs for detailed documentation:**

- [`README.md`](README.md) — Project overview, quickstart, philosophy
- [`apps/api/README.md`](apps/api/README.md) — Server daemon (sibyld), MCP API, REST endpoints
- [`apps/cli/README.md`](apps/cli/README.md) — Client CLI (sibyl), user commands
- [`apps/web/README.md`](apps/web/README.md) — Web UI, components, React Query hooks
- [`packages/python/sibyl-core/README.md`](packages/python/sibyl-core/README.md) — Core library,
  models, graph client

---

## Sibyl Integration

**This project uses Sibyl as its own knowledge repository.**

### ALWAYS Use Skills

**Use `/sibyl`** for ALL Sibyl operations. This skill knows the correct patterns and handles
authentication properly.

- `/sibyl` - Search, explore, add knowledge, manage tasks, project audits, sprint planning

**Never call Sibyl MCP tools or CLI directly** without going through a skill first.

**Use `/uv`** before running any `uv` commands. The skill provides current best practices and
prevents common mistakes.

- **NEVER run `uv pip`** — it bypasses the project's dependency management. Use `uv add`, `uv sync`,
  or `uv run` instead.

### Research → Do → Reflect Cycle

Every significant task follows this cycle:

**1. RESEARCH** (before coding)

```
/sibyl search "topic"
/sibyl explore patterns
```

**2. DO** (while coding)

```
/sibyl task start <id>
```

**3. REFLECT** (after completing)

```
/sibyl task complete <id> --learnings "What I learned"
/sibyl add "Pattern Title" "What, why, how, caveats"
```

---

## Quick Reference

### Monorepo Structure

```
sibyl/
├── apps/
│   ├── api/              # sibyld - Server daemon (serve, worker, db)
│   ├── cli/              # sibyl - Client CLI (task, search, add, etc.)
│   ├── runner/           # sibyl-runner - Distributed execution daemon
│   └── web/              # Next.js 16 frontend
├── packages/python/
│   └── sibyl-core/       # Shared library (models, graph, tools)
├── skills/               # Claude Code skills
└── charts/               # Helm charts
```

### CLI Executables

| Binary         | Package       | Purpose                                    |
| -------------- | ------------- | ------------------------------------------ |
| `sibyld`       | `apps/api`    | Server daemon (serve, worker, db, up/down) |
| `sibyl`        | `apps/cli`    | Client CLI (task, search, add, explore)    |
| `sibyl-runner` | `apps/runner` | Distributed execution daemon               |

### Development Commands

**⚡ Always use `moon` for monorepo operations.** Moon handles task orchestration, caching, and
cross-package dependencies. Never use raw `pnpm`/`uv` commands for lint, test, build, or typecheck.

```bash
# Lifecycle
moon run dev              # Start everything (FalkorDB, API, worker, web)
moon run stop             # Stop all services

# Quality (from any directory)
moon run :lint            # Lint current project (or all if at root)
moon run :test            # Test current project
moon run :typecheck       # Typecheck current project
moon run :check           # All quality checks (lint + typecheck + test)

# Target specific packages
moon run web:lint         # Lint web app
moon run api:test         # Test API
moon run core:check       # Full check on sibyl-core

# Build & Install
moon run :build           # Build current project
moon run install-dev      # Install everything editable (sibyl, sibyld, skills)
moon run install          # Install everything (production)
```

**Why moon?** Caches results, runs only what changed, handles dependencies between packages. A bare
`pnpm lint` won't respect the monorepo graph and may miss cross-package issues.

### Dev Introspection Tools

**Use these when debugging Sibyl itself.** Requires OWNER role.

```bash
# System health at a glance
sibyl debug status              # API/worker/graph/queue health + recent errors

# Inspect the graph directly
sibyl debug schema              # Entity types and counts
sibyl debug query "MATCH ..."   # Run read-only Cypher queries

# Server logs
sibyl logs tail                 # Last 50 log entries
sibyl logs tail -n 100          # More entries
sibyl logs tail -l error        # Filter by level (debug/info/warning/error)
sibyl logs tail -s api          # Filter by service (api/worker)
sibyl logs tail -f              # Stream in real-time (Ctrl+C to stop)

# JSON output for scripting
sibyl debug status --json
sibyl logs tail --json
```

**When to use:**

- Tests failing mysteriously → `sibyl logs tail -l error`
- Graph queries returning unexpected results → `sibyl debug query "MATCH ..."`
- Need to understand entity distribution → `sibyl debug schema`
- Something feels broken → `sibyl debug status`

### Ports

| Service   | Port |
| --------- | ---- |
| API + MCP | 3334 |
| Frontend  | 3337 |
| FalkorDB  | 6380 |

---

## Key Patterns

### Multi-Tenancy

**Every graph operation requires org context - NO defaults:**

```python
manager = EntityManager(client, group_id=str(org.id))
```

Each organization gets its own isolated FalkorDB graph. Forgetting org scope queries the wrong graph
or breaks isolation.

### FalkorDB Write Concurrency

All writes use a semaphore to prevent corruption:

```python
async with client.write_lock:
    await client.execute_write_org(org_id, query, **params)
```

### Node Labels

Graphiti creates two node types:

- `Episodic` - Created by `add_episode()`
- `Entity` - Extracted entities

**Queries must handle both:**

```cypher
WHERE (n:Episodic OR n:Entity) AND n.entity_type = $type
```

### Package Imports

```python
# Core library
from sibyl_core.models import Task, Entity
from sibyl_core.graph import EntityManager

# Server-side (apps/api)
from sibyl.auth.context import get_current_user
from sibyl.cli.common import ELECTRIC_PURPLE
```

---

## Common Gotchas

### FalkorDB

- **Port 6380** (not 6379) to avoid Redis conflicts
- **Graph corruption** can crash - nuke with `GRAPH.DELETE <org-uuid>`
- **SEMAPHORE_LIMIT** must be set before importing graphiti

### Graphiti

- `add_episode()` creates `Episodic` nodes, not `Entity` nodes
- Always query both labels

### Next.js 16

- Server components are default - add `'use client'` only when needed
- Middleware file is `proxy.ts` (not `middleware.ts`)
- API rewrites: `/api/*` proxies to backend `:3334`

### Monorepo

- **Use `moon run` for everything** — lint, test, build, typecheck. No exceptions.
- **Run `/uv` skill first** before any uv commands to get current best practices
- **NEVER use `uv pip`** — always use `uv add`, `uv sync`, or `uv run` instead
- Run from workspace root unless working on isolated package
- `uv sync` at root syncs all Python deps
- Raw `pnpm`/`uv` commands bypass moon's caching and dependency graph

---

## Task Workflow

When working on Sibyl itself:

1. **Run `/sibyl`** at session start
2. **Check current tasks:** `sibyl task list --status doing`
3. **Start a task:** `sibyl task start <id>`
4. **Search for context:** Query Sibyl for relevant patterns
5. **Implement** following patterns in the READMEs
6. **Complete with learnings:** `sibyl task complete <id> --learnings "..."`
7. **Capture new knowledge:** Add patterns for gotchas discovered

---

## SilkCircuit Design System

```css
--sc-purple: #e135ff; /* Primary, importance */
--sc-cyan: #80ffea; /* Interactions */
--sc-coral: #ff6ac1; /* Secondary, data */
--sc-yellow: #f1fa8c; /* Warnings */
--sc-green: #50fa7b; /* Success */
--sc-red: #ff6363; /* Errors */
```

See [`apps/web/README.md`](apps/web/README.md) for full design system documentation.
