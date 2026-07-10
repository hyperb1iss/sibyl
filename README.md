<p align="center">
  <img src="docs/images/sibyl-logo.png" alt="Sibyl" width="400">
</p>

<p align="center">
  <strong>One CLI. One graph. Every AI tool you use, sharing memory.</strong><br>
  <sub>✦ Cross-agent memory for AI coding tools · self-hostable · yours to keep ✦</sub>
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
    <img src="https://img.shields.io/badge/License-Apache_2.0-blue?style=flat-square&logo=apache&logoColor=white" alt="License">
  </a>
</p>

<p align="center">
  <a href="#-why-sibyl">Why Sibyl?</a> •
  <a href="#-quickstart">Quickstart</a> •
  <a href="#-the-memory-loop">Memory Loop</a> •
  <a href="#the-cli">CLI</a> •
  <a href="#-skills--hooks">Skills</a> •
  <a href="#mcp-integration">MCP</a> •
  <a href="https://hyperb1iss.github.io/sibyl/">📖 Docs</a>
</p>

---

## 🔮 Why Sibyl

In 2026, every app remembers you. Your assistant imports your history from the last one, every tool
quietly assembles a profile, and the memory you generate becomes someone else's asset. The question
stopped being _whether_ it remembers and became _who the memory works for_.

Sibyl is **cross-agent memory** where the answer is you. One knowledge graph holds your decisions,
your gotchas, and the conventions you actually follow. It is self-hosted on your own hardware and
shared across every coding agent you run: Claude Code, Codex, Cursor, and the agents you build
yourself. Tell one tool your context, and every tool keeps it. Nothing harvested into a vendor's
profile, nothing trapped behind an export you can't take.

The interaction surface is the shell. If your agent can run a command, it already speaks Sibyl. No
SDK to adopt, no MCP server you are locked into. The depth lives in the CLI; MCP is there for the
clients that prefer it.

**Switch tools, swap models, walk to a new machine. The graph comes with you, because it was always
yours.**

## ✦ What You Get

| Capability                       | What It Means                                                                                                           |
| -------------------------------- | ----------------------------------------------------------------------------------------------------------------------- |
| 🔮 **Compounding Context**       | Every session adds to the graph instead of starting over. The longer you use it, the sharper it gets                    |
| 🪄 **The Memory Loop**           | `context → act → remember → reflect` runs through the CLI, skills, MCP, and hooks. Agents wake with context and leave it behind |
| 🎯 **Semantic Search**           | Find knowledge by meaning. "Authentication patterns" surfaces OAuth notes even when "OAuth" isn't in the text           |
| 🦋 **Task Workflow**             | Plan with epics and tasks, then track execution across sessions and teammates in one place                              |
| 🧪 **Source-Grounded Synthesis** | Draft verified documents from your own memory with citation, freshness, and gap checks                                  |
| 🌊 **Source Ingestion**          | Crawl docs, import sources (mailboxes, archives), and ingest agent transcripts into scoped raw content and graph memory |
| 💎 **Scoped Multi-Tenancy**      | Namespace-isolated graphs, org-scoped content/auth records, and policy gates for personal, project, and team scopes     |

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

```bash
# Shell installer: puts sibyl on your PATH, starts the local stack, opens setup UI
curl -fsSL https://raw.githubusercontent.com/hyperb1iss/sibyl/main/install.sh | sh

# Or Homebrew
brew install hyperb1iss/tap/sibyl && sibyl up
```

Use `--remote` for a CLI-only install against a hosted server, or `--daemon` for the embedded daemon
without the web UI. For containers, `sibyl docker init && sibyl docker up` brings up API, web, and
SurrealDB. Full matrix in [Installation](docs/guide/installation.md).

### First Five Minutes

```bash
# Capture a learning the moment you find it
sibyl remember "Stale auth token bug" \
  "Redis TTL mismatch dropped the cached token early" --kind error_pattern

# Pull it back as working context for your next session
sibyl context "auth token bug" --intent debug

# Load a broader context across accessible projects
sibyl context "review stale auth token and Redis TTL history" --intent review --all

# Package wake-up context for the next coding session
sibyl session bundle
```

## 🪄 The Memory Loop

Sibyl is built around a durable loop that both humans and agents follow:

```
context ──▶ act ──▶ remember ──▶ reflect
   ▲                                │
   └────────────────────────────────┘
```

1. **Context** before you start. `sibyl context "<goal>"` returns a compact context
   pack: active work, decisions, plans, constraints, and recent lessons, scoped to your project.
2. **Act** with that context in hand.
3. **Remember** durable knowledge as you learn it. `sibyl remember` stores decisions, plans, ideas,
   claims, procedures, and gotchas so the next session does not rediscover them.
4. **Reflect** at clean breakpoints. `sibyl reflect` distills raw session notes into reviewable
   memory candidates and can persist them into the graph.

```bash
sibyl context "ship the context graph" --intent build
sibyl remember "Use context packs" "Group memory before dispatching agents" --kind decision
sibyl reflect "We decided X. Next we build Y." --title "Planning checkpoint" --persist
```

