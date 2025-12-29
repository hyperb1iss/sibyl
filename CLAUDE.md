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

**Use `/sibyl-knowledge` and `/sibyl-project-manager` skills** for ALL Sibyl operations. These
skills know the correct patterns and handle authentication properly.

- `/sibyl-knowledge` - Search, explore, add knowledge, manage tasks
- `/sibyl-project-manager` - Project audits, task triage, sprint planning

**Never call Sibyl MCP tools or CLI directly** without going through a skill first.

### Research → Do → Reflect Cycle

Every significant task follows this cycle:

**1. RESEARCH** (before coding)

```
/sibyl-knowledge search "topic"
/sibyl-knowledge explore patterns
```

**2. DO** (while coding)

```
/sibyl-knowledge task start <id>
```

**3. REFLECT** (after completing)

```
/sibyl-knowledge task complete <id> --learnings "What I learned"
/sibyl-knowledge add "Pattern Title" "What, why, how, caveats"
```

---

## Quick Reference

### Monorepo Structure

```
sibyl/
├── apps/
│   ├── api/              # sibyld - Server daemon (serve, worker, db)
│   ├── cli/              # sibyl - Client CLI (task, search, add, etc.)
│   └── web/              # Next.js 16 frontend
├── packages/python/
│   └── sibyl-core/       # Shared library (models, graph, tools)
├── skills/               # Claude Code skills
└── charts/               # Helm charts
```

### CLI Executables

| Binary   | Package    | Purpose                                    |
| -------- | ---------- | ------------------------------------------ |
| `sibyld` | `apps/api` | Server daemon (serve, worker, db, up/down) |
| `sibyl`  | `apps/cli` | Client CLI (task, search, add, explore)    |

### Development Commands

```bash
moon run dev              # Start everything (FalkorDB, API, worker, web)
moon run stop             # Stop all services

moon run api:test         # Run API tests
moon run api:lint         # Lint API
moon run web:dev          # Start frontend only
moon run core:check       # Lint + typecheck + test core

# Installation
moon run install-dev      # Install everything (sibyl, sibyld, skills) - editable
moon run install          # Install everything (production)
```

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

- Run from workspace root unless working on isolated package
- `uv sync` at root syncs all Python deps
- `moon run` commands for orchestration

---

## Task Workflow

When working on Sibyl itself:

1. **Run `/sibyl-knowledge`** at session start
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
