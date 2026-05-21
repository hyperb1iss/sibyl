<p align="center">
  <img src="docs/images/sibyl-logo.png" alt="Sibyl" width="400">
</p>

<p align="center">
  <strong>Persistent memory for the AI you build with</strong><br>
  <sub>✦ Yours alone · In your projects · In the systems you share ✦</sub>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Self--hostable-Yes-e135ff?style=for-the-badge" alt="Self-hostable">
  <img src="https://img.shields.io/badge/Storage-SurrealDB-ff6ac1?style=for-the-badge&logo=surrealdb&logoColor=white" alt="SurrealDB">
  <img src="https://img.shields.io/badge/Backend-FastAPI-009688?style=for-the-badge&logo=fastapi&logoColor=white" alt="FastAPI">
  <img src="https://img.shields.io/badge/Frontend-Next.js_16-000000?style=for-the-badge&logo=next.js&logoColor=white" alt="Next.js 16">
  <img src="https://img.shields.io/badge/Python-3.13-3776ab?style=for-the-badge&logo=python&logoColor=white" alt="Python 3.13">
</p>

<p align="center">
  <a href="https://github.com/hyperb1iss/sibyl/actions/workflows/ci.yml">
    <img src="https://img.shields.io/github/actions/workflow/status/hyperb1iss/sibyl/ci.yml?branch=main&style=flat-square&logo=github&logoColor=white&label=CI" alt="CI Status">
  </a>
  <a href="https://github.com/hyperb1iss/sibyl/releases">
    <img src="https://img.shields.io/github/v/release/hyperb1iss/sibyl?style=flat-square&logo=github&logoColor=white" alt="Latest Release">
  </a>
  <a href="https://github.com/hyperb1iss/sibyl/blob/main/LICENSE">
    <img src="https://img.shields.io/badge/License-AGPL_3.0-blue?style=flat-square&logo=gnu&logoColor=white" alt="License">
  </a>
</p>

<p align="center">
  <a href="#-why-sibyl">Why Sibyl?</a> •
  <a href="#-quickstart">Quickstart</a> •
  <a href="#-the-memory-loop">Memory Loop</a> •
  <a href="#the-cli">CLI</a> •
  <a href="#mcp-integration">MCP</a> •
  <a href="#faq">FAQ</a>
</p>

---

## 🔮 Why Sibyl

Sibyl is persistent memory for the coding agents in your terminal, the projects you ship with them,
and the teams you build alongside. One self-hostable knowledge graph, scoped by context. Yours to
run, yours to keep.

Most AI sessions start cold. Coding agents forget yesterday's decisions. Notes drift between
worktrees, tasks scatter across tools, and the context you earned the hard way evaporates the
moment a session ends.

A durable knowledge graph holds what matters: personal context, project work, shared spaces, source
documents, decisions, agent state. Each lives in its own scope and gets tied to the others when
context calls for it. A built-in memory loop (`recall → act → remember → reflect`) keeps hard-won
context close at hand for humans and AI alike. Source-grounded synthesis turns what you already know
into verified documents.

The CLI is the interaction surface. Any agent that runs shell commands (Claude Code, Codex, Cursor,
Aider, Cline, your own scripts) can recall context, capture learnings, and run task workflows the
same way you do. MCP is available for clients that prefer it; the depth lives in the CLI.

**Every session adds up instead of starting over.**

## ✦ What You Get

| Capability                       | What It Means                                                                                                          |
| -------------------------------- | ---------------------------------------------------------------------------------------------------------------------- |
| 🔮 **Compounding Context**       | Every session adds to the graph instead of starting over. The longer you use it, the sharper it gets                   |
| 🪄 **The Memory Loop**           | `recall → act → remember → reflect` runs through the CLI, MCP, and hooks. Agents wake with context and leave it behind |
| 🎯 **Semantic Search**           | Find knowledge by meaning. "Authentication patterns" surfaces OAuth notes even when "OAuth" isn't in the text          |
| 🦋 **Task Workflow**             | Plan with epics and tasks, then track execution across sessions and teammates in one place                             |
| 🧪 **Source-Grounded Synthesis** | Draft verified documents from your own memory with citation, freshness, and gap checks                                 |
| 🌊 **Source Ingestion**          | Crawl documentation sites and import sources (mailboxes, archives) into the same graph                                 |
| 💎 **Scoped Multi-Tenancy**      | Isolated graphs per organization with role-based access. Personal, project, and team scopes stay separate by design    |

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

### Homebrew

```bash
brew install hyperb1iss/tap/sibyl
sibyl init --local
sibyl serve
```

Homebrew installs both `sibyl` and `sibyld`. The local context runs an embedded SurrealDB store
under `~/.sibyl/data/surreal`; use `sibyl start`/`sibyl stop` for a background daemon or
`sibyl service install` to write a launchd/systemd user service.

