---
name: sibyl-knowledge
description: Graph-RAG knowledge oracle with CLI interface. Use `uv run sibyl` for semantic search, task management, knowledge capture, and graph exploration. Invoke when you need persistent memory across sessions, pattern/learning lookup, or task tracking. Requires FalkorDB running.
allowed-tools: Bash(uv run sibyl:*)
---

# Sibyl Knowledge Oracle

Sibyl gives you persistent memory across coding sessions. Search patterns, track tasks, capture learnings—all stored in a knowledge graph.

## Quick Start

```bash
# Search for knowledge
uv run sibyl search "authentication patterns"

# Quickly add a learning
uv run sibyl add "Redis insight" "Connection pool must be >= concurrent requests"

# List your projects
uv run sibyl project list

# Create a task in a project
uv run sibyl task create --title "Implement OAuth" --project proj_abc --priority high

# List tasks in a project
uv run sibyl task list --project proj_abc --status todo

# Start a task
uv run sibyl task start task_xyz --assignee alice

# Complete with learnings
uv run sibyl task complete task_xyz --learnings "OAuth tokens expire..."
```

---

## The Agent Feedback Loop

```
1. SEARCH FIRST     → uv run sibyl search "topic"
2. CHECK TASKS      → uv run sibyl task list --status doing
3. WORK & CAPTURE   → uv run sibyl entity create (for learnings)
4. COMPLETE         → uv run sibyl task complete --learnings "..."
```

---

## Core Commands

### Search - Find Knowledge by Meaning

```bash
# Semantic search across all types
uv run sibyl search "error handling patterns"

# Filter by entity type
uv run sibyl search "OAuth" --type pattern

# Limit results
uv run sibyl search "debugging redis" --limit 5
```

**When to use:** Before implementing anything. Find existing patterns, past solutions, gotchas.

---

### Add - Quick Knowledge Capture

```bash
# Basic: title and content
uv run sibyl add "Title" "What you learned..."

# With metadata
uv run sibyl add "OAuth insight" "Token refresh timing..." -c authentication -l python

# Auto-link to related entities
uv run sibyl add "Redis pattern" "Connection pooling..." --auto-link

# Create a pattern instead of episode
uv run sibyl add "Retry pattern" "Exponential backoff..." --type pattern
```

**When to use:** After discovering something non-obvious. Quick way to capture learnings.

---

### Task Management - Full Lifecycle

```bash
# CREATE a task (requires project)
uv run sibyl task create --title "Implement OAuth" --project proj_abc
uv run sibyl task create -t "Add rate limiting" -p proj_api --priority high
uv run sibyl task create -t "Fix bug" -p proj_web --assignee alice --tech python,redis
```

```bash
# List tasks (ALWAYS filter by project or status)
uv run sibyl task list --project proj_abc
uv run sibyl task list --status todo
uv run sibyl task list --status doing
uv run sibyl task list --assignee alice

# Show task details
uv run sibyl task show task_xyz

# Start working (generates branch name)
uv run sibyl task start task_xyz --assignee alice

# Block with reason
uv run sibyl task block task_xyz --reason "Waiting on API keys"

# Resume blocked task
uv run sibyl task unblock task_xyz

# Submit for review
uv run sibyl task review task_xyz --pr "github.com/.../pull/42"

# Complete with learnings (IMPORTANT: capture what you learned!)
uv run sibyl task complete task_xyz --hours 4.5 --learnings "Token refresh needs..."

# Archive
uv run sibyl task archive task_xyz --yes

# Direct update (bulk/historical updates)
uv run sibyl task update task_xyz --status done --priority high
uv run sibyl task update task_xyz -s todo -p medium
```

**Task States:** `backlog ↔ todo ↔ doing ↔ blocked ↔ review ↔ done ↔ archived`

(Any transition is allowed for flexibility with historical/bulk data)

---

### Project Management

```bash
# List all projects
uv run sibyl project list

# Show project details
uv run sibyl project show proj_abc

# Create a project
uv run sibyl project create --name "Auth System" --description "OAuth and JWT implementation"
```

---

### Entity Operations - Generic CRUD

```bash
# List entities by type
uv run sibyl entity list --type pattern
uv run sibyl entity list --type episode
uv run sibyl entity list --type rule --language python

# Show entity details
uv run sibyl entity show entity_xyz

# Create an entity (for capturing learnings)
uv run sibyl entity create --type episode --name "Redis insight" --content "Discovered that..."

# Find related entities
uv run sibyl entity related entity_xyz

# Delete (with confirmation)
uv run sibyl entity delete entity_xyz
```

**Entity Types:** pattern, rule, template, tool, topic, episode, task, project, source, document

---

### Graph Exploration

```bash
# Find related entities (1-hop)
uv run sibyl explore related entity_xyz

# Multi-hop traversal
uv run sibyl explore traverse entity_xyz --depth 2

# Task dependency chain
uv run sibyl explore dependencies task_xyz

# Find path between entities
uv run sibyl explore path from_id to_id
```

---

### Admin & Health

```bash
# Check system health
uv run sibyl health

# Show statistics
uv run sibyl stats

# Show config
uv run sibyl config
```

---

## Common Workflows

### Starting a New Session

```bash
# 1. Check for in-progress work
uv run sibyl task list --status doing

# 2. Or find todo tasks in your project
uv run sibyl task list --project proj_abc --status todo

# 3. Start working
uv run sibyl task start task_xyz --assignee you
```

### Research Before Implementation

```bash
# Find patterns
uv run sibyl search "what you're implementing" --type pattern

# Find past learnings
uv run sibyl search "related topic" --type episode

# Check for gotchas
uv run sibyl search "common mistakes" --type episode
```

### Capture a Learning

```bash
# Quick capture
uv run sibyl add "Descriptive title" "What you learned and why it matters"

# With metadata
uv run sibyl add "Descriptive title" "What you learned..." -c debugging -l python --auto-link
```

### Complete Task with Learnings

```bash
# Capture insights when completing
uv run sibyl task complete task_xyz \
  --hours 4.5 \
  --learnings "Key insight: The OAuth flow requires..."
```

---

## Output Formats

**All list commands output JSON by default** (optimal for LLM parsing). Use flags to change:

```bash
# Default: JSON output (clean, structured)
uv run sibyl task list

# Human-readable table
uv run sibyl task list --table
uv run sibyl task list -t

# CSV for spreadsheets
uv run sibyl task list --csv

# Same for other commands
uv run sibyl project list          # JSON
uv run sibyl project list --table  # Table
uv run sibyl entity list --type pattern --csv
```

**Output format by command:**
| Command | Default | Table Flag | CSV Flag |
|---------|---------|------------|----------|
| `task list` | JSON | `--table` / `-t` | `--csv` |
| `project list` | JSON | `--table` / `-t` | `--csv` |
| `entity list` | JSON | `--table` | `--csv` |
| `source list` | JSON | `--table` / `-t` | - |

---

## Key Principles

1. **Search Before Implementing** - Always check for existing knowledge
2. **Project-First for Tasks** - Filter tasks by project, not globally
3. **Capture Non-Obvious Learnings** - If it took time to figure out, save it
4. **Complete with Learnings** - Always capture insights when finishing tasks
5. **Use Entity Types Properly**:
   - `episode` - Temporal insights, debugging discoveries
   - `pattern` - Reusable coding patterns
   - `rule` - Hard constraints, must-follow rules
   - `task` - Work items with lifecycle

---

## Prerequisites

```bash
# Ensure FalkorDB is running
docker compose up -d

# Check health
uv run sibyl health

# If fresh install, run setup
uv run sibyl setup
```
