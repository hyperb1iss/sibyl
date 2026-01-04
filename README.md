<h1 align="center">
  <br>
  Sibyl
  <br>
</h1>

<p align="center">
  <strong>Persistent Memory & Agent Orchestration for Teams</strong>
</p>

<p align="center">
  <a href="#-the-problem">
    <img src="https://img.shields.io/badge/Knowledge-Graph_RAG-e135ff?style=for-the-badge&logo=graphql&logoColor=white" alt="Graph-RAG">
  </a>
  <a href="#-agent-orchestration">
    <img src="https://img.shields.io/badge/Agents-Orchestration-80ffea?style=for-the-badge&logo=openai&logoColor=white" alt="Agents">
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
  <a href="#-the-problem">Why Sibyl?</a> •
  <a href="#-quickstart">Quickstart</a> •
  <a href="#-agent-orchestration">Agents</a> •
  <a href="#-the-cli">CLI</a> •
  <a href="#-web-ui">Web UI</a> •
  <a href="#-faq">FAQ</a>
</p>

---

## The Problem

Your team's knowledge is scattered. Solutions live in Slack threads. Learnings die in commit
messages. Every new team member repeats old mistakes. Your AI agents have amnesia—starting fresh
every session, unable to build on what came before.

**Sibyl fixes this.**

Sibyl is a **knowledge graph** that connects everything your team learns. A **task system** that
tracks progress and captures insights. An **agent orchestrator** that lets AI work alongside you
with persistent memory. Search by meaning, not keywords. Never lose an insight again.

## What You Get

| Capability              | What It Means                                                                                               |
| ----------------------- | ----------------------------------------------------------------------------------------------------------- |
| **Semantic Search**     | Find knowledge by meaning—"authentication patterns" finds OAuth solutions even if "OAuth" isn't in the text |
| **Persistent Memory**   | What you learn today helps tomorrow. AI agents remember across sessions                                     |
| **Agent Orchestration** | Spawn Claude agents that work autonomously with human-in-the-loop approvals                                 |
| **Task Workflow**       | Full lifecycle tracking with blockers, learnings, and automatic knowledge linking                           |
| **Doc Ingestion**       | Crawl and index external documentation into your graph                                                      |
| **Multi-Tenancy**       | Isolated graphs per organization. Enterprise-ready from day one                                             |
| **Graph Visualization** | Interactive D3 visualization of your knowledge connections                                                  |

<p align="center">
  <img src="docs/images/dashboard.png" alt="Sibyl Dashboard" width="800">
  <br>
  <em>Dashboard — Stats, entity distribution, quick actions</em>
</p>

<p align="center">
  <img src="docs/images/graph.png" alt="Knowledge Graph" width="800">
  <br>
  <em>Graph — Interactive visualization of knowledge connections</em>
</p>

<p align="center">
  <img src="docs/images/tasks.png" alt="Task Workflow" width="800">
  <br>
  <em>Tasks — Kanban workflow with search and quick creation</em>
</p>

## Quickstart

### Docker (Fastest)

```bash
# Clone and configure
git clone https://github.com/hyperb1iss/sibyl.git
cd sibyl
cp .env.quickstart.example .env

# Add your API keys to .env
# SIBYL_OPENAI_API_KEY=sk-...     (required - for embeddings)
# SIBYL_ANTHROPIC_API_KEY=sk-... (optional - for agents)

# Start everything
docker compose -f docker-compose.prod.yml up -d

# Verify it's working
curl http://localhost:3334/api/health
# → {"status": "healthy", ...}

# Open the web UI
open http://localhost:3337
```

### Development Setup

```bash
# One-line setup (installs proto, moon, toolchain, dependencies)
./setup-dev.sh

# Or manually:
curl -fsSL https://moonrepo.dev/install/proto.sh | bash
proto use                  # Installs node, pnpm, python, uv
proto install moon
uv sync && pnpm install

# Configure
cp apps/api/.env.example apps/api/.env
# Add SIBYL_OPENAI_API_KEY + SIBYL_JWT_SECRET

# Launch everything
moon run dev

# Verify
curl http://localhost:3334/api/health
```

**Ports:**

| Service   | Port | URL                   |
| --------- | ---- | --------------------- |
| API + MCP | 3334 | http://localhost:3334 |
| Web UI    | 3337 | http://localhost:3337 |
| FalkorDB  | 6380 | —                     |

## Agent Orchestration