### Python Tools

```bash
uv tool install sibyl-dev
sibyl init --remote https://sibyl.example.com
sibyl login
```

For a Python-only local install, add the daemon package:

```bash
uv tool install sibyld
sibyl init --local
sibyl serve
```

### Docker Self-Host

```bash
sibyl docker init       # Generate ~/.sibyl/docker/.env + compose
sibyl docker up         # Start API, web, and SurrealDB
sibyl docker logs       # Follow logs
sibyl docker down       # Stop services
sibyl docker upgrade    # Pull and recreate
```

### First Five Minutes

Everything below runs against your local Sibyl stack. MCP wiring is optional.

```bash
# Capture a learning the moment you find it
sibyl remember "Stale auth token bug" \
  "Redis TTL mismatch dropped the cached token early" --kind error_pattern

# Pull it back as working context for your next session
sibyl recall "auth token bug" --intent debug

# Or search semantically across the whole graph
sibyl search "stale auth token redis ttl"

# Package wake-up context for the next coding session
sibyl session bundle
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
# Set SIBYL_JWT_SECRET (auto-generated in dev) and at least one LLM provider key.
# Embeddings use SIBYL_OPENAI_API_KEY or SIBYL_GEMINI_API_KEY.

# Install CLIs globally (editable, source changes reflect immediately)
moon run install-dev

# Launch the default local-dev stack (SurrealDB + API + web)
moon run dev

# Verify
curl http://localhost:3334/api/health
```

`moon run dev` is the single-machine flow. When `SIBYL_SURREAL_URL` is unset it starts local
SurrealDB, points the API at `ws://127.0.0.1:8000/rpc`, and stores data files in
`.moon/cache/surreal-dev`. Jobs and schedules run in-process by default
(`SIBYL_COORDINATION_BACKEND=local`). Set `SIBYL_SURREAL_URL` to connect to a hosted SurrealDB
endpoint, including Surreal Cloud, instead.

**Ports:**

| Service      | Port | URL                     |
| ------------ | ---- | ----------------------- |
| API + MCP    | 3334 | http://localhost:3334   |
| Web UI       | 3337 | http://localhost:3337   |
| SurrealDB    | 8000 | ws://localhost:8000/rpc |
| Redis/Valkey | 6381 | optional                |

## 🪄 The Memory Loop

Sibyl is built around a durable loop that both humans and agents follow:

```
recall ──▶ act ──▶ remember ──▶ reflect
   ▲                                │
   └────────────────────────────────┘
```

1. **Recall** working context before you start. `sibyl recall "<goal>"` returns a compact context
   pack: active work, decisions, plans, constraints, and recent lessons, scoped to your linked
   project.
2. **Act** with that context in hand.
3. **Remember** durable knowledge as you learn it. `sibyl remember` stores decisions, plans, ideas,
   claims, procedures, and gotchas so the next session does not rediscover them.
4. **Reflect** at clean breakpoints. `sibyl reflect` distills raw session notes into reviewable
   memory candidates and can persist them into the graph.

```bash
sibyl recall "ship the context graph" --intent build
sibyl remember "Use context packs" "Group memory before dispatching agents" --kind decision
sibyl reflect "We decided X. Next we build Y." --title "Planning checkpoint" --persist
```

Memory is graded, auditable, and scoped. Raw captures stay verbatim, reflection candidates pass an
automatic review before promotion, and a nightly dream-cycle keeps the graph consolidated. See
[`docs/guide/capturing-knowledge.md`](docs/guide/capturing-knowledge.md).

## The CLI

The CLI is the power-user interface. Clean output, built for scripting and durable project
workflows.

```bash
uv tool install sibyl-dev    # published package
uv tool install sibyld       # direct daemon CLI package
moon run cli:install         # or install from source
```

### Command Families

```bash
# Memory loop
sibyl recall "<goal>"                 # Compile working context
sibyl remember "Title" "Body"         # Store durable memory
sibyl reflect "<notes>" --persist     # Distill notes into candidates
sibyl capture "<quick note>"          # Fast verbatim capture
sibyl search "authentication patterns"

# Knowledge & graph
sibyl add "Redis pooling" "Pool size must be >= concurrent requests"
sibyl explore related ent_xyz         # Find connected entities
sibyl entity show <id>                # Full content by ID

# Task workflow
sibyl task list --status todo,doing
sibyl task start <task_id>
sibyl task complete <task_id> --learnings "Key insight: check TTL first"

# Synthesis, sources, projects, orgs
sibyl synthesis draft "Onboarding guide"
sibyl crawl add "https://docs.example.com" --name "Example Docs"
sibyl project link proj_xxx
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
sibyl task list           # Table output (default)
sibyl task list --json    # JSON for scripts
sibyl task list --csv     # For spreadsheets
```