Memory is graded, auditable, and scoped. Raw captures stay verbatim, reflection candidates pass an
automatic review before promotion, and a nightly dream-cycle keeps the graph consolidated. See
[Capturing Knowledge](docs/guide/capturing-knowledge.md).

## The CLI

The CLI is the power-user interface: clean output, built for scripting and durable project
workflows.

```bash
# Memory loop
sibyl context "<goal>"                 # Compile working context
sibyl remember "Title" "Body"         # Store durable memory
sibyl reflect "<notes>" --persist     # Distill notes into candidates
sibyl capture "<quick note>"          # Fast verbatim capture
sibyl correct <raw_memory_id>          # Inspect or correct source memory

# Knowledge & graph
sibyl remember "Redis pooling" "Pool size must be >= concurrent requests" --kind rule
sibyl explore related ent_xyz         # Find connected entities
sibyl show <id>                       # Full content by ID

# Task workflow
sibyl task list --status todo,doing
sibyl task start <task_id>
sibyl task complete <task_id> --learnings "Key insight: check TTL first"

# Synthesis, sources, projects, orgs
sibyl synthesis draft "Onboarding guide"
sibyl crawl add "https://docs.example.com" --name "Example Docs"
sibyl ingest claude-code ~/transcripts/   # Import agent transcripts into memory
sibyl docs list                           # Browse document collections
sibyl project link proj_xxx
```

Tasks flow `backlog → todo → doing → review → done → archived` (with a `blocked` side state), and
every list command supports `--json` and `--csv` for scripting. Full command reference:
[`docs/cli/`](docs/cli/).

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

## 🛠️ Skills & Hooks

Skills are how an agent learns to _use_ Sibyl, the bridge that makes "if your agent can run a
command, it speaks Sibyl" real. The loader teaches the memory loop, every CLI verb with the flags
that actually exist on your machine, context-pack usage, and the error patterns to avoid, so agents
stop guessing the interface from stale training data.

**Skills are not Claude-only.** `sibyl skill install` drops the loader into every agent skill root it
knows: Claude Code (`~/.claude/skills`), Codex (`~/.codex/skills`), and the generic `~/.agents/skills`
convention. The same workflow follows you across tools.

```bash
sibyl skill install        # Install the tiny /sibyl loader into every agent skill root
sibyl skill list           # List the version-matched packs the CLI can serve
sibyl skill get core       # Print the full workflow + command contract
moon run hooks:install     # Optional Claude Code context hooks for repo dev
```

The installed skill is deliberately tiny: a loader that points the agent back at the CLI. The real
skill packs (`core`, `quick`, `workflows`, `examples`, `migration`) are **built into the CLI** and
served on demand with `sibyl skill get`, each matched to the exact Sibyl version on the machine.
Upgrade the CLI and the guidance upgrades with it. No stale skill copies drift out of sync, and a
subagent on any host gets the same source of truth from one command.

Hooks are separate and, for now, specific to Claude Code: a single **SessionStart** hook prints a
compact wake-up bundle with active tasks and relevant memory, then the agent owns invoking the
`sibyl` skill and calling `sibyl context` for working context. See [Skills & Hooks](docs/guide/skills.md).

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
- **Storage:** SurrealDB unifies graph, content, and auth in one runtime
- **AI routing:** Anthropic, OpenAI, and Gemini swap per surface; embeddings are pluggable
- **Coordination:** In-process by default; Redis/Valkey is optional for multi-process deployments
- **Build:** moonrepo + uv (Python) + pnpm (TypeScript)

See [Why SurrealDB](docs/guide/why-surreal.md) for the rationale and
[Storage Modes](docs/guide/storage-modes.md) for the mode matrix.

## 🧪 Benchmarks

Sibyl reaches the LongMemEval-S retrieval ceiling on the live `/api/search` path, measured in an
ephemeral CI stack, with no LLM extraction and no LLM reranking.

| Metric      | Value                            |
| ----------- | -------------------------------- |
| `hit@5`     | **100.00%** (500/500)            |
| `recall@5`  | **96.96%** (strict multi-answer) |
| `recall@10` | **98.90%**                       |
| `ndcg@5`    | 94.63%                           |

`hit@5` and strict `recall@5` measure different things, and many published "LongMemEval" numbers are
end-to-end QA accuracy with an LLM judge, a different metric than retrieval recall. Full results and
honest side-by-side positioning: [LongMemEval](docs/testing/longmemeval.md) ·
[AI Memory Landscape](docs/testing/ai-memory-landscape.md).

## Deployment

```bash
# Docker Compose
sibyl docker init && sibyl docker up

# Kubernetes (Helm)
helm install sibyl ./charts/sibyl \
  --set backend.existingSecret=sibyl-secrets \
  --set backend.surreal.existingSecret=sibyl-surreal \
  --set ingress.enabled=true
```

