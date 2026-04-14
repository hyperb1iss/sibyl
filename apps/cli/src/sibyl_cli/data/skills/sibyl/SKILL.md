---
name: sibyl
description:
  Graph-RAG knowledge system with CLI interface. Use for semantic search, task management, knowledge
  capture, project audits, and sprint planning. Invoke when you need persistent memory across
  sessions, pattern/learning lookup, or task tracking. Requires FalkorDB running.
allowed-tools: Bash, Grep, Glob, Read
---

# Sibyl

Sibyl gives you persistent memory across coding sessions. Search patterns, track tasks, capture
learnings—all stored in a knowledge graph.

## Agent Rules (READ FIRST)

These rules exist because real agent sessions consistently fail without them.

1. **NEVER redirect stderr.** Do not append `2>/dev/null` to sibyl commands. Error messages contain
   diagnostic information you need. Suppressing them causes silent failures and blind retry spirals.

2. **Link your project BEFORE doing anything else.** Run `sibyl context` first. If it shows
   `Project: none`, you MUST run `sibyl project link <id>` before searching or listing tasks.
   Without a link, searches return results from unrelated projects and task lists show global noise.

3. **Always complete the retrieval pattern.** Search returns truncated previews. When you need
   details, follow up with `sibyl entity show <id>` using the ID from the search result. Working
   from truncated summaries leads to incomplete understanding.

4. **Capture learnings proactively.** When you solve something non-obvious, run `sibyl add` or use
   `--learnings` on task completion. Do not ask permission first—the whole point is building
   institutional memory.

5. **Check health before retrying.** If a command fails with a connection error, run `sibyl health`.
   If the server is down, don't retry the same command. Report it and move on.

6. **Never invent subcommands.** If you're unsure whether a command exists, run
   `sibyl <group> --help`. Do not guess. Commands like `sibyl auth token`, `sibyl db backup`, and
   `sibyl explore path` do not exist.

---

## Quick Start

```bash
# 1. Check connection
sibyl health

# 2. Link your directory to a project (one-time, critical!)
sibyl project list                        # Find your project ID
sibyl project link proj_a1b2c3d4e5f6      # Link cwd to that project
sibyl context                             # Verify: should show your project

# 3. Now task commands auto-scope to your project
sibyl task list --status todo   # Only shows tasks for linked project

# 4. Search for knowledge
sibyl search "authentication patterns"

# 5. Get full content from a search result
sibyl entity show "episode:abc123-uuid-here"

# 6. Add a learning
sibyl add "Redis insight" "Connection pool must be >= concurrent requests"

# 7. Start a task
sibyl task start task_a1b2c3d4e5f6

# 8. Complete with learnings
sibyl task complete task_a1b2c3d4e5f6 --learnings "OAuth tokens expire..."
```

**Pro tips:**

- **Link your project first** — then task commands just work without `--project`
- **Table output is default** — use `--json` only for scripting
- Use `--all` flag to bypass context and see all projects

---

## The Agent Feedback Loop

```
1. SEARCH           -> sibyl search "topic"
2. RETRIEVE         -> sibyl entity show <id>  (get full content by ID from search)
3. CHECK TASKS      -> sibyl task list --status doing
4. WORK & CAPTURE   -> sibyl add "Title" "Learning..."
5. COMPLETE         -> sibyl task complete --learnings "..."
```

**Key insight:** Search shows IDs. Use `sibyl entity show <id>` to fetch full content.

---

## Task Data Model

### Task States

```
backlog <-> todo <-> doing <-> blocked <-> review <-> done -> archived
```

### Priority Levels

| Priority   | When to Use                                |
| ---------- | ------------------------------------------ |
| `critical` | Production bugs, security issues, blockers |
| `high`     | Core functionality bugs, blocking features |
| `medium`   | Standard features, improvements            |
| `low`      | Nice-to-haves, polish, future work         |
| `someday`  | Backlog parking lot                        |

### Common Tags

`backend`, `frontend`, `database`, `devops`, `bug`, `feature`, `refactor`, `chore`, `security`,
`performance`, `testing`

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

# Search across all projects (bypass context)
sibyl search "python conventions" --all
```

**Output includes:**

- Document name and source
- Section path (heading hierarchy)
- Content preview
- **Full entity ID** for retrieval

**Two-step retrieval pattern:**

```bash
# 1. Search to find relevant knowledge
sibyl search "redis connection pooling"
# Output shows full IDs like: convention:abe924cb-8cee-4cb5-...

