<h1 align="center">
  <br>
  🔮 Sibyl
  <br>
</h1>

<p align="center">
  <strong>Collective Intelligence Runtime for AI Agents</strong>
</p>

<p align="center">
  <a href="#-what-sibyl-does">
    <img src="https://img.shields.io/badge/Knowledge-Graph_RAG-e135ff?style=for-the-badge&logo=graphql&logoColor=white" alt="Graph-RAG">
  </a>
  <a href="#-the-4-tool-api">
    <img src="https://img.shields.io/badge/Interface-4_Tools-80ffea?style=for-the-badge&logo=openai&logoColor=white" alt="4 Tools">
  </a>
  <a href="#-the-cli">
    <img src="https://img.shields.io/badge/CLI-First-ff6ac1?style=for-the-badge&logo=gnubash&logoColor=white" alt="CLI First">
  </a>
</p>

<p align="center">
  <a href="https://github.com/hyperb1iss/sibyl/actions/workflows/ci.yml">
    <img src="https://img.shields.io/github/actions/workflow/status/hyperb1iss/sibyl/ci.yml?branch=main&style=flat-square&logo=github&logoColor=white&label=CI" alt="CI Status">
  </a>
  <a href="https://github.com/hyperb1iss/sibyl/releases">
    <img src="https://img.shields.io/github/v/release/hyperb1iss/sibyl?style=flat-square&logo=github&logoColor=white" alt="Latest Release">
  </a>
  <a href="https://github.com/hyperb1iss/sibyl/blob/main/LICENSE">
    <img src="https://img.shields.io/github/license/hyperb1iss/sibyl?style=flat-square&logo=apache&logoColor=white" alt="License">
  </a>
</p>

<p align="center">
  <a href="#-what-sibyl-does">What It Does</a> •
  <a href="#-quickstart">Quickstart</a> •
  <a href="#-monorepo-structure">Structure</a> •
  <a href="#-the-cli">CLI</a> •
  <a href="#-the-4-tool-api">API</a>
</p>

---

> **AI Agents:** Sibyl is your extended memory. Search before implementing. Capture what you learn.
> Track your work. The graph should be smarter after every session.

## 💎 What Sibyl Does

Sibyl transforms scattered development knowledge into a queryable graph. Patterns, learnings, tasks,
documentation—all connected, all searchable by meaning.

| Capability            | What You Get                                   |
| --------------------- | ---------------------------------------------- |
| **Semantic Search**   | Find knowledge by meaning, not keywords        |
| **Task Tracking**     | Full workflow with status, blockers, learnings |
| **Auto-Linking**      | New knowledge connects to related entities     |
| **Graph Traversal**   | Navigate relationships to discover connections |
| **Doc Ingestion**     | Crawl and index external documentation         |
| **Multi-Tenancy**     | Isolated graphs per organization               |
| **Persistent Memory** | What you learn today helps tomorrow            |

## 🏗️ Monorepo Structure

```
sibyl/
├── apps/
│   ├── api/              # FastAPI + MCP server
│   └── web/              # Next.js 16 frontend
├── packages/python/
│   ├── sibyl-core/       # Shared library (models, graph, tools)
│   └── sibyl-cli/        # REST API client CLI
├── skills/               # Claude Code skills
├── charts/               # Helm charts for K8s
└── infra/                # Local infrastructure
```

**Stack:**

- **Backend:** Python 3.13 / FastMCP / FastAPI / Graphiti / FalkorDB
- **Frontend:** Next.js 16 / React 19 / React Query / Tailwind 4
- **Build:** moonrepo + uv (Python) + pnpm (TypeScript)
- **Toolchain:** proto (version management)

See each package's README for detailed documentation:

- [`apps/api/README.md`](apps/api/README.md) — Server, MCP, REST API, auth
- [`apps/web/README.md`](apps/web/README.md) — Web UI, components, design system
- [`packages/python/sibyl-core/README.md`](packages/python/sibyl-core/README.md) — Core library
- [`packages/python/sibyl-cli/README.md`](packages/python/sibyl-cli/README.md) — CLI client

## ⚡ Quickstart

```bash
# Start FalkorDB
moon run docker-up

# Install dependencies
uv sync                    # Python (from workspace root)
cd apps/web && pnpm install  # Frontend

# Configure
cp apps/api/.env.example apps/api/.env
# Add SIBYL_OPENAI_API_KEY + SIBYL_JWT_SECRET

# Launch everything
moon run dev               # Starts API, worker, and web concurrently
```

**Ports:**

| Service   | Port |
| --------- | ---- |
| API + MCP | 3334 |
| Frontend  | 3337 |
| FalkorDB  | 6380 |

## 🪄 The CLI

**The CLI is the preferred interface.** Clean JSON output, optimized for AI agents.

