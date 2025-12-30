---
name: sibyl-knowledge
description:
  Graph-RAG knowledge oracle with CLI interface. Use `sibyl` for semantic search, task management,
  knowledge capture, and graph exploration. Invoke when you need persistent memory across sessions,
  pattern/learning lookup, or task tracking. Requires FalkorDB running.
allowed-tools: Bash
---

# Sibyl Knowledge Oracle

Sibyl gives you persistent memory across coding sessions. Search patterns, track tasks, capture
learnings—all stored in a knowledge graph.

## Quick Start

```bash
# Link your directory to a project (one-time setup)
sibyl project link              # Interactive picker
sibyl project link project_abc  # Or specify ID directly

# Check current context
sibyl context

# Now task commands auto-scope to your project!
sibyl task list --status todo   # Only shows tasks for linked project

# Search for knowledge
sibyl search "authentication patterns"

# Quickly add a learning
sibyl add "Redis insight" "Connection pool must be >= concurrent requests"

# Start a task
sibyl task start task_xyz

# Complete with learnings
sibyl task complete task_xyz --learnings "OAuth tokens expire..."
```

**Pro tips:**

- **Link your project first** - then task commands just work without `--project`
- **Table output is default** - use `--json` only for scripting
- Use `--all` flag to bypass context and see all projects

---

## The Agent Feedback Loop

```
1. SEARCH FIRST     -> sibyl search "topic"
2. CHECK TASKS      -> sibyl task list --status doing
3. WORK & CAPTURE   -> sibyl entity create (for learnings)
4. COMPLETE         -> sibyl task complete --learnings "..."
```

---

## Core Commands

### Search - Find Knowledge by Meaning

```bash
# Semantic search across all types
sibyl search "error handling patterns"

# Filter by entity type
sibyl search "OAuth" --type pattern

# Limit results
sibyl search "debugging redis" --limit 5
```

**When to use:** Before implementing anything. Find existing patterns, past solutions, gotchas.

---

### Add - Quick Knowledge Capture

```bash
# Basic: title and content
sibyl add "Title" "What you learned..."

# With metadata
sibyl add "OAuth insight" "Token refresh timing..." -c authentication -l python

# Create a pattern instead of episode
sibyl add "Retry pattern" "Exponential backoff..." --type pattern
```

**When to use:** After discovering something non-obvious. Quick way to capture learnings.

---

### Task Management - Full Lifecycle

```bash
# CREATE a task (requires project)
sibyl task create --title "Implement OAuth" --project proj_abc
sibyl task create --title "Add rate limiting" -p proj_api --priority high
```

**IMPORTANT:** Use `--title` (not `-t`). The `-t` flag was for table output (now default).

```bash
# List tasks (table output is default, comma-separated values supported)
sibyl task list --status todo,doing,blocked
sibyl task list --priority critical,high
sibyl task list --tags bug,urgent

# Filter by epic
sibyl task list --epic epic_abc          # Tasks in specific epic
sibyl task list --no-epic                # Tasks without any epic (orphaned/unplanned)

# Combine filters
sibyl task list --status todo --priority high --feature backend

# Semantic search within tasks
sibyl task list -q "authentication"

# Show task details
sibyl task show task_xyz

# Start working (generates branch name)
sibyl task start task_xyz --assignee alice

# Block with reason
sibyl task block task_xyz --reason "Waiting on API keys"

# Resume blocked task
sibyl task unblock task_xyz

# Submit for review
sibyl task review task_xyz --pr "github.com/.../pull/42"

# Complete with learnings (IMPORTANT: capture what you learned!)
sibyl task complete task_xyz --hours 4.5 --learnings "Token refresh needs..."

# Archive single task
sibyl task archive task_xyz --reason "Completed: implemented feature"

# Direct update
sibyl task update task_xyz --status done --priority high
```

**Task States:** `backlog <-> todo <-> doing <-> blocked <-> review <-> done <-> archived`

---

### Project Management

```bash
# List all projects
sibyl project list

# Show project details
sibyl project show proj_abc

# Create a project
sibyl project create --name "Auth System" --description "OAuth and JWT implementation"
```

---

### Project Context (Directory Linking)

Link directories to projects for automatic task scoping.

```bash
# Link current directory to a project
sibyl project link                    # Interactive - pick from list
sibyl project link project_abc123     # Link to specific project

# Check current context
sibyl context

# List all directory-to-project links
sibyl project links

# Remove a link
sibyl project unlink
```

---

### Entity Operations - Generic CRUD

```bash
# List entities by type
sibyl entity list --type pattern
sibyl entity list --type episode

# Show entity details
sibyl entity show entity_xyz

# Create an entity (for capturing learnings)
sibyl entity create --type episode --name "Redis insight" --content "Discovered that..."

# Find related entities
sibyl entity related entity_xyz

# Delete (with confirmation)
sibyl entity delete entity_xyz
```

**Entity Types:** pattern, rule, template, tool, topic, episode, task, project, source, document

---

### Graph Exploration

```bash
# Find related entities (1-hop)
sibyl explore related entity_xyz

# Multi-hop traversal
sibyl explore traverse entity_xyz --depth 2

# Task dependency chain
sibyl explore dependencies task_xyz

# Find path between entities
sibyl explore path from_id to_id
```

---

### Admin & Health

```bash
# Check system health
sibyl health

# Show statistics
sibyl stats

# Show config
sibyl config
```

---

## Common Workflows

### Starting a New Session

```bash
# 1. Check current context
sibyl context

# 2. Check for in-progress work
sibyl task list --status doing

# 3. Or find todo tasks
sibyl task list --status todo

# 4. Start working
sibyl task start task_xyz
```

### Research Before Implementation

```bash
sibyl search "what you're implementing" --type pattern
sibyl search "related topic" --type episode
sibyl search "common mistakes" --type episode
```

### Capture a Learning

```bash
sibyl add "Descriptive title" "What you learned and why it matters"
```

### Complete Task with Learnings

```bash
sibyl task complete task_xyz --hours 4.5 --learnings "Key insight: The OAuth flow requires..."
```

---

## Output Formats

- **Table** (default): Human-readable, clean output
- **JSON**: Add `--json` for scripting
- **CSV**: Add `--csv` for spreadsheet export

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

## Troubleshooting

### Connection errors

```bash
sibyl health
```

If unhealthy, verify the Sibyl server and FalkorDB are running.

### Task list shows wrong project's tasks

```bash
sibyl context                      # See which project you're linked to
sibyl project link                 # Link to correct project
sibyl task list --all              # Bypass context to see all
```

---

## Common Pitfalls

| Wrong                        | Correct                              |
| ---------------------------- | ------------------------------------ |
| `sibyl task add "..."`       | `sibyl task create --title "..."`    |
| `sibyl task list --todo`     | `sibyl task list --status todo`      |
| `sibyl task create -t "..."` | `sibyl task create --title "..."` (!) |

The CLI supports short ID prefixes - it will resolve them automatically:

```bash
sibyl task show task_c24          # Works - CLI finds the full ID
sibyl task show task_c24fc3228e7c # Also works
```

---

## Prerequisites

```bash
sibyl health   # Check connectivity
sibyl setup    # First-time setup
```