# 2. Fetch full content by ID (copy from search output)
sibyl entity show "convention:abe924cb-8cee-4cb5-9dd1-818201c1c946"
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
# CREATE a task (project auto-resolves from linked directory)
sibyl task create --title "Implement OAuth"
sibyl task create --title "Add rate limiting" --priority high --epic epic_a1b2c3d4e5f6
```

**IMPORTANT:** Use `--title` for the task name. Project auto-resolves from linked directory.

```bash
# List tasks (table output is default, comma-separated values supported)
sibyl task list --status todo,doing,blocked
sibyl task list --priority critical,high
sibyl task list --tags bug,urgent

# Filter by epic
sibyl task list --epic epic_a1b2c3d4e5f6       # Tasks in specific epic
sibyl task list --no-epic                # Tasks without any epic (orphaned/unplanned)

# Combine filters
sibyl task list --status todo --priority high --feature backend

# Semantic search within tasks (powerful!)
sibyl task list -q "authentication"   # Find tasks by meaning, not just text match

# Show task details
sibyl task show task_a1b2c3d4e5f6

# Start working (generates branch name)
sibyl task start task_a1b2c3d4e5f6

# Block with reason
sibyl task block task_a1b2c3d4e5f6 --reason "Waiting on API keys"

# Resume blocked task
sibyl task unblock task_a1b2c3d4e5f6

# Submit for review
sibyl task review task_a1b2c3d4e5f6 --pr "github.com/.../pull/42"

# ⚠️ COMPLETE WITH LEARNINGS - always use this to finish tasks!
# This marks done AND creates a searchable episode in the knowledge graph
sibyl task complete task_a1b2c3d4e5f6 --hours 4.5 --learnings "Token refresh needs..."

# Archive single task
sibyl task archive task_a1b2c3d4e5f6 --reason "Superseded by new approach"

# Direct update (use sparingly - prefer `complete --learnings` for finishing work)
sibyl task update task_a1b2c3d4e5f6 --status done --priority high

# Add a note DURING work (progress breadcrumbs, NOT for completion)
sibyl task note task_a1b2c3d4e5f6 "Found the root cause"
sibyl task note task_a1b2c3d4e5f6 "Implemented fix" --assistant

# List notes for a task
sibyl task notes task_a1b2c3d4e5f6
```

**Task States:** `backlog <-> todo <-> doing <-> blocked <-> review <-> done <-> archived`

---

### Project Management

```bash
# List all projects
sibyl project list

# Show project details
sibyl project show proj_a1b2c3d4e5f6

# Create a project
sibyl project create --name "Auth System" --description "OAuth and JWT implementation"
```

---

### Epic Management (Feature Grouping)

Epics group related tasks into larger features or initiatives.

```bash
sibyl epic list                                    # List epics
sibyl epic list --status in_progress               # Filter by status
sibyl epic create --title "Auth System"            # Create epic
sibyl epic show epic_a1b2c3d4e5f6                  # Show with progress
sibyl epic start epic_a1b2c3d4e5f6                 # Start epic
sibyl epic complete epic_a1b2c3d4e5f6              # Complete epic
sibyl epic archive epic_a1b2c3d4e5f6               # Archive epic
```

**Workflow:** Create epic → create tasks with `--epic` flag → work tasks → complete

**Find tasks in an epic:** `sibyl task list --epic epic_a1b2c3d4e5f6`

---

### Project Context (Directory Linking)

Link directories to projects for automatic task scoping.

```bash
# First, find your project ID
sibyl project list

# Link current directory to a project
sibyl project link proj_a1b2c3d4e5f6     # Requires project ID

# Check current context
sibyl context

# List all directory-to-project links
sibyl project links

# Remove a link
sibyl project unlink
```

**One project per repo:** Each repository should link to exactly one Sibyl project. This enables
automatic task scoping without needing `--project` flags.

---

### Entity Operations - Generic CRUD

```bash
# List entities by type
sibyl entity list --type pattern
sibyl entity list --type episode

# Show entity details (use ID from search)
sibyl entity show epsd_a1b2c3d4e5f6

# Create an entity (for capturing learnings)
sibyl entity create --type episode --name "Redis insight" --content "Discovered that..."

# Find related entities
sibyl entity related epsd_a1b2c3d4e5f6

# Delete (with confirmation)
sibyl entity delete epsd_a1b2c3d4e5f6
```

**Entity Types:** task, epic, project, pattern, episode, document, note, source, placeholder

---

### Graph Exploration

```bash
# Find related entities (1-hop)
sibyl explore related ptrn_a1b2c3d4e5f6

# Multi-hop traversal
sibyl explore traverse ptrn_a1b2c3d4e5f6 --depth 2

# Task dependency chain
sibyl explore dependencies task_a1b2c3d4e5f6

