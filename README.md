<p align="center">
  <img src="docs/images/sibyl-logo.png" alt="Sibyl" width="400">
</p>

<p align="center">
  <strong>Build With Memory That Compounds</strong><br>
  <sub>✦ Knowledge Graph + Task Workflow ✦</sub>
</p>

<p align="center">
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
  <a href="#-the-cli">CLI</a> •
  <a href="#-web-ui">Web UI</a> •
  <a href="#-faq">FAQ</a>
</p>

---

## 🔮 The Vision

Persistent memory for your projects, tasks, and research. A collective intelligence that compounds
with every session and makes your graph more useful over time.

Most coding sessions start cold. No memory of what worked, what failed, or what you learned
yesterday. Notes drift. Tasks scatter. Useful context disappears.

**Sibyl changes that.**

A knowledge graph gives your work persistent memory. Epics and tasks structure execution. Search,
docs ingestion, and graph exploration keep hard-won context close at hand for both humans and tools.

**The whole becomes greater than the sum of its parts.**

## ✦ What You Get

| Capability                     | What It Means                                                                                               |
| ------------------------------ | ----------------------------------------------------------------------------------------------------------- |
| 🔮 **Collective Intelligence** | Every session compounds. The graph gets smarter as your team and tools capture real work                    |
| 🎯 **Semantic Search**         | Find knowledge by meaning. "Authentication patterns" finds OAuth solutions even if "OAuth" isn't in the text |
| 🔮 **Persistent Memory**       | What you learn today helps tomorrow. Patterns, decisions, and gotchas stay searchable across sessions       |
| 🦋 **Task Workflow**           | Plan with epics and tasks. Track execution across sessions and teammates in one place                       |
| 🌊 **Doc Ingestion**           | Crawl and index external documentation into your graph                                                      |
| 💎 **Multi-Tenancy**           | Isolated graphs per organization. Enterprise-ready from day one                                             |
| ⚡ **Graph Visualization**     | Interactive D3 visualization of your knowledge connections                                                  |

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

### One-Liner Install

```bash
curl -fsSL https://raw.githubusercontent.com/hyperb1iss/sibyl/main/install.sh | sh
```

Installs uv (if needed), installs sibyl-dev, starts Sibyl. Done.

### Manual Install (UV)

```bash
uv tool install sibyl-dev
sibyl local start
```

### Alternative: pipx

```bash
pipx install sibyl-dev
sibyl local start
```

### CLI Commands

```bash
sibyl local start    # Start all services
sibyl local stop     # Stop services
sibyl local status   # Show running services
sibyl local logs     # Follow logs
sibyl local reset    # Nuke and start fresh
```

### First Five Minutes

Everything below runs against your local Sibyl stack. MCP wiring is optional.

```bash
# Capture a fresh learning right away
sibyl capture "Redis TTL mismatch caused the stale auth token bug" --type episode --tags auth,redis

# Search it back semantically
sibyl search "stale auth token redis ttl"

# Package wake-up context for the next coding session
sibyl session bundle

# Review raw captures that still need graph linkage
sibyl archive list --surface cli
# Then visit http://localhost:3337/archive?link=unlinked
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
cp .env.example .env
# Add SIBYL_OPENAI_API_KEY + SIBYL_JWT_SECRET

# Install CLIs globally (editable, source changes reflect immediately)
moon run install-dev

# Launch everything
moon run dev

# Verify
curl http://localhost:3334/api/health
```

### Retrieval Benchmarks

```bash
# Live artifact-producing evaluation against your running Sibyl stack
moon run bench-live -- --label legacy --metadata store=legacy

# Compare a later Surreal run against the legacy artifact
moon run bench-live -- --label surreal --metadata store=surreal
uv run python benchmarks/compare_eval_reports.py \
  benchmarks/results/eval_unified_legacy_20260419_120000.json \
  benchmarks/results/eval_unified_surreal_20260419_123000.json

# Live read-only smoke and latency checks against the same stack
moon run bench-live-smoke

# Synthetic retrieval and ranking component benchmarks
moon run bench-retrieval

# Offline LongMemEval-style baseline (not the live runtime path)
uv run python benchmarks/longmemeval_bench.py /path/to/longmemeval.json --mode hybrid
```

