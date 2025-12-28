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
- **Always use JSON output** (default) - it's structured and jq-parseable
- Use `--all` flag to bypass context and see all projects
- Use `2>&1` when piping to capture all output (spinner goes to stderr)

---

## The Agent Feedback Loop

```
1. SEARCH FIRST     → sibyl search "topic"
2. CHECK TASKS      → sibyl task list --status doing
3. WORK & CAPTURE   → sibyl entity create (for learnings)
4. COMPLETE         → sibyl task complete --learnings "..."
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

# Auto-link to related entities
sibyl add "Redis pattern" "Connection pooling..." --auto-link

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
sibyl task create --title "Fix bug" -p proj_web --assignee alice --tech python,redis
```

**IMPORTANT:** Use `--title` (not `-t`). The `-t` flag is for table output!

```bash
# List tasks (ALWAYS filter by project, status, or priority)
sibyl task list --project proj_abc
sibyl task list --status todo
sibyl task list --status doing
sibyl task list --priority high
sibyl task list --assignee alice

# Multiple statuses (comma-separated)
sibyl task list --status todo,doing,blocked

# Multiple priorities (comma-separated)
sibyl task list --priority critical,high

# Filter by complexity
sibyl task list --complexity simple
sibyl task list --complexity trivial,simple,medium  # Multiple

# Filter by feature area
sibyl task list --feature auth
sibyl task list -f backend           # Short form

# Filter by tags (matches if task has ANY of the tags)
sibyl task list --tags bug
sibyl task list --tags bug,urgent,critical

# Combine multiple filters (all backend-filtered, respects pagination limits)
sibyl task list --status todo --priority high --complexity simple --feature auth

# Semantic search within tasks (use -q for query mode)
sibyl task list -q "authentication"
sibyl task list -q "fix bug" --status todo

# Pagination (max 200 per page)
sibyl task list --status todo --limit 100
sibyl task list --status todo --page 2
sibyl task list --status todo --limit 50 --offset 100

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
sibyl task archive task_xyz --yes

# Bulk archive via stdin (pipe task IDs)
sibyl task list -q "test" --status todo 2>&1 | jq -r '.[].id' | sibyl task archive --stdin --yes

# Direct update (bulk/historical updates)
sibyl task update task_xyz --status done --priority high
sibyl task update task_xyz -s todo -p medium
```

**Task States:** `backlog ↔ todo ↔ doing ↔ blocked ↔ review ↔ done ↔ archived`

(Any transition is allowed for flexibility with historical/bulk data)

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

Link directories to projects for automatic task scoping. Once linked, you don't need `--project`
flags.

```bash
# Link current directory to a project
sibyl project link                    # Interactive - pick from list
sibyl project link project_abc123     # Link to specific project
sibyl project link proj_x --path ~/dev/other  # Link a different path

# Check current context (verify linked project)
cat ~/.sibyl/config.toml              # View path mappings
sibyl project list                    # See all projects

# List all directory-to-project links
sibyl project links

# Remove a link
sibyl project unlink                  # Unlink current directory
sibyl project unlink --path ~/old/project
```

**How it works:**

- Mappings stored in `~/.sibyl/config.toml` under `[paths]`
- Uses longest-prefix matching (so `~/dev/sibyl/web` matches `~/dev/sibyl`)
- Task commands auto-resolve project from cwd
- Use `--all` flag to bypass context: `sibyl task list --all`

---

### Entity Operations - Generic CRUD

```bash
# List entities by type
sibyl entity list --type pattern
sibyl entity list --type episode
sibyl entity list --type rule --language python

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

### First-Time Setup (per project)

```bash
# Link your project directory (one-time)
cd ~/dev/my-project
sibyl project link

# Verify
sibyl context -t
```

### Starting a New Session

```bash
# 1. Check current context (should show your project)
sibyl context

# 2. Check for in-progress work
sibyl task list --status doing

# 3. Or find todo tasks (auto-scoped to project)
sibyl task list --status todo

# 4. Start working
sibyl task start task_xyz
```

### Research Before Implementation

```bash
# Find patterns
sibyl search "what you're implementing" --type pattern

# Find past learnings
sibyl search "related topic" --type episode

# Check for gotchas
sibyl search "common mistakes" --type episode
```

### Capture a Learning

```bash
# Quick capture
sibyl add "Descriptive title" "What you learned and why it matters"

# With metadata
sibyl add "Descriptive title" "What you learned..." -c debugging -l python --auto-link
```

### Complete Task with Learnings

```bash
# Capture insights when completing
sibyl task complete task_xyz \
  --hours 4.5 \
  --learnings "Key insight: The OAuth flow requires..."