# Project-wide dependencies
sibyl explore dependencies --project proj_a1b2c3d4e5f6
```

---

### Admin & Health

```bash
# Check system health
sibyl health

# Show statistics
sibyl stats

# Show configuration
sibyl config show
```

---

### Documentation & Sources

Sibyl can crawl and index external documentation for RAG search.

```bash
# List crawl sources
sibyl crawl sources

# Add a documentation source
sibyl crawl add "https://docs.example.com" --name "Example Docs" --depth 2

# Start crawling
sibyl crawl ingest source_a1b2c3d4e5f6

# Check crawl status
sibyl crawl status source_a1b2c3d4e5f6

# List crawled documents
sibyl document list --source source_a1b2c3d4e5f6

# Read a crawled document
sibyl document show doc_a1b2c3d4e5f6
```

---

### Context Management

Contexts bundle server, org, and project settings. Useful for switching between environments.

```bash
# Show current context
sibyl context

# List all contexts
sibyl context list

# Create a named context
sibyl context create prod --server https://sibyl.example.com --org myorg --use

# Switch contexts
sibyl context use prod
```

---

### Server Logs & Debugging

Requires OWNER role. Useful when debugging graph issues or unexpected results.

```bash
# View recent logs
sibyl logs tail
sibyl logs tail -l error              # Filter by level
sibyl logs tail -s api -n 100         # Filter by service, more entries

# Search logs
sibyl logs search "timeout" --from 2025-04-01

# Inspect graph schema
sibyl debug schema

# Run read-only Cypher query
sibyl debug query "MATCH (n:Entity) RETURN labels(n), count(*)"

# Database metrics
sibyl debug metrics
```

---

### Entity History (Bi-Temporal)

Query how entities and their relationships changed over time.

```bash
# Full history of an entity
sibyl entity history entity_a1b2c3d4e5f6

# Point-in-time snapshot
sibyl entity history entity_a1b2c3d4e5f6 --as-of 2025-03-15

# Timeline view
sibyl entity history entity_a1b2c3d4e5f6 --mode timeline
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
sibyl task start task_a1b2c3d4e5f6
```

### Research Before Implementation

```bash
sibyl search "what you're implementing" --type pattern
sibyl search "related topic" --type episode
sibyl search "common mistakes" --type episode

# Get full content from any result (use ID from search output)
sibyl entity show <id>
```

### Capture a Learning

```bash
sibyl add "Descriptive title" "What you learned and why it matters"
```

### Complete Task with Learnings

```bash
sibyl task complete task_a1b2c3d4e5f6 --hours 4.5 --learnings "Key insight: The OAuth flow requires..."
```

---

## Output Formats

- **Table** (default): Human-readable, clean output
- **JSON**: Add `--json` for scripting
- **CSV**: Add `--csv` for spreadsheet export

---

## Key Principles

1. **Search Before Implementing** — Always check for existing knowledge
2. **Project-First for Tasks** — Link your directory, then filter by project
3. **Capture Non-Obvious Learnings** — If it took time to figure out, save it
4. **Complete with Learnings** — Always capture insights when finishing tasks
5. **Use Entity Types Properly**:
   - `episode` — Temporal insights, debugging discoveries
   - `pattern` — Reusable coding patterns
   - `note` — Progress breadcrumbs, observations
   - `task` — Work items with lifecycle
   - `document` — Crawled documentation pages

---

## Concurrency & Locking

Sibyl uses distributed locks to prevent data corruption when multiple agents update the same entity
concurrently. This is important because graph operations (especially via Graphiti) can take 20+
seconds.

### How It Works

- **Entity updates and deletes acquire a lock** before modifying the graph
- **Lock TTL is 30 seconds** - automatically released if the process dies
- **Concurrent requests wait** up to 45 seconds for the lock to become available
- **409 Conflict** is returned if the lock cannot be acquired

### Handling Lock Conflicts

If you get a 409 error, the entity is being modified by another process. Simply retry:

```bash
# If this fails with "locked by another process"
sibyl task update task_a1b2c3d4e5f6 --status doing

# Wait a moment and retry
sleep 2
sibyl task update task_a1b2c3d4e5f6 --status doing
```

### For Agents

When making API calls programmatically:

```python
import httpx
import asyncio

async def update_with_retry(task_id: str, updates: dict, max_retries: int = 3):
    for attempt in range(max_retries):
        response = await client.patch(f"/api/tasks/{task_id}", json=updates)
        if response.status_code == 409:  # Locked
            await asyncio.sleep(2 ** attempt)  # Exponential backoff
            continue
        response.raise_for_status()
        return response.json()
    raise Exception(f"Failed to update {task_id} after {max_retries} retries")
