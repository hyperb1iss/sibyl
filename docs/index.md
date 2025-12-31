---
layout: home

hero:
  name: Sibyl
  text: Your AI's Memory
  tagline: Stop watching your agents rediscover the same solutions every session. Sibyl gives them persistent memory, semantic search, and the ability to learn from each other.
  actions:
    - theme: brand
      text: Get Started
      link: /guide/
    - theme: alt
      text: View on GitHub
      link: https://github.com/hyperb1iss/sibyl

features:
  - icon: 🧠
    title: Agents That Remember
    details: Knowledge survives across sessions. Your agents remember patterns, gotchas, and hard-won lessons—forever.
  - icon: 🎯
    title: Skills-Forward Design
    details: Skills teach your agents HOW to work. Hooks inject relevant knowledge automatically. No manual prompting needed.
  - icon: 📋
    title: Project-Centric Tasks
    details: Track work across agents and sessions. Full lifecycle from backlog to completion, with learnings captured along the way.
  - icon: 🔍
    title: Find by Meaning
    details: Semantic search finds knowledge by intent, not keywords. Ask "how do I handle auth?" and find relevant patterns instantly.
  - icon: 📚
    title: Ingest External Docs
    details: Crawl documentation sites and make them searchable. Your agents can reference React docs, API references, anything.
  - icon: 🤝
    title: Multi-Agent Collaboration
    details: When one agent learns something, all agents benefit. Build collective intelligence across your entire workflow.
---

## The Problem

Every time you start a new Claude Code session, your agent forgets everything. That OAuth gotcha you debugged for 2 hours? Gone. The pattern that finally made your tests pass? Vanished. The configuration quirk that took forever to figure out? Lost to the void.

**Your AI agents are brilliant but amnesiac.**

## The Solution

Sibyl is a **Collective Intelligence Runtime**—a persistent knowledge graph that gives your AI agents:

- **Memory**: Store patterns, learnings, and solutions that persist forever
- **Task Tracking**: Manage work across sessions with full lifecycle support
- **Semantic Search**: Find knowledge by meaning, not exact keywords
- **Document Ingestion**: Crawl external docs and make them searchable
- **Multi-Agent Coordination**: Share knowledge across all your agents

## How It Works

```
┌──────────────────────────────────────────────────────────────────┐
│                       Your AI Agents                              │
│    Claude Code • Custom Agents • Multi-Agent Systems             │
└────────────────────────────┬─────────────────────────────────────┘
                             │
        ┌────────────────────┼────────────────────┐
        │                    │                    │
        ▼                    ▼                    ▼
   ┌─────────┐        ┌───────────┐        ┌──────────┐
   │ Skills  │        │   Hooks   │        │   CLI    │
   │ Teach   │        │  Inject   │        │ Express  │
   │ workflow│        │  context  │        │  power   │
   └─────────┘        └───────────┘        └──────────┘
        │                    │                    │
        └────────────────────┼────────────────────┘
                             │
                             ▼
┌──────────────────────────────────────────────────────────────────┐
│                        Sibyl Server                               │
│  Knowledge Graph (FalkorDB) • Semantic Search • Task Management  │
└──────────────────────────────────────────────────────────────────┘
                             │
                             ▼
┌──────────────────────────────────────────────────────────────────┐
│                         Web UI                                    │
│      Human collaboration • Project management • Graph explorer   │
└──────────────────────────────────────────────────────────────────┘
```

### For Agents: Skills + Hooks

**Skills** teach your agents structured workflows:
```bash
# The agent learns to search before implementing
sibyl search "authentication patterns"

# Track work with full lifecycle
sibyl task start task_xyz

# Capture learnings when done
sibyl task complete task_xyz --learnings "OAuth tokens need refresh..."
```

**Hooks** automatically inject relevant knowledge into every prompt—your agent doesn't even need to ask.

### For Humans: Web UI + CLI

**Web UI** for collaboration and oversight:
- Visual knowledge graph exploration
- Project and task management
- Document source configuration
- Team-wide dashboards

**CLI** for power users and scripting:
- Full CRUD on all entities
- Semantic search from terminal
- Task lifecycle management
- Source crawling and ingestion

## Quick Start

```bash
# Install the CLI
pip install sibyl-cli

# Start the infrastructure
docker run -d --name falkordb -p 6380:6379 falkordb/falkordb:latest

# Configure and start
cd sibyl && moon run dev

# Search your knowledge
sibyl search "authentication patterns"

# Add a learning
sibyl add "Redis insight" "Connection pool must be >= concurrent requests"

# Manage tasks
sibyl task list --status doing
sibyl task complete task_xyz --learnings "OAuth tokens expire after 1 hour"
```

## The Workflow

Sibyl enforces a simple but powerful cycle:

```
┌─────────────────────────────────────────────────────┐
│  1. RESEARCH                                        │
│     Search for existing patterns before coding      │
│     sibyl search "what you're implementing"         │
└─────────────────────────┬───────────────────────────┘
                          ▼
┌─────────────────────────────────────────────────────┐
│  2. DO                                              │
│     Work on your task with context from search      │
│     sibyl task start task_xyz                       │
└─────────────────────────┬───────────────────────────┘
                          ▼
┌─────────────────────────────────────────────────────┐
│  3. REFLECT                                         │
│     Capture learnings for future agents             │
│     sibyl task complete task_xyz --learnings "..."  │
└─────────────────────────────────────────────────────┘
```

Every completed task makes your knowledge graph smarter. Every pattern discovered helps future agents work faster. **The system learns as you work.**

## Why Sibyl?

| Without Sibyl | With Sibyl |
|---------------|------------|
| Agent rediscovers same solutions | Agent finds existing patterns instantly |
| Context lost between sessions | Knowledge persists forever |
| Manual prompting required | Hooks inject context automatically |
| No task tracking | Full lifecycle with learnings capture |
| Scattered documentation | Searchable, connected knowledge graph |

## Get Started

1. **[Installation](./guide/installation)** — Set up Sibyl in 5 minutes
2. **[Quick Start](./guide/quick-start)** — Your first knowledge graph session
3. **[Skills & Hooks](./guide/skills)** — Teach your agents the workflow
4. **[Web UI Tour](./guide/)** — Manage your knowledge visually

---

<p style="text-align: center; opacity: 0.7; margin-top: 3rem;">
Built for AI agents that deserve to remember.
</p>
