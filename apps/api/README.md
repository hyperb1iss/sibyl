<h1 align="center">
  <br>
  🔮 Sibyl
  <br>
</h1>

<p align="center">
  <strong>Your AI agent's persistent memory</strong>
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
  <a href="#-the-cli">CLI</a> •
  <a href="#-the-4-tool-api">API</a> •
  <a href="#-the-web-ui">Web UI</a>
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
| **Persistent Memory** | What you learn today helps tomorrow            |

**Stack:** Python 3.11+ / FastMCP / Graphiti / FalkorDB / Next.js 16

## ⚡ Quickstart

```bash
# Start FalkorDB
docker compose up -d

# Install
uv sync --all-extras

# Configure
cp .env.example .env
# Add your SIBYL_OPENAI_API_KEY + SIBYL_JWT_SECRET

# Verify
uv run sibyl health

# Launch
uv run sibyl serve
```

Server runs at `localhost:3334`. Web UI at `localhost:3337` (if running frontend).

## 🔐 Auth

Sibyl uses a signed JWT access token for web auth (stored in the `sibyl_access_token` HTTP-only
cookie) and supports scoped API keys for programmatic access.

**Required env:**

- `SIBYL_JWT_SECRET` (JWT signing secret)

**Optional env (GitHub login):**

- `SIBYL_GITHUB_CLIENT_ID`, `SIBYL_GITHUB_CLIENT_SECRET`
- `SIBYL_SERVER_URL` (public base URL; used for OAuth callbacks)
- `SIBYL_FRONTEND_URL` (default post-login redirect)

**Cookie defaults:**

- `Secure` auto-enables when `SIBYL_SERVER_URL` starts with `https://` (override with
  `SIBYL_COOKIE_SECURE=true|false`)
- Host-only cookies by default (override with `SIBYL_COOKIE_DOMAIN=…`)

**Local users (email/password):**

- `POST /api/auth/local/signup` and `POST /api/auth/local/login` (accept JSON or form posts)
- Optional hardening: set `SIBYL_PASSWORD_PEPPER` and (if needed) `SIBYL_PASSWORD_ITERATIONS`

**MCP protection:**

- Set `SIBYL_MCP_AUTH_MODE=auto|on|off` (auto enforces when `SIBYL_JWT_SECRET` is set)

**API key scopes:**

- `mcp`: required for `/mcp` tool calls (MCP clients)
- `api:read`: allows REST safe methods (`GET/HEAD/OPTIONS`) with an API key
- `api:write`: allows REST writes (and implies read) with an API key

## 🪄 The CLI

**The CLI is the preferred interface.** Clean JSON output, optimized for AI agents.

```bash
# Search for knowledge
uv run sibyl search "authentication patterns"
uv run sibyl search "OAuth" --type pattern

# List tasks
uv run sibyl task list --status todo
uv run sibyl task list --project proj_abc

# Capture a learning
uv run sibyl add "Redis insight" "Connection pool must be >= concurrent requests"

# Task lifecycle
uv run sibyl task start <id>
uv run sibyl task complete <id> --learnings "Key insight: ..."

# Direct updates (bulk/historical)
uv run sibyl task update <id> --status done --priority high
```

### Task Workflow

```
backlog ──► todo ──► doing ──► review ──► done ──► archived
                       │
                       ▼
                    blocked
```

Any state transition is allowed. Use workflow commands for semantics, `update` for direct changes.

### Output Formats

```bash
uv run sibyl task list              # JSON (default, for agents)
uv run sibyl task list --table      # Human-readable
uv run sibyl task list --csv        # Spreadsheets
```

## 🔮 The 4-Tool API

Sibyl exposes exactly 4 MCP tools. Simple surface, rich capabilities.

| Tool      | Purpose            | Examples                              |
| --------- | ------------------ | ------------------------------------- |
| `search`  | Find by meaning    | Patterns, tasks, docs, errors         |
| `explore` | Navigate structure | List entities, traverse relationships |
| `add`     | Create knowledge   | Episodes, patterns, tasks             |
| `manage`  | Lifecycle & admin  | Task workflow, crawling, health       |