```

### Valid Task Statuses

When updating task status, use these exact values:

- `backlog` - Future work, not committed
- `todo` - Committed to sprint
- `doing` - Active development (NOT `in_progress`)
- `blocked` - Waiting on something
- `review` - In code review
- `done` - Completed
- `archived` - Terminal state (no longer active)

**Common mistake:** Using `in_progress` instead of `doing`. The API will reject invalid status
values with a 422 validation error.

---

## Troubleshooting

### Connection errors

```bash
sibyl health
```

If unhealthy, the server or FalkorDB is down. Do not retry commands blindly. Report it and continue
without Sibyl for this session.

### Task list shows wrong project's tasks

This happens when your directory is not linked to a project. All commands return global results.

```bash
sibyl context                      # Check — does it show your project?
sibyl project list                 # Find correct project ID
sibyl project link proj_xxx        # Link to correct project
sibyl context                      # Verify the link worked
```

### "Entity not found" after search returns results

Search results may reference entities by graph UUID. Use the exact ID from search output:

```bash
sibyl entity show "episode:abc123-full-uuid-here"
```

### "Failed to start task" with no details

Usually a lock conflict or invalid state transition. Check the task's current state:

```bash
sibyl task show task_a1b2c3d4e5f6
```

If it's already in `doing`, you don't need to start it. If locked, wait a few seconds and retry.

### Search returns results from other projects

Your directory is not linked. Run `sibyl context` — if `Project: none`, link it first.

---

## Common Pitfalls

| Wrong                                | Correct                                          |
| ------------------------------------ | ------------------------------------------------ |
| `sibyl task add "..."`               | `sibyl task create --title "..."`                |
| `sibyl task list --todo`             | `sibyl task list --status todo`                  |
| `sibyl task create -t "..."`         | `sibyl task create --title "..."` (`-t` is type) |
| `sibyl task update --learnings`      | `sibyl task complete --learnings` (!)            |
| `sibyl task note` for completion     | `sibyl task complete --learnings` (!)            |
| `sibyl add note "content..."`        | `sibyl add "Title" "content..." --type note`     |
| `sibyl search ... 2>/dev/null`       | `sibyl search ...` (never suppress stderr)       |
| `sibyl search ... \|\| true`         | `sibyl search ...` (let errors surface)          |
| `sibyl config`                       | `sibyl config show`                              |
| `sibyl explore path A B`             | Not a real command — use `explore related`       |
| `sibyl auth token`                   | Not a real command — use `sibyl auth status`     |
| Using `--type rule` or `--type tool` | These types don't exist — use `pattern`/`note`   |

### Notes vs Learnings

These are **different things** with different purposes:

| Command                                      | When          | Purpose              | Creates                        |
| -------------------------------------------- | ------------- | -------------------- | ------------------------------ |
| `sibyl task note <id> "..."`                 | During work   | Progress breadcrumbs | Note (task metadata)           |
| `sibyl task complete <id> --learnings "..."` | At completion | Capture insights     | Episode (searchable knowledge) |

**Wrong:** Using `task note` when completing a task **Right:** Using `task complete --learnings` -
this marks done AND creates a searchable episode

```bash
# WRONG - notes are for ongoing work, not completion
sibyl task update task_xxx --status done
sibyl task note task_xxx "What I learned..."

# RIGHT - complete with learnings does both
sibyl task complete task_xxx --learnings "What I learned..."
```

**Full task IDs are required** - always use the complete ID returned by list/search commands:

```bash
sibyl task show task_c24fc3228e7c  # Full ID required (17 chars)
```

---

## Prerequisites

```bash
sibyl health         # Check connectivity
sibyl local setup    # First-time assistant setup
sibyl auth status    # Check authentication
```

---

## MCP Tools (Programmatic Access)

When used as an MCP server, Sibyl exposes 5 tools. These are different from CLI commands.

| MCP Tool  | Purpose                                     |
| --------- | ------------------------------------------- |
| `search`  | Unified semantic search (graph + docs)      |
| `explore` | Browse graph: list, related, traverse, deps |
| `add`     | Add knowledge, tasks, or projects           |
| `manage`  | Task lifecycle, source ops, analysis, admin |
| `logs`    | View server logs (OWNER role required)      |

The `manage` tool accepts an `action` parameter: `start_task`, `block_task`, `unblock_task`,
`submit_review`, `complete_task`, `archive_task`, `update_task`, `crawl`, `sync`, `health`, `stats`,
`estimate`, `prioritize`, `detect_cycles`, `suggest`.
