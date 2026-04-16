---
title: Introduction
description: Build a knowledge graph your team can actually reuse
---

# Introduction

Welcome to Sibyl, the knowledge graph and task workflow that turns scattered notes, tasks, and
hard-won debugging lessons into reusable project memory.

## What You'll Learn

This guide teaches you how to:

1. **Set up your prompts**: Configure CLAUDE.md for effective workflows
2. **Use skills and hooks**: Automatic context injection
3. **Build a conventions repo**: Centralize team patterns
4. **Manage knowledge**: Through the web UI and CLI
5. **Track execution**: Projects, epics, and tasks across sessions
6. **Capture durable learnings**: Turn debugging into reusable memory

## The Philosophy

Sibyl is built on a simple insight: **good work compounds when context survives the session.**

Most coding sessions start from scratch. The OAuth gotcha you figured out yesterday disappears. The
pattern that finally made your tests pass gets buried in scrollback. Useful context exists, but it
isn't structured or searchable.

Sibyl fixes this by providing:

- **Persistent Memory**: Knowledge stored in a graph database survives forever
- **Semantic Search**: Find relevant patterns by meaning, not keywords
- **Automatic Context**: Hooks inject knowledge without manual prompting
- **Structured Workflows**: Skills teach the Research → Do → Reflect cycle

## The Architecture

![Sibyl Dashboard](/screenshots/web-dashboard.png)

Sibyl consists of three main components:

### 1. CLI + Skills

Skills and CLI workflows teach your tools and teammates how to work with Sibyl. When you invoke
`/sibyl` in Claude Code, the assistant receives:

- Command reference for all CLI operations
- Workflow patterns (when to search, when to capture)
- Best practices for knowledge quality

### 2. Hooks (Automatic Context)

Hooks are the magic that makes Sibyl invisible. They run automatically:

- **SessionStart**: Loads your active tasks when you begin a session
- **UserPromptSubmit**: Searches for relevant knowledge on every prompt

Relevant patterns appear automatically in context instead of relying on memory or manual lookup.

### 3. Web UI

The web interface gives you visibility and control:

![Knowledge Graph Visualization](/screenshots/web-graph.png)

**Graph Explorer**: Visualize connections between entities, patterns, and learnings. See how
knowledge clusters and relates.

![Task Management](/screenshots/web-tasks.png)

**Task Management**: Track work across projects with full lifecycle support. Filter by status,
priority, assignee, and more.

![Entity Browser](/screenshots/web-entities.png)

**Entity Browser**: Browse all knowledge types (patterns, episodes, conventions, rules). Search and
filter to find what you need.

![Semantic Search](/screenshots/web-search.png)

**Semantic Search**: Find knowledge by meaning across all entity types, documentation, and code.

## The Workflow

Every effective Sibyl workflow follows the same cycle:

```
┌─────────────────────────────────────────────────────────┐
│  RESEARCH                                               │
│  Before implementing anything, search for existing      │
│  patterns. Your past self (or teammates) may have       │
│  already solved this problem.                           │
│                                                         │
│  sibyl search "what you're about to implement"          │
└─────────────────────────┬───────────────────────────────┘
                          ▼
┌─────────────────────────────────────────────────────────┐
│  DO                                                     │
│  Work on your task with the context you found.          │
│  Track progress with task lifecycle commands.           │
│                                                         │
│  sibyl task start task_xyz                              │
└─────────────────────────┬───────────────────────────────┘
                          ▼
┌─────────────────────────────────────────────────────────┐
│  REFLECT                                                │
│  When you finish, capture what you learned.             │
│  Future sessions will thank you.                        │
│                                                         │
│  sibyl task complete task_xyz --learnings "..."         │
│  sibyl add "Pattern Title" "What you discovered..."     │
└─────────────────────────────────────────────────────────┘
```

## What to Capture

Not everything belongs in the knowledge graph. Focus on:

### Always Capture

- **Non-obvious solutions**: If it took time to figure out, save it
- **Gotchas and quirks**: Configuration issues, platform differences
- **Architectural decisions**: Why you chose approach A over B
- **Error patterns**: Problems and their root causes

### Consider Capturing

- **Useful patterns**: Reusable code structures
- **Performance findings**: What made things faster
- **Integration approaches**: How to connect systems

### Skip

- **Trivial info**: Things obvious from documentation
- **Temporary hacks**: Quick fixes that should be replaced
- **Well-documented basics**: Standard library usage

## Quality Bar

The knowledge graph gets smarter with every entry, but only if entries are high quality.

**Bad entry:**

> "Fixed the auth bug"

**Good entry:**

> "JWT refresh tokens fail silently when Redis TTL expires. Root cause: token service doesn't handle
> WRONGTYPE error. Fix: Add try/except with token regeneration fallback. Prevention: Always handle
> Redis type mismatches in token renewal logic."

The good entry includes:

- What happened
- Root cause
- How to fix it
- How to prevent it

## CLI vs MCP vs Web UI

Sibyl offers three interfaces, each suited to different users:

| Interface  | Best For                      | Token Usage              |
| ---------- | ----------------------------- | ------------------------ |
| **CLI**    | Scripts, workflows, quick ops | Low (text output only)     |
| **MCP**    | Direct tool invocation        | Higher (full JSON schemas) |
| **Web UI** | Humans managing projects      | N/A (visual interface)     |

For routine operations, **prefer the CLI**. It's expressive, scriptable, and lighter-weight than MCP
tool calls.

## Next Steps

### Getting Started

1. **[Installation](./installation)**: Get Sibyl running locally
2. **[Quick Start](./quick-start)**: Create your first knowledge entries

### Working Effectively

3. **[Setting Up Prompts](./setting-up-prompts)**: Configure your CLAUDE.md
4. **[Skills & Hooks](./skills)**: Automatic context injection
5. **[Conventions Repository](./conventions-repository)**: Centralize team patterns

### Core Concepts

6. **[Knowledge Graph](./knowledge-graph)**: Understand the data model
7. **[Task Management](./task-management)**: Track work across sessions
8. **[Sources](./capturing-knowledge)**: Ingest external documentation
