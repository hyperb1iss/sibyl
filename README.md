<p align="center">
  <img src="docs/images/sibyl-logo.png" alt="Sibyl" width="400">
</p>

<p align="center">
  <strong>Build With Agents That Remember</strong><br>
  <sub>✦ A Collective Intelligence Runtime ✦</sub>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Claude-Agents-d4a574?style=for-the-badge&logo=anthropic&logoColor=white" alt="Claude">
  <img src="https://img.shields.io/badge/Graphiti-Memory-e135ff?style=for-the-badge&logo=neo4j&logoColor=white" alt="Graphiti">
  <img src="https://img.shields.io/badge/FalkorDB-Graph-00aaff?style=for-the-badge&logo=redis&logoColor=white" alt="FalkorDB">
  <img src="https://img.shields.io/badge/PostgreSQL_18-Data-336791?style=for-the-badge&logo=postgresql&logoColor=white" alt="PostgreSQL">
  <img src="https://img.shields.io/badge/FastAPI-Backend-009688?style=for-the-badge&logo=fastapi&logoColor=white" alt="FastAPI">
  <img src="https://img.shields.io/badge/Next.js_16-Frontend-000000?style=for-the-badge&logo=next.js&logoColor=white" alt="Next.js">
  <img src="https://img.shields.io/badge/moon-Monorepo-af63d3?style=for-the-badge&logo=moonrepo&logoColor=white" alt="moon">
</p>

<p align="center">
  <a href="https://github.com/hyperb1iss/sibyl/actions/workflows/ci.yml">
    <img src="https://img.shields.io/github/actions/workflow/status/hyperb1iss/sibyl/ci.yml?branch=main&style=flat-square&logo=github&logoColor=white&label=CI" alt="CI Status">
  </a>
  <a href="https://github.com/hyperb1iss/sibyl/releases">
    <img src="https://img.shields.io/github/v/release/hyperb1iss/sibyl?style=flat-square&logo=github&logoColor=white" alt="Latest Release">
  </a>
  <a href="https://github.com/hyperb1iss/sibyl/blob/main/LICENSE">
    <img src="https://img.shields.io/github/license/hyperb1iss/sibyl?style=flat-square&logo=gnu&logoColor=white" alt="License">
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

## 🔮 The Problem

Your AI agents have amnesia. Every session starts fresh—no memory of what worked, what failed, or
what you learned yesterday. And when you're running multiple agents across different features?
Chaos. No visibility. No coordination.

**What if your agents could remember—and you could orchestrate them?**

Sibyl gives your AI agents **persistent memory** through a knowledge graph. Plan work as epics and
tasks. Spawn agents that execute autonomously—each building on shared learnings, each tracked in one
place. You stay in control: approving decisions, seeing progress across all parallel efforts,
keeping the whole project moving forward.

Solo dev? Your agents become your team—a collective that gets smarter with every session. Actual
team? Everyone's insights compound. Either way: **the whole becomes greater than the sum of its
parts.**

## ✦ What You Get

| Capability                     | What It Means                                                                                               |
| ------------------------------ | ----------------------------------------------------------------------------------------------------------- |
| 🔮 **Collective Intelligence** | Every agent contributes. Every session compounds. The graph gets smarter over time                          |
| 🎯 **Semantic Search**         | Find knowledge by meaning—"authentication patterns" finds OAuth solutions even if "OAuth" isn't in the text |
| 🧠 **Persistent Memory**       | What you learn today helps tomorrow. AI agents remember across sessions                                     |
| ⚡ **Agent Orchestration**     | Spawn Claude agents that work autonomously with human-in-the-loop approvals                                 |
| 🦋 **Task Workflow**           | Plan with epics and tasks. Track parallel work across agents. See everything in one place                   |
| 📚 **Doc Ingestion**           | Crawl and index external documentation into your graph                                                      |
| 🏢 **Multi-Tenancy**           | Isolated graphs per organization. Enterprise-ready from day one                                             |
| 🌐 **Graph Visualization**     | Interactive D3 visualization of your knowledge connections                                                  |

<table>
  <tr>
    <td align="center">
      <img src="docs/images/dashboard.png" alt="Dashboard" width="400"><br>
      <sub>Dashboard</sub>
    </td>
    <td align="center">
      <img src="docs/images/projects.png" alt="Projects" width="400"><br>
      <sub>Projects</sub>
    </td>
  </tr>
  <tr>
    <td align="center">
      <img src="docs/images/graph.png" alt="Graph" width="400"><br>
      <sub>Knowledge Graph</sub>
    </td>
    <td align="center">
      <img src="docs/images/tasks.png" alt="Tasks" width="400"><br>
      <sub>Task Workflow</sub>
    </td>
  </tr>