```bash
# Search for knowledge
sibyl search "authentication patterns"
sibyl search "OAuth" --type pattern

# List tasks
sibyl task list --status todo
sibyl task list --project proj_abc

# Capture a learning
sibyl add "Redis insight" "Connection pool must be >= concurrent requests"

# Task lifecycle
sibyl task start <id>
sibyl task complete <id> --learnings "Key insight: ..."
```

### Task Workflow

```
backlog ──► todo ──► doing ──► review ──► done ──► archived
                       │
                       ▼
                    blocked
```

## 🔮 The 4-Tool API

Sibyl exposes exactly 4 MCP tools. Simple surface, rich capabilities.

| Tool      | Purpose            | Examples                              |
| --------- | ------------------ | ------------------------------------- |
| `search`  | Find by meaning    | Patterns, tasks, docs, errors         |
| `explore` | Navigate structure | List entities, traverse relationships |
| `add`     | Create knowledge   | Episodes, patterns, tasks             |
| `manage`  | Lifecycle & admin  | Task workflow, crawling, health       |

See [`apps/api/README.md`](apps/api/README.md) for complete API documentation.

## 🔐 Auth

Sibyl uses JWT tokens for web auth and scoped API keys for programmatic access.

**Required:** `SIBYL_JWT_SECRET`

**Optional (GitHub OAuth):** `SIBYL_GITHUB_CLIENT_ID`, `SIBYL_GITHUB_CLIENT_SECRET`

**API Key Scopes:**

- `mcp` — MCP tool access
- `api:read` — REST GET/HEAD/OPTIONS
- `api:write` — REST writes (implies read)

## 🔌 Integration

### Claude Code (MCP)

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

### Subprocess Mode

```json
{
  "mcpServers": {
    "sibyl": {
      "command": "uv",
      "args": ["--directory", "/path/to/sibyl/apps/api", "run", "sibyl-serve", "-t", "stdio"],
      "env": { "SIBYL_OPENAI_API_KEY": "sk-..." }
    }
  }
}
```

## 🛠️ Development

### moonrepo Tasks

```bash
# Full stack
moon run dev              # Start everything
moon run stop             # Stop everything

# Individual services
moon run dev-api          # API + worker only
moon run dev-web          # Frontend only

# Per-project
moon run api:test         # Run API tests
moon run api:lint         # Lint API code
moon run web:build        # Build frontend
moon run core:check       # Lint + typecheck + test core

# Docker
moon run docker-up        # Start FalkorDB
moon run docker-down      # Stop FalkorDB

# Installation
moon run install-cli      # Install sibyl CLI globally
moon run install-skills   # Install Claude Code skills
```

### Direct Commands

```bash
# In apps/api/
uv run pytest             # Run tests
uv run ruff check src     # Lint
uv run sibyl-serve        # Start server

# In apps/web/
pnpm dev                  # Start dev server
pnpm build                # Production build
pnpm biome check .        # Lint
```

## 🧪 Entity Types

| Type       | What It Holds                   |
| ---------- | ------------------------------- |
| `pattern`  | Reusable coding patterns        |
| `episode`  | Temporal learnings, discoveries |
| `task`     | Work items with workflow        |
| `project`  | Container for related work      |
| `epic`     | Feature-level grouping          |
| `rule`     | Sacred constraints, invariants  |
| `source`   | Knowledge origins (URLs, repos) |
| `document` | Crawled/ingested content        |

## 💜 Philosophy

### Search Before Implementing

The graph knows things. Before you code:

```bash
sibyl search "what you're building"
sibyl search "error you hit" --type episode
```

### Work In Task Context

Never do significant work outside a task. Tasks provide traceability, progress tracking, and
knowledge linking.

### Capture What You Learn

If it took time to figure out, save it:

```bash
sibyl add "Descriptive title" "What, why, how, caveats"
```

**Bad:** "Fixed the bug" **Good:** "JWT refresh fails when Redis TTL expires. Root cause: token
service doesn't handle WRONGTYPE. Fix: try/except with regeneration fallback."

### Complete With Learnings

```bash
sibyl task complete <id> --learnings "Key insight: ..."
```

The graph should be smarter after every session.

## License

Apache 2.0 — See [LICENSE](LICENSE)

---

<p align="center">
  Created by <a href="https://hyperbliss.tech">Stefanie Jane</a>
</p>

<p align="center">
  <a href="https://github.com/hyperb1iss">
    <img src="https://img.shields.io/badge/GitHub-hyperb1iss-181717?style=for-the-badge&logo=github" alt="GitHub">
  </a>
  <a href="https://bsky.app/profile/hyperbliss.tech">
    <img src="https://img.shields.io/badge/Bluesky-@hyperbliss.tech-1185fe?style=for-the-badge&logo=bluesky" alt="Bluesky">
  </a>
</p>