Sibyl's flagship feature: **spawn AI agents that work autonomously** while you review and approve
their actions.

### How It Works

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│   Web UI    │────▶│   Sibyl     │────▶│   Claude    │
│  (approve)  │◀────│  (orchestr) │◀────│   (agent)   │
└─────────────┘     └─────────────┘     └─────────────┘
       │                   │                   │
       │                   ▼                   │
       │           ┌─────────────┐             │
       └──────────▶│  Knowledge  │◀────────────┘
                   │    Graph    │
                   └─────────────┘
```

1. **Spawn agents** from the web UI or CLI with a task description
2. **Agents work autonomously** using Claude SDK with your knowledge graph as context
3. **Human-in-the-loop approvals** for destructive operations, sensitive files, external APIs
4. **Progress streams** in real-time via WebSocket to the chat UI
5. **Checkpoints save state** so agents can resume after interruptions

### Agent Features

- **Task Assignment** — Agents claim tasks and update status automatically
- **Git Worktrees** — Each agent works in an isolated worktree to prevent conflicts
- **Approval Queue** — Review and approve/deny agent actions before execution
- **Cost Tracking** — Monitor token usage and USD cost per agent
- **Checkpointing** — Save/restore agent state for crash recovery
- **Multi-Agent** — Multiple agents can collaborate on related tasks

### Spawning an Agent

**Web UI:** Navigate to `/agents` → Click "Spawn Agent" → Describe the task

**CLI:**

```bash
sibyl agent spawn --task task_abc123 "Implement the OAuth flow"
```

**REST API:**

```bash
curl -X POST http://localhost:3334/api/agents \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"prompt": "Implement OAuth", "task_id": "task_abc123"}'
```

## The CLI

The CLI is the power-user interface. Clean output, optimized for scripting and AI agent consumption.

```bash
# Install globally
moon run install-cli

# Or use directly
uv tool install sibyl-cli
```

### Core Commands

```bash
# Search your knowledge
sibyl search "authentication patterns"
sibyl search "OAuth" --type pattern

# Add knowledge
sibyl add "Redis connection pooling" "Pool size must be >= concurrent requests to avoid blocking"

# Task workflow
sibyl task list --status todo,doing
sibyl task start task_abc
sibyl task complete task_abc --learnings "Key insight: always check TTL first"

# Explore the graph
sibyl explore related ent_xyz    # Find connected entities
sibyl explore communities        # View knowledge clusters
```

### Task Workflow

```
backlog ──▶ todo ──▶ doing ──▶ review ──▶ done ──▶ archived
                       │
                       ▼
                    blocked
```

### Output Formats

```bash
sibyl task list                  # JSON (default, for scripts)
sibyl task list --table          # Human-friendly table
sibyl task list --csv            # For spreadsheets
```

## Web UI

A full admin interface at `http://localhost:3337`:

- **Dashboard** — Stats overview, recent activity, quick actions
- **Agents** — Spawn, monitor, and chat with AI agents
- **Tasks** — Kanban-style workflow with inline editing
- **Graph** — Interactive D3 visualization of knowledge connections
- **Search** — Semantic search with filters
- **Sources** — Configure documentation crawling
- **Settings** — Organizations, API keys, preferences

**Built with:** Next.js 16, React 19, React Query, Tailwind CSS, SilkCircuit design system

## MCP Integration

Connect Claude Code, Cursor, or any MCP client to Sibyl:

### HTTP Mode (Recommended)

```json
{
  "mcpServers": {
    "sibyl": {
      "type": "http",
      "url": "http://localhost:3334/mcp",
      "headers": {
        "Authorization": "Bearer sk_your_api_key"
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
      "args": ["--directory", "/path/to/sibyl/apps/api", "run", "sibyld", "serve", "-t", "stdio"],
      "env": { "SIBYL_OPENAI_API_KEY": "sk-..." }
    }
  }
}
```

### The 4-Tool API

| Tool      | Purpose            | Examples                              |
| --------- | ------------------ | ------------------------------------- |
| `search`  | Find by meaning    | Patterns, tasks, docs, errors         |
| `explore` | Navigate structure | List entities, traverse relationships |
| `add`     | Create knowledge   | Episodes, patterns, tasks             |
| `manage`  | Lifecycle & admin  | Task workflow, crawling, health       |

## Architecture

