---
layout: home

hero:
  name: Sibyl
  text: Cross-agent memory for AI coding tools
  tagline:
    One self-hostable knowledge graph, shared across Claude Code, Codex, OpenCode, Cursor, and the
    agents you build. If it runs a shell command, it speaks Sibyl.
  actions:
    - theme: brand
      text: Get Started
      link: /guide/
    - theme: alt
      text: View on GitHub
      link: https://github.com/hyperb1iss/sibyl

features:
  - icon: 🔮
    title: Every Agent, One Graph
    details:
      Claude Code, Codex, OpenCode, Cursor, and the agents you build share one durable knowledge
      graph. Tell one tool your context; every tool keeps it.
  - icon: 🪄
    title: The Memory Loop
    details:
      Recall context before you act, remember what you learn, and reflect raw notes into durable
      memory. Built into the CLI, MCP, and hooks.
  - icon: 🎯
    title: Project-Centric Tasks
    details:
      Track work across sessions. Full lifecycle from backlog to completion, with learnings captured
      along the way.
  - icon: ⚡
    title: Find by Meaning
    details:
      Semantic search finds knowledge by intent, not keywords. Ask "how do I handle auth?" and
      surface the relevant patterns instantly.
  - icon: 🧪
    title: Source-Grounded Synthesis
    details:
      Draft verified documents from your own memory, with citation, freshness, and gap checks built
      in.
  - icon: 🌊
    title: Ingest External Sources
    details:
      Crawl documentation sites and import sources into the same graph as your own project
      knowledge.
---

## The Problem

Every app wants to remember you now. Your assistant imports your history from the last one, builds a
profile, and turns the context you generate into someone else's asset. And none of it travels: what
you taught Claude Code, Cursor never sees, so you re-explain the same project to every tool you use.
The question stopped being whether your tools remember and became who the memory works for.

## The Solution

Sibyl is **cross-agent memory** where the answer is you. A knowledge graph, memory loop, and task
workflow shared across every coding agent you use, self-hosted on your own hardware so the memory
works for you and stays yours:

- **Memory:** Store patterns, decisions, and solutions that persist across sessions
- **The Memory Loop:** `recall → act → remember → reflect`, built into every surface
- **Task Tracking:** Manage work across sessions with full lifecycle support
- **Semantic Search:** Find knowledge by meaning, not exact keywords
- **Synthesis:** Generate verified documents grounded in your own memory
- **Source Ingestion:** Crawl external docs and import sources into one graph

## How It Works

```mermaid
flowchart TD
    W["Your Actual Workflow<br/>Claude Code · Editors · Scripts · Teammates"]
    S["Skills<br/>Teach the workflow"]
    H["Hooks<br/>Inject context"]
    C["CLI<br/>Express power"]
    SRV{{"Sibyl Server<br/>SurrealDB graph · Semantic search · Memory loop · Tasks"}}
    UI["Web UI<br/>Human collaboration · Memory workspace · Graph explorer"]
    W --> S & H & C
    S & H & C --> SRV
    SRV --> UI
```

### Skills + Hooks

**Skills** teach your tools and teammates structured workflows:

```bash
# Recall context before implementing
sibyl recall "authentication work" --intent build

# Track work with full lifecycle
sibyl task start task_xyz

# Capture learnings when done
sibyl task complete task_xyz --learnings "OAuth tokens need refresh..."
```

**Hooks** automatically inject relevant knowledge into every prompt so useful context shows up
before you have to go looking for it.

### For Humans: Web UI + CLI

**Web UI** for collaboration and oversight:

- Visual knowledge graph exploration
- Project and task management
- The memory workspace: captures, imports, and synthesis
- Document source configuration
- Team-wide dashboards

**CLI** for power users and scripting:

- The full memory loop from the terminal
- Semantic search and graph navigation
- Task lifecycle management
- Source crawling and ingestion

## Quick Start

```bash
# Start a local daemon
sibyl init --local
sibyl serve

# Capture a learning
sibyl remember "Redis insight" "Pool size must be >= concurrent requests" --kind pattern

# Recall it as working context
sibyl recall "redis connection pool"

# Manage tasks
sibyl task list --status doing
sibyl task complete <task_id> --learnings "OAuth tokens expire after 1 hour"
```

## The Memory Loop

Sibyl is built around a durable cycle that both humans and agents follow:

```mermaid
flowchart LR
    R(["RECALL<br/>Pull working context before you act"])
    A(["ACT<br/>Do the work with context in hand"])
    M(["REMEMBER + REFLECT<br/>Capture learnings, distill session notes"])
    R --> A --> M
    M -.->|the graph gets smarter| R
```

Every completed task makes your knowledge graph smarter. Every pattern discovered helps future
sessions move faster. **The system learns as you work.**

## Why Sibyl?

| Without Sibyl                    | With Sibyl                                |
| -------------------------------- | ----------------------------------------- |
| Agent rediscovers same solutions | Agent recalls existing patterns instantly |
| Context lost between sessions    | Knowledge persists in a graph             |
| Manual prompting required        | Hooks inject context automatically        |
| No task tracking                 | Full lifecycle with learnings capture     |
| Scattered documentation          | Searchable, connected knowledge graph     |

## Get Started

1. **[Installation](./guide/installation)** Set up Sibyl in five minutes
2. **[Quick Start](./guide/quick-start)** Your first knowledge graph session
3. **[Skills & Hooks](./guide/skills)** Teach your tools and teammates the workflow
4. **[Capturing Knowledge](./guide/capturing-knowledge)** Use the memory loop well

---

<p style="text-align: center; opacity: 0.7; margin-top: 3rem;">
Built for projects that deserve to remember.
</p>