### search

```python
# Find patterns
search("OAuth 2.0 implementation", types=["pattern"])

# Find open tasks
search("", types=["task"], status="doing")

# Search crawled docs
search("hooks state management", source="react-docs")
```

### explore

```python
# List all projects
explore(mode="list", types=["project"])

# Find related knowledge
explore(mode="related", entity_id="pattern_oauth")

# Task dependencies
explore(mode="dependencies", entity_id="task_abc")
```

### add

```python
# Record a learning
add("Redis connection pooling",
    "Connection pool must be >= concurrent requests...",
    category="debugging", technologies=["redis"])

# Create a task
add("Implement OAuth", "Add Google and GitHub...",
    entity_type="task", project="proj_auth", priority="high")
```

### manage

```python
# Task workflow
manage("start_task", entity_id="task_abc")
manage("complete_task", entity_id="task_abc",
       data={"learnings": "Token refresh needs exact URI match"})

# Crawl documentation
manage("crawl", data={"url": "https://docs.example.com", "depth": 3})

# Health check
manage("health")
```

## 🦋 The Web UI

Full-featured dashboard at `localhost:3337`:

- **Dashboard** — Stats, task overview, quick actions
- **Projects** — Organize work into containers
- **Tasks** — Kanban-style task management
- **Entities** — Browse all knowledge types
- **Graph** — Visual exploration of connections
- **Search** — Semantic search with filters

```bash
cd web
pnpm install
pnpm dev
```

## 🧪 Entity Types

| Type       | What It Holds                   |
| ---------- | ------------------------------- |
| `pattern`  | Reusable coding patterns        |
| `episode`  | Temporal learnings, discoveries |
| `task`     | Work items with workflow        |
| `project`  | Container for related work      |
| `rule`     | Sacred constraints, invariants  |
| `source`   | Knowledge origins (URLs, repos) |
| `document` | Crawled/ingested content        |

## 💜 Philosophy

### Search Before Implementing

The graph knows things. Before you code:

```bash
uv run sibyl search "what you're building"
uv run sibyl search "error you hit" --type episode
```

### Work In Task Context

Never do significant work outside a task. Tasks provide traceability, progress tracking, and
knowledge linking.

### Capture What You Learn

If it took time to figure out, save it:

```bash
uv run sibyl add "Descriptive title" "What, why, how, caveats"
```

**Bad:** "Fixed the bug" **Good:** "JWT refresh fails when Redis TTL expires. Root cause: token
service doesn't handle WRONGTYPE. Fix: try/except with regeneration fallback."

### Complete With Learnings

```bash
uv run sibyl task complete <id> --learnings "Key insight: ..."
```

The graph should be smarter after every session.

## 🔧 Configuration

```bash
# Required
SIBYL_OPENAI_API_KEY=sk-...

# FalkorDB (defaults work with docker-compose)
SIBYL_FALKORDB_HOST=localhost
SIBYL_FALKORDB_PORT=6380
SIBYL_FALKORDB_PASSWORD=conventions

# Optional
SIBYL_LOG_LEVEL=INFO
SIBYL_EMBEDDING_MODEL=text-embedding-3-small
```

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
      "args": ["--directory", "/path/to/sibyl", "run", "sibyl", "serve", "-t", "stdio"],
      "env": { "SIBYL_OPENAI_API_KEY": "sk-..." }
    }
  }
}
```

## 📚 Documentation

| Doc                                                   | What's Inside                           |
| ----------------------------------------------------- | --------------------------------------- |
| [Architecture](docs/CONSOLIDATED_ARCHITECTURE.md)     | System design deep dive                 |
| [Agent Prompt](docs/agent-system-prompt.md)           | How to integrate Sibyl in agent prompts |
| [Graph-RAG Research](docs/graph-rag-sota-research.md) | SOTA research summary                   |

## 🛠️ Development

```bash
just lint          # ruff check + pyright
just fix           # ruff fix + format
just test          # pytest
just serve         # Start server

# Frontend
cd web && pnpm dev
```

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