Full command reference: [`docs/cli/`](docs/cli/).

## 🦋 Web UI

A full admin interface at `http://localhost:3337`:

- **Dashboard:** Stats overview, recent activity, quick actions
- **Tasks:** Kanban-style workflow with inline editing
- **Graph:** Interactive force-directed visualization of knowledge connections
- **Search:** Semantic search with filters
- **Memory:** The memory workspace, raw captures, source imports, and synthesis
- **Sources:** Configure and inspect documentation crawling
- **Settings:** Organizations, teams, API keys, security, LLM routing, backups

**Built with:** Next.js 16, React 19, React Query, Tailwind CSS 4, and the SilkCircuit design
system.

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

### The Tool API

Sibyl exposes eleven MCP tools, organized by what they do:

| Tool               | Purpose                                                   |
| ------------------ | --------------------------------------------------------- |
| `search`           | Unified semantic search across graph and crawled docs     |
| `context`          | Compile an agent context pack for a goal (intent + depth) |
| `explore`          | Navigate the graph: list, related, traverse, dependencies |
| `add`              | Create knowledge: episodes, patterns, tasks, projects     |
| `remember`         | Capture durable memory: decisions, plans, ideas, claims   |
| `reflect`          | Distill raw notes into reviewable memory candidates       |
| `synthesis_plan`   | Plan source-grounded synthesis from authorized memory     |
| `synthesis_draft`  | Draft, verify, and optionally remember an artifact        |
| `synthesis_verify` | Verify citation, freshness, and gap coverage              |
| `manage`           | State changes: task lifecycle, crawling, analysis, admin  |
| `logs`             | Recent server logs (requires OWNER role)                  |

### Claude Code Skills & Hooks