```
sibyl/
├── apps/
│   ├── api/              # FastAPI + MCP server (sibyld)
│   ├── cli/              # REST client CLI (sibyl)
│   └── web/              # Next.js 16 frontend
├── packages/python/
│   └── sibyl-core/       # Shared library (models, graph, tools)
├── skills/               # Claude Code skills
├── charts/               # Helm charts for K8s
└── docs/                 # Documentation
```

**Stack:**

- **Backend:** Python 3.13 / FastMCP / FastAPI / Graphiti / FalkorDB
- **Frontend:** Next.js 16 / React 19 / React Query / Tailwind 4
- **Database:** FalkorDB (graph) + PostgreSQL (relational)
- **Build:** moonrepo + uv (Python) + pnpm (TypeScript)
- **Agents:** Claude SDK with human-in-the-loop approvals

## Authentication

### JWT Sessions (Web UI)

```bash
SIBYL_JWT_SECRET=your-secret-key    # Required
SIBYL_JWT_EXPIRY_HOURS=24            # Optional
```

### API Keys (Programmatic Access)

```bash
# Create via CLI
sibyl auth api-key create --name "CI/CD" --scopes mcp,api:read

# Scopes: mcp, api:read, api:write
```

### OAuth (GitHub)

```bash
SIBYL_GITHUB_CLIENT_ID=...
SIBYL_GITHUB_CLIENT_SECRET=...
```

## Deployment

### Docker Compose (Production)

```bash
docker compose -f docker-compose.prod.yml up -d
```

### Kubernetes (Helm)

```bash
helm install sibyl ./charts/sibyl \
  --set backend.existingSecret=sibyl-secrets \
  --set backend.database.existingSecret=sibyl-postgres
```

See [`docs/deployment/`](docs/deployment/) for detailed guides:

- [Docker Compose](docs/deployment/docker-compose.md)
- [Kubernetes](docs/deployment/kubernetes.md)
- [Environment Variables](docs/deployment/environment.md)

## Development

```bash
# Start everything
moon run dev

# Individual services
moon run dev-api          # API + worker
moon run dev-web          # Frontend only

# Quality checks
moon run api:test         # Run API tests
moon run api:lint         # Lint
moon run web:typecheck    # TypeScript check
moon run core:check       # Full check on core library

# Database
moon run docker-up        # Start FalkorDB + PostgreSQL
moon run docker-down      # Stop databases
```

## Entity Types

| Type       | What It Holds                   |
| ---------- | ------------------------------- |
| `pattern`  | Reusable coding patterns        |
| `episode`  | Temporal learnings, discoveries |
| `task`     | Work items with full workflow   |
| `project`  | Container for related work      |
| `epic`     | Feature-level grouping          |
| `rule`     | Sacred constraints, invariants  |
| `source`   | Knowledge origins (URLs, repos) |
| `document` | Crawled/ingested content        |
| `agent`    | AI agent records and state      |

## FAQ

### Who is Sibyl for?

**Teams** who want persistent, searchable knowledge. **Solo developers** building with AI who want
agents that remember. **Anyone** tired of re-solving the same problems.

### Is this just for AI agents?

No. Sibyl is a knowledge management system first. AI agents are a powerful use case, but teams use
it for documentation, task tracking, and collaborative learning without any AI involvement.

### How does it compare to Mem0 / LangMem / similar?

Sibyl is **self-hosted and open source**—you own your data. It includes a full **task workflow
system**, not just memory. It has a **web UI** for humans, not just APIs for machines. And it
provides **agent orchestration** with approvals, not just memory storage.

### What LLM APIs do I need?

- **OpenAI** (required): For embeddings (`text-embedding-3-small`)
- **Anthropic** (optional): For agent orchestration and entity extraction

A typical solo developer uses ~$5/month in API costs.

### Can multiple people collaborate?

Yes. Organizations have isolated graphs with role-based access. Multiple users can share knowledge,
assign tasks, and collaborate on the same graph.

### Is it production-ready?

Sibyl is in active development (v0.1.x). The core features work well, but expect rough edges. We use
it daily for our own development.

## Philosophy

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

## Contributing

We welcome contributions! See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

```bash
# Fork, clone, then:
./setup-dev.sh
moon run dev

# Make changes, then:
moon run :check           # Lint + typecheck + test
```

## License

Apache 2.0 — See [LICENSE](LICENSE)

---

<p align="center">
  <sub>
    ✦ Built with obsession by <a href="https://hyperbliss.tech"><strong>Hyperbliss Technologies</strong></a> ✦
  </sub>
</p>