Authentication supports JWT sessions for the web UI, scoped API keys for programmatic access
(`sibyl auth api-key create --scopes mcp,api:read`), GitHub OAuth, and self-service password reset
over SMTP. MCP endpoints enforce Bearer auth when a JWT secret is set. Detailed guides:
[Docker Compose](docs/deployment/docker-compose.md) · [Kubernetes](docs/deployment/kubernetes.md) ·
[Environment](docs/deployment/environment.md) · [Auth reference](docs/api/).

## Knowledge Model

Sibyl models a wide range of entity types so memory stays structured, not just a pile of notes:

- **Work:** `task`, `epic`, `project`, `milestone`
- **Knowledge:** `pattern`, `episode`, `procedure`, `rule`, `guide`, `error_pattern`
- **Memory:** `decision`, `plan`, `idea`, `claim`, `artifact`, `session`, `note`
- **Sources:** `source`, `document`, `domain`

The full registry and how types relate live in [Entity Types](docs/guide/entity-types.md).

## FAQ

### Who is Sibyl for?

**Solo developers** who want durable memory for projects and debugging. **Teams** who want shared
knowledge that compounds. **Anyone** building with AI who wants the memory they generate to stay
theirs, across every tool.

### Do I need AI agents to use Sibyl?

No. The knowledge graph and task system are the core product: documentation, task tracking, captured
learnings, and semantic search over what your team already knows. AI agents make the memory loop
automatic, but they are not required.

### How does it compare to Mem0 / LangMem / similar?

Sibyl is **self-hosted and open source**, so you own your data. It includes a full **task workflow
system**, not just memory, a **web UI** for humans, not just APIs for machines, and it keeps
knowledge, tasks, and docs connected in one graph instead of scattering them across tools. On
retrieval quality, see the [Benchmarks](#-benchmarks) above.

### What LLM APIs do I need?

- **Anthropic, OpenAI, or Gemini** (required): for language-model surfaces such as crawler
  extraction, synthesis, and reflection.
- **OpenAI or Gemini** (required): for embeddings and semantic search.

Providers and models are configurable globally or per surface from the web admin settings. A typical
solo developer uses around $5/month in API costs.

### Is it production-ready?

**Yes.** Sibyl is **1.0**. SurrealDB is the runtime for graph, content, and auth. **We use Sibyl to
build Sibyl.** Every feature, task, and learning you see here was tracked inside the system itself.

## 🎯 What's Next

Past 1.0, the work is making memory more automatic and the sources deeper:

- **Memory Workspace OS:** automatic memory decisions become visible, explainable, correctable, and
  undoable from one product surface.
- **Correction propagation:** edits, redactions, and rollbacks flow forward into future recall and
  synthesis, not just the record you touched.
- **Broader ingestion:** source import reaches past mailbox-style archives into more corpora, scoped
  and provenance-tracked.

Already shipped on the way here: a Surreal-only default runtime, plus source, document, and agent
transcript ingestion via `sibyl ingest` and `sibyl docs`. See
[`docs/architecture/SIBYL_1_0_ROADMAP.md`](docs/architecture/SIBYL_1_0_ROADMAP.md) for the full
direction.

## 💜 Philosophy

**Recall before you act.** The graph knows things, so pull context before you code. **Work in task
context** for traceability, progress, and knowledge linking. **Remember what you learn:** if it took
time to figure out, save it so the next session doesn't pay for it twice.

```bash
sibyl remember "Descriptive title" "What, why, how, caveats" --kind decision
```

> **Bad:** "Fixed the bug." **Good:** "JWT refresh fails when Redis TTL expires. Root cause: token
> service does not handle WRONGTYPE. Fix: try/except with regeneration fallback."

The graph should be smarter after every session.

## Contributing

We welcome contributions. See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

```bash
# One-line setup (installs proto, moon, toolchain, dependencies)
./setup-dev.sh              # macOS / Linux
pwsh -File .\setup-dev.ps1  # Windows (PowerShell 7+)

# Configure your shell, then launch the local-dev stack
export SIBYL_OPENAI_API_KEY=sk-...   # embeddings; SIBYL_JWT_SECRET auto-generates in dev
moon run install-dev                 # editable CLI installs
moon run dev                         # SurrealDB + API + web

# Verify, then run the quality gates before a PR
curl http://localhost:3334/api/health
moon run :check                      # lint + typecheck + test
```

`moon run dev` is the single-machine flow: with `SIBYL_SURREAL_URL` unset it starts local SurrealDB,
points the API at `ws://127.0.0.1:8000/rpc`, and stores data in `.moon/cache/surreal-dev`. Set
`SIBYL_SURREAL_URL` to use a hosted endpoint, including Surreal Cloud.

| Service      | Port | URL                     |
| ------------ | ---- | ----------------------- |
| API + MCP    | 3334 | http://localhost:3334   |
| Web UI       | 3337 | http://localhost:3337   |
| SurrealDB    | 8000 | ws://localhost:8000/rpc |
| Redis/Valkey | 6381 | optional                |

## License

Apache-2.0. See [LICENSE](LICENSE).

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