Sibyl ships with [skills](https://docs.anthropic.com/en/docs/claude-code/skills) and
[hooks](https://docs.anthropic.com/en/docs/claude-code/hooks) for built-in Claude Code integration.

```bash
moon run skills:install    # Install the /sibyl skill
moon run hooks:install     # Install context hooks
```

The `/sibyl` skill gives Claude Code full CLI access. Hooks inject context automatically:

| Hook                 | Trigger        | Action                                                                |
| -------------------- | -------------- | --------------------------------------------------------------------- |
| **SessionStart**     | Session begins | Prints a compact session bundle with active tasks and relevant memory |
| **UserPromptSubmit** | Every prompt   | Searches the graph and injects relevant patterns as context           |

See [`skills/`](skills/) and [`hooks/`](hooks/) for implementation details.

## Architecture

```
sibyl/
├── apps/
│   ├── api/              # sibyld - FastAPI + MCP server daemon
│   ├── cli/              # sibyl  - REST client CLI
│   ├── web/              # Next.js 16 frontend
│   └── e2e/              # End-to-end tests
├── packages/python/
│   └── sibyl-core/       # Shared library (models, graph, ai, retrieval, services)
├── skills/               # Claude Code skills
├── hooks/                # Claude Code context hooks
├── charts/               # Helm chart for Kubernetes
├── infra/                # Ansible self-host + local compose
└── docs/                 # Documentation site (VitePress)
```

**Stack:**

- **Backend:** Python 3.13 / FastAPI / FastMCP / SurrealDB-native runtime
- **Frontend:** Next.js 16 / React 19 / React Query / Tailwind 4
- **Storage:** SurrealDB unifies graph, content, and auth. PostgreSQL is retained only for migration
  and archive rehearsal.
- **AI routing:** Anthropic, OpenAI, and Gemini swap per surface; embeddings are pluggable.
- **Coordination:** In-process by default; Redis/Valkey is optional for multi-process or distributed
  deployments.
- **Build:** moonrepo + uv (Python) + pnpm (TypeScript)
- **Compatibility:** Legacy Graphiti-shaped records are handled by Sibyl-owned Surreal projection
  and archive code. No supported install pulls Graphiti.

See [`docs/guide/why-surreal.md`](docs/guide/why-surreal.md) for the rationale and
[`docs/guide/storage-modes.md`](docs/guide/storage-modes.md) for the mode matrix.

## Authentication

### JWT Sessions (Web UI)

```bash
SIBYL_JWT_SECRET=your-secret-key        # Required (auto-generated in dev)
SIBYL_ACCESS_TOKEN_EXPIRE_MINUTES=60    # Optional (default: 60)
```

### API Keys (Programmatic Access)

```bash
sibyl auth api-key create --name "CI/CD" --scopes mcp,api:read
# Scopes: mcp, api:read, api:write
```

### OAuth (GitHub)

```bash
SIBYL_GITHUB_CLIENT_ID=...
SIBYL_GITHUB_CLIENT_SECRET=...
```

MCP endpoints enforce Bearer auth when a JWT secret is set (`SIBYL_MCP_AUTH_MODE=auto`). See
[`docs/api/`](docs/api/) for the full auth reference.

## Deployment

### Docker Compose

```bash
sibyl docker init
sibyl docker up
```

### Kubernetes (Helm)

```bash
helm install sibyl ./charts/sibyl \
  --set backend.existingSecret=sibyl-secrets \
  --set backend.surreal.existingSecret=sibyl-surreal \
  --set ingress.enabled=true
```

See [`docs/deployment/`](docs/deployment/) for detailed guides:

- [Docker Compose](docs/deployment/docker-compose.md)
- [Kubernetes](docs/deployment/kubernetes.md)
- [Environment Variables](docs/deployment/environment.md)

## Knowledge Model

Sibyl models a wide range of entity types so memory stays structured, not just a pile of notes:

- **Work:** `task`, `epic`, `project`, `milestone`
- **Knowledge:** `pattern`, `episode`, `procedure`, `rule`, `guide`, `error_pattern`
- **Memory:** `decision`, `plan`, `idea`, `claim`, `artifact`, `session`, `note`
- **Sources:** `source`, `document`, `domain`, `community`

The full registry and how types relate is documented in
[`docs/guide/entity-types.md`](docs/guide/entity-types.md).

## FAQ

### Who is Sibyl for?

**Solo developers** who want durable memory for projects and debugging. **Teams** who want shared
knowledge that compounds. **Anyone** building with AI who is tired of repeating context every
session.

### Do I need AI agents to use Sibyl?

No. The knowledge graph and task system are the core product: documentation, task tracking, captured
learnings, and semantic search over what your team already knows. AI agents make the memory loop
automatic, but they are not required.

### How does it compare to Mem0 / LangMem / similar?

Sibyl is **self-hosted and open source**. You own your data. It includes a full **task workflow
system**, not just memory. It has a **web UI** for humans, not just APIs for machines. And it keeps
knowledge, tasks, and docs connected in one graph instead of scattering them across tools.

### What LLM APIs do I need?

- **Anthropic, OpenAI, or Gemini** (required): for language-model surfaces such as crawler
  extraction, synthesis, and reflection.
- **OpenAI or Gemini** (required): for embeddings and semantic search.

Providers and models are configurable globally or per surface, and the web admin settings page can
save instance-wide model routing. A typical solo developer uses around $5/month in API costs.

### Is it production-ready?

Sibyl is in active development (v0.10.x, heading toward 1.0). SurrealDB is the default runtime for
graph, content, and auth; legacy PostgreSQL paths are retained only for migration and archive
rehearsal. **We use Sibyl to build Sibyl.** Every feature, task, and learning you see here was
tracked inside the system itself.

## 🎯 Roadmap

**Where we're headed after v0.10.0, toward 1.0:**

- **Corpus Runtime:** real private source corpora can be imported, searched, inspected, corrected,
  and synthesized without leaking scope.
- **Memory Workspace OS:** automatic memory decisions become visible, explainable, correctable, and
  undoable from one product surface.
- **Surreal-only closure:** Graphiti and Redis-required local assumptions leave the supported
  runtime once native behavior has receipts.
- **1.0 evidence freeze:** release claims cite gates, artifacts, and install rehearsals.

The graph gets smarter. The workflow gets sharper. See
[`docs/architecture/SIBYL_1_0_ROADMAP.md`](docs/architecture/SIBYL_1_0_ROADMAP.md).

## 💜 Philosophy

### Recall Before You Act

The graph knows things. Before you code, pull context:

```bash
sibyl recall "what you're building" --intent build
sibyl search "error you hit" --type episode
```

### Work In Task Context

Never do significant work outside a task. Tasks provide traceability, progress tracking, and
knowledge linking.

### Remember What You Learn

If it took time to figure out, save it:

```bash
sibyl remember "Descriptive title" "What, why, how, caveats" --kind decision
```

**Bad:** "Fixed the bug." **Good:** "JWT refresh fails when Redis TTL expires. Root cause: token
service does not handle WRONGTYPE. Fix: try/except with regeneration fallback."

### Complete With Learnings

```bash
sibyl task complete <id> --learnings "Key insight: ..."
```

The graph should be smarter after every session.

## Contributing

We welcome contributions. See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

```bash
# Fork, clone, then:
./setup-dev.sh
moon run dev

# Make changes, then:
moon run :check           # Lint + typecheck + test
```

## License

AGPL-3.0. See [LICENSE](LICENSE).

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
    If Sibyl helps your team remember, give us a star or <a href="https://ko-fi.com/hyperb1iss">support the project</a>
    <br><br>
    ✦ Built with obsession by <a href="https://hyperbliss.tech"><strong>Hyperbliss Technologies</strong></a> ✦
  </sub>
</p>
