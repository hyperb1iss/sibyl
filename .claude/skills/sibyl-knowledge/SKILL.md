---
name: sibyl-knowledge
description: Graph-RAG knowledge oracle with CLI interface. Use `uv run sibyl` for semantic search, task management, knowledge capture, and graph exploration. Invoke when you need persistent memory across sessions, pattern/learning lookup, or task tracking. Requires FalkorDB running.
allowed-tools: Bash
---

# Sibyl Knowledge Oracle

Sibyl gives you persistent memory across coding sessions. Search patterns, track tasks, capture learnings—all stored in a knowledge graph.

## Quick Start

```bash
# Search for knowledge
uv run sibyl search "authentication patterns"

# Quickly add a learning
uv run sibyl add "Redis insight" "Connection pool must be >= concurrent requests"

# List tasks (always filter with --status to reduce noise)
uv run sibyl task list --status todo
uv run sibyl task list --status done

# List projects
uv run sibyl project list

# Create a task in a project
uv run sibyl task create --title "Implement OAuth" --project proj_abc --priority high

# Start a task
uv run sibyl task start task_xyz --assignee alice

# Complete with learnings
uv run sibyl task complete task_xyz --learnings "OAuth tokens expire..."
```

**Pro tips:**
- **Always use JSON output** (default) - it's structured and jq-parseable
- Always filter with `--status` or `--project` to avoid noise
- Use `2>&1` when piping to capture all output (spinner goes to stderr)
- Parse with `jq` for reliable field extraction

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

**Always use JSON output** (the default). Parse with `jq`.

### Extracting Data with jq

```bash
# Extract just task names
uv run sibyl task list --status todo 2>&1 | jq -r '.[].name'

# Count tasks by status
uv run sibyl task list --status todo 2>&1 | jq 'length'

# Get names and priorities
uv run sibyl task list --status todo 2>&1 | jq -r '.[] | "\(.metadata.priority)\t\(.name)"'

# Filter by priority
uv run sibyl task list --status todo 2>&1 | jq -r '.[] | select(.metadata.priority == "high") | .name'

# Get tasks grouped by feature
uv run sibyl task list --status todo 2>&1 | jq -r 'group_by(.metadata.feature) | .[] | "\(.[0].metadata.feature // "none"):\n\(.[].name | "  - \(.)")"'

# Sorted by priority (critical first)
uv run sibyl task list --status todo 2>&1 | jq -r 'sort_by(.metadata.priority) | .[].name'
```

### JSON Output Notes

- **Clean output**: Embeddings are automatically stripped from CLI output
- **Valid JSON**: Output is properly escaped and jq-parseable

### CSV Export (Alternative)

```bash
uv run sibyl task list --csv
uv run sibyl entity list --type pattern --csv
```

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

## CLI vs MCP Tools

**Prefer CLI over MCP tools** (`mcp__sibyl__*`):

| Aspect | CLI (`uv run sibyl`) | MCP Tools |
|--------|---------------------|-----------|
| Reliability | Always works | May have session issues |
| Output control | `--table`, `--csv`, JSON | JSON only |
| Bulk operations | Pipes, grep, scripts | One call at a time |
| Status filtering | `--status`, `--project` | Parameters in JSON |

The MCP tools (`mcp__sibyl__search`, `mcp__sibyl__add`, etc.) are available but may return session errors if the server isn't running. **CLI is the reliable path.**

---

## Troubleshooting

### "No valid session ID" from MCP tools
The Sibyl MCP server isn't running. Use CLI instead:
```bash
uv run sibyl search "query"  # Instead of mcp__sibyl__search
```

### FalkorDB connection errors
```bash
# Check if FalkorDB is running
docker ps | grep falkordb

# Start it
docker compose up -d

# Verify
uv run sibyl health
```

### Task list shows old/test data
Filter by status or project to focus:
```bash
uv run sibyl task list --status todo      # Active work only
uv run sibyl task list --project my-proj  # Specific project
```

---

## Task Reporting Recipes

### Get task counts by status
```bash
echo "TODO: $(uv run sibyl task list --status todo 2>&1 | jq 'length')"
echo "DOING: $(uv run sibyl task list --status doing 2>&1 | jq 'length')"
echo "DONE: $(uv run sibyl task list --status done 2>&1 | jq 'length')"
```

### List task names by status
```bash
uv run sibyl task list --status todo 2>&1 | jq -r '.[].name'
```

### List tasks with priority and feature (sorted)
```bash
# Priority + feature + name, sorted
uv run sibyl task list --status todo 2>&1 | jq -r '.[] | "\(.metadata.priority)\t\(.metadata.feature // "-")\t\(.name)"' | sort
```

### Filter by priority
```bash
# Only critical/high priority
uv run sibyl task list --status todo 2>&1 | jq -r '.[] | select(.metadata.priority == "critical" or .metadata.priority == "high") | .name'
```

### Group by feature
```bash
uv run sibyl task list --status todo 2>&1 | jq -r 'group_by(.metadata.feature) | .[] | "\(.[0].metadata.feature // "other"):", (.[].name | "  - \(.)")'
```

### Export for external tools
```bash
# CSV export for spreadsheets
uv run sibyl task list --status todo --csv > tasks.csv
```

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