</table>

## ⚡ Quickstart

### Docker (Fastest)

```bash
# Clone and start (no config needed!)
git clone https://github.com/hyperb1iss/sibyl.git
cd sibyl
docker compose -f docker-compose.quickstart.yml up -d

# Open the web UI and complete onboarding
open http://localhost:3337
# → Enter your API keys in the setup wizard
# → Keys are saved securely to the database
```

**Zero-config approach:** No `.env` file required! API keys are entered during onboarding and stored
encrypted in the database. For advanced setup, see `.env.quickstart.example`.

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

## 🤖 Agent Orchestration

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

### The 4-Tool API

| Tool      | Purpose            | Examples                              |
| --------- | ------------------ | ------------------------------------- |
| `search`  | Find by meaning    | Patterns, tasks, docs, errors         |
| `explore` | Navigate structure | List entities, traverse relationships |
| `add`     | Create knowledge   | Episodes, patterns, tasks             |
| `manage`  | Lifecycle & admin  | Task workflow, crawling, health       |

### Claude Code Skills & Hooks

Sibyl ships with [skills](https://docs.anthropic.com/en/docs/claude-code/skills) and
[hooks](https://docs.anthropic.com/en/docs/claude-code/hooks) for seamless Claude Code integration.

**Install:**

```bash
moon run skills:install    # Install /sibyl skill
moon run hooks:install     # Install context hooks
```

**`/sibyl` skill** — Full CLI access from Claude Code:

```bash
/sibyl search "authentication patterns"
/sibyl task list --status doing
/sibyl add "OAuth insight" "Token refresh needs..."
```

**Hooks** — Automatic context injection:

| Hook                 | Trigger        | Action                                           |
| -------------------- | -------------- | ------------------------------------------------ |
| **SessionStart**     | Session begins | Shows active tasks, reminds to capture learnings |
| **UserPromptSubmit** | Every prompt   | Searches graph, injects relevant patterns        |

The `UserPromptSubmit` hook extracts keywords from your prompt, searches Sibyl, and injects matching
patterns as context—so Claude always knows what you've learned before.

See [`skills/`](skills/) and [`hooks/`](hooks/) for implementation details.

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

**Solo developers** who want a team of AI agents working on their codebase—with memory that
persists. **Teams** who want shared knowledge that compounds. **Anyone** building with AI who's
tired of repeating context every session.

### Do I need AI agents to use Sibyl?

No. The knowledge graph and task system work great standalone—for documentation, task tracking, and
capturing learnings. But agents are where Sibyl really shines: autonomous workers that share memory
and coordinate through your graph.

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

Sibyl is in active development (v0.1.x). The core features work well, but expect rough edges. **We
use Sibyl to build Sibyl**—every feature, task, and learning you see here was tracked and
orchestrated through the system itself.

## 🗺️ Roadmap

**Where we're headed:**

- **Multi-vendor agents** — Orchestrate Claude, Codex, Gemini, and future models from one place
- **Meta-orchestrator** — A higher-order agent that manages your fleet: distributing tasks, monitoring
  progress, responding to your plans
- **Brainstorming spaces** — Dedicated areas for ideation before execution
- **External data connectors** — Feed more sources into the collective intelligence
- **Deeper reasoning** — Multi-hop graph traversal, community-based retrieval, agentic reflection

The collective gets smarter. The orchestration gets deeper.

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

AGPL-3.0 — See [LICENSE](LICENSE)

---

<p align="center">
  <a href="https://github.com/hyperb1iss/sibyl">
    <img src="https://img.shields.io/github/stars/hyperb1iss/sibyl?style=social" alt="Star on GitHub">
  </a>
  &nbsp;&nbsp;
  <a href="https://ko-fi.com/hyperb1iss">
    <img src="https://img.shields.io/badge/Ko--fi-Support%20Development-ff5e5b?logo=ko-fi&logoColor=white" alt="Ko-fi">
  </a>
</p>

<p align="center">
  <sub>
    If Sibyl helps your agents remember, give us a ⭐ or <a href="https://ko-fi.com/hyperb1iss">support the project</a>
    <br><br>
    ✦ Built with obsession by <a href="https://hyperbliss.tech"><strong>Hyperbliss Technologies</strong></a> ✦
  </sub>
</p>