```

---

## Output Formats

- **JSON** (default): Parse with `jq`, embeddings auto-stripped
- **Table**: Add `-t` for human-readable output
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

## CLI vs MCP Tools

Both work well. Choose based on context:

| Use Case              | Best Choice              |
| --------------------- | ------------------------ |
| Single operations     | Either works             |
| Bulk/batch operations | CLI (pipes, scripts)     |
| Output formatting     | CLI (`--table`, `--csv`) |
| Direct from agent     | MCP tools are fine       |

Both require the Sibyl server to be reachable. Use `sibyl health` to verify connectivity.

---

## Troubleshooting

### Connection errors

Check server health first:

```bash
sibyl health
```

If unhealthy, verify the Sibyl server and FalkorDB are running. Check your `SIBYL_API_URL` if using
a remote server.

### Task list shows wrong project's tasks

Check your project context:

```bash
sibyl context                      # See which project you're linked to
sibyl project link                 # Link to correct project
sibyl task list --all              # Bypass context to see all
```

### Task list shows old/test data

Filter by status to focus:

```bash
sibyl task list --status todo      # Active work only
```

---

## Quick jq Recipes

```bash
# Count tasks
sibyl task list --status todo 2>&1 | jq 'length'

# List names
sibyl task list --status todo 2>&1 | jq -r '.[].name'

# Filter by priority/complexity/feature/tags (USE THE FLAGS, not jq!)
sibyl task list --priority high              # Backend filters - efficient!
sibyl task list --priority critical,high     # Multiple priorities
sibyl task list --complexity simple          # Filter by complexity
sibyl task list --feature auth               # Filter by feature area
sibyl task list --tags bug,urgent            # Filter by tags (ANY match)

# Combine filters (all backend-filtered, respects pagination limits)
sibyl task list --status todo --priority high --feature backend

# Export CSV
sibyl task list --status todo --csv > tasks.csv
```

See WORKFLOWS.md for advanced recipes.

---

## Common Pitfalls (AVOID THESE)

Based on analysis of 200MB of conversation history, these are the most common CLI mistakes:

### Wrong Command Names

| ❌ Wrong                  | ✅ Correct                        | Note                          |
| ------------------------- | --------------------------------- | ----------------------------- |
| `sibyl task add "..."`    | `sibyl task create --title "..."` | Use `create` not `add`        |
| `sibyl task list --todo`  | `sibyl task list --status todo`   | Status is a value, not a flag |
| `sibyl task list --doing` | `sibyl task list --status doing`  | Same issue                    |

### Non-Existent Flags

| ❌ Won't Work                    | Why                         | ✅ Alternative                |
| -------------------------------- | --------------------------- | ----------------------------- |
| `--json`                         | JSON is already the default | Just omit it                  |
| `--description` on `task update` | Only at creation            | Set in `task create -d "..."` |
| `--format json`                  | Not a flag                  | JSON is default, just omit    |
| `--format csv`                   | Not a flag                  | Use `--csv` instead           |

### Title Flag Confusion

```bash
# ❌ WRONG: -t is for TABLE output, not title!
sibyl task create -t "My Task" -p project_id

# ✅ CORRECT: Use --title (no short form)
sibyl task create --title "My Task" -p project_id
```

### JSON Field Names

When parsing CLI JSON output with jq, tasks use `name` not `title`:

```bash
# ❌ WRONG: Field is called "name", not "title"
sibyl task list --status todo 2>&1 | jq '.[].title'

# ✅ CORRECT
sibyl task list --status todo 2>&1 | jq '.[].name'
```

### Multiline Commands Break

Trailing spaces after `\` cause "Got unexpected extra arguments":

```bash
# ❌ WRONG: Space after backslash
sibyl task create --title "Fix" \
  --priority high

# ✅ CORRECT: Single line or no trailing spaces
sibyl task create --title "Fix" --priority high -p project_id
```

### Error Handling

When piping to jq, failed commands send error text instead of JSON:

```bash
# If server is down, jq will fail with parse error
# Check server first: sibyl health
```

### Task ID Resolution

The CLI supports short ID prefixes - it will resolve them automatically:

```bash
# Both work - CLI finds the full ID
sibyl task show task_c24
sibyl task show task_c24fc3228e7c

# Only fails if prefix matches multiple tasks (ambiguous)
```

---

## Prerequisites

```bash
# Check connectivity
sibyl health

# First-time setup (checks environment, dependencies)
sibyl setup
```