`bench-live` is the canonical runtime evaluation entry point. It exercises the real `/api/search`
and RAG surfaces with your CLI auth context and writes JSON artifacts to `benchmarks/results/`
unless you pass `--no-save`. Use `--label` and repeated `--metadata key=value` flags when you want
to compare runs across stores or datasets.

For graph migration drills, `sibyld export graph --org-id ...` now produces a restoreable graph
artifact. That file can be loaded into the active graph runtime with `sibyld db restore ... --yes`
before you run `bench-live`.

`bench-live-smoke` keeps the existing read-only pytest latency and shape checks for local health
verification.

`bench-retrieval` and `benchmarks/longmemeval_bench.py` are intentionally offline. They are useful
for relative tuning and apples-to-apples baselines, but they do not measure the production HTTP
runtime path.

See [`docs/testing/benchmark-methodology.md`](./docs/testing/benchmark-methodology.md) for the
measurement ladder, artifact expectations, and how to avoid benchmark drift.

**Ports:**

| Service   | Port | URL                   |
| --------- | ---- | --------------------- |
| API + MCP | 3334 | http://localhost:3334 |
| Web UI    | 3337 | http://localhost:3337 |
| FalkorDB  | 6380 | -                     |

## 🔮 Core Workflow

Sibyl is strongest when it stays close to the work itself:

1. **Capture knowledge** from debugging, implementation, and research
2. **Search semantically** when you need the pattern again
3. **Track execution** with projects, epics, and tasks
4. **Ingest docs** so external references live beside internal learnings
5. **Explore the graph** to see how ideas, tasks, and sources connect

The happy path is local-first: start the stack, capture something useful, search it back, then add
MCP clients or broader automation once the core loop feels good.

## The CLI

The CLI is the power-user interface. Clean output, optimized for scripting and durable project
workflows.

```bash
# Install globally
moon run cli:install

# Or install the published package directly
uv tool install sibyl-dev
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
sibyl task start <task_id>
sibyl task complete <task_id> --learnings "Key insight: always check TTL first"

# Explore the graph
sibyl explore related ent_xyz    # Find connected entities
sibyl explore traverse ent_xyz   # Walk outward from an entity
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
sibyl task list                  # Table output (default)
sibyl task list --json           # JSON for scripts
sibyl task list --csv            # For spreadsheets
```

## Web UI

A full admin interface at `http://localhost:3337`:

- **Dashboard:** Stats overview, recent activity, quick actions
- **Tasks:** Kanban-style workflow with inline editing
- **Graph:** Interactive D3 visualization of knowledge connections
- **Search:** Semantic search with filters
- **Sources:** Configure documentation crawling
- **Settings:** Organizations, API keys, preferences

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
[hooks](https://docs.anthropic.com/en/docs/claude-code/hooks) for built-in Claude Code integration.

**Install:**

```bash
moon run skills:install    # Install /sibyl skill
moon run hooks:install     # Install context hooks
```

**`/sibyl` skill:** Full CLI access from Claude Code:

```bash
/sibyl search "authentication patterns"
/sibyl task list --status doing
/sibyl add "OAuth insight" "Token refresh needs..."
```

**Hooks:** Automatic context injection:

| Hook                 | Trigger        | Action                                           |
| -------------------- | -------------- | ------------------------------------------------ |
| **SessionStart**     | Session begins | Prints a compact session bundle with active tasks and relevant memory |
| **UserPromptSubmit** | Every prompt   | Searches graph, injects relevant patterns        |

The `UserPromptSubmit` hook extracts keywords from your prompt, searches Sibyl, and injects matching
patterns as context, so Claude always knows what you've learned before.

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
- **Integrations:** Claude Code, MCP clients, and project-local hooks

## Authentication

### JWT Sessions (Web UI)

```bash
SIBYL_JWT_SECRET=your-secret-key    # Required
SIBYL_ACCESS_TOKEN_EXPIRE_MINUTES=60  # Optional (default: 60)
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
# Install CLIs globally (editable, picks up source changes)
moon run install-dev

# Install CLIs globally (frozen copy, for CI / production)
moon run install

# Start everything
moon run dev

# Start the Surreal-backed stack explicitly.
# Auto-resumes the newest rehearsal store in .moon/cache if one exists.
moon run dev-surreal

# Falkor/Postgres alias
moon run dev-legacy

# Individual services
moon run dev-api          # API only
moon run dev-web          # Frontend only

# Quality checks
moon run api:test         # Run API tests
moon run api:lint         # Lint
moon run web:typecheck    # TypeScript check
moon run core:check       # Full check on core library

# Database
moon run docker-up        # Start SurrealDB + Redis/Valkey
moon run docker-down      # Stop databases
```

`moon run dev` stays on the FalkorDB/PostgreSQL stack for now while the Surreal path stabilizes.
`moon run dev-surreal` is the explicit Surreal server-mode flow. When `SIBYL_SURREAL_URL` is unset
it starts local SurrealDB plus Redis/Valkey, points the API and worker at
`ws://127.0.0.1:8000/rpc`, and auto-resumes the newest rehearsal store in `.moon/cache` if one
exists. Set `SIBYL_SURREAL_URL` to a hosted SurrealDB endpoint, including Surreal Cloud, to skip
the local database and connect remotely instead. `moon run dev-legacy` remains as an alias for the
Falkor/PostgreSQL path.

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

## FAQ

### Who is Sibyl for?

**Solo developers** who want durable memory for projects and debugging. **Teams** who want shared
knowledge that compounds. **Anyone** building with AI who is tired of repeating context every
session.

### Do I need AI agents to use Sibyl?

No. The knowledge graph and task system are the core product: documentation, task tracking, captured
learnings, and semantic search over what your team already knows.

### How does it compare to Mem0 / LangMem / similar?

Sibyl is **self-hosted and open source**. You own your data. It includes a full **task workflow
system**, not just memory. It has a **web UI** for humans, not just APIs for machines. And it keeps
knowledge, tasks, and docs connected in one graph instead of scattering them across tools.

### What LLM APIs do I need?

- **OpenAI** (required): For embeddings (`text-embedding-3-small`)
- **Anthropic** (optional): For additional model-powered extraction workflows

A typical solo developer uses ~$5/month in API costs.

### Can multiple people collaborate?

Yes. Organizations have isolated graphs with role-based access. Multiple users can share knowledge,
assign tasks, and collaborate on the same graph.

### Is it production-ready?

Sibyl is in active development (v0.1.x). The core features work well, but expect rough edges. **We
use Sibyl to build Sibyl**. Every feature, task, and learning you see here was tracked inside the
system itself.

## 🎯 Roadmap

**Where we're headed:**

- **Stronger project boundaries:** Finish project membership and permission flows end to end
- **Deeper graph retrieval:** Better ranking, traversal, deduplication, and relationship-aware
  search
- **Brainstorming spaces:** Dedicated areas for ideation before execution
- **External data connectors:** Feed more docs, repos, and notes into the graph
- **Workflow polish:** Sharper epic/task planning, progress views, and review loops

The graph gets smarter. The workflow gets sharper.

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

AGPL-3.0. See [LICENSE](LICENSE)

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
    If Sibyl helps your team remember, give us a ⭐ or <a href="https://ko-fi.com/hyperb1iss">support the project</a>
    <br><br>
    ✦ Built with obsession by <a href="https://hyperbliss.tech"><strong>Hyperbliss Technologies</strong></a> ✦
  </sub>
</p>
