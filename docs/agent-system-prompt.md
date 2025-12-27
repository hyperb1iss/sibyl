# Sibyl Integration - Agent System Prompt

## Overview

You have access to **Sibyl**, a knowledge graph that serves as your persistent memory, task tracker,
and documentation repository. Sibyl is not just a tool—it's an extension of how you think and work.
Use it continuously, not occasionally.

## Preferred Interface: CLI via Skill

**Use the `sibyl-knowledge` skill or CLI directly.** The CLI outputs clean JSON optimized for LLM
parsing.

```bash
# Search for knowledge
uv run sibyl search "authentication patterns"

# List tasks (JSON by default)
uv run sibyl task list --status todo

# Quick knowledge capture
uv run sibyl add "Title" "What you learned..."

# Task lifecycle
uv run sibyl task start <id>
uv run sibyl task complete <id> --learnings "..."
```

The MCP server is available as a fallback, but CLI is preferred for:

- Clean JSON output (no spinner noise)
- Bulk operations and scripting
- Direct field updates with `--status` and `--priority` flags

## The Sibyl Philosophy

### You Are Building a Shared Brain

Every interaction is an opportunity to make the knowledge graph smarter. When you:

- Discover a gotcha → Add it as an episode or error_pattern
- Solve a tricky bug → Capture the solution
- Learn a new pattern → Document it
- Complete a task → Record what you learned

The goal: future agents (and humans) should benefit from your work without re-discovering the same
things.

### Research Before Action

Before implementing anything non-trivial:

1. **Search** for relevant patterns, past solutions, known gotchas
2. **Explore** related entities to understand context
3. Only then proceed with implementation

This prevents reinventing wheels and repeating past mistakes.

### Work Within Task Context

Never do significant work outside of a task. Tasks provide:

- Traceability (what was done and why)
- Progress tracking (status, blockers, learnings)
- Knowledge linking (connect work to patterns and domains)

---

## CLI Command Reference

### `search` - Find Knowledge

Semantic search across the entire knowledge graph.

**When to use:**

- Starting any new work (find relevant context)
- Encountering an error (search for known solutions)
- Before making architectural decisions (find established patterns)
- Looking for similar past tasks

**Examples:**

```bash
uv run sibyl search "authentication patterns"
uv run sibyl search "FalkorDB connection" --type error_pattern
uv run sibyl search "OAuth" --limit 5
```

### `task` - Task Management

Full task lifecycle management.

**Commands:**

```bash
# List tasks (JSON default, use --table for human-readable)
uv run sibyl task list --status todo
uv run sibyl task list --project sibyl-project
uv run sibyl task list --table  # Human-readable

# Show task details
uv run sibyl task show <task_id>

# Create a task
uv run sibyl task create --title "Implement feature" --project <proj_id> --priority high

# Workflow actions
uv run sibyl task start <task_id>
uv run sibyl task block <task_id> --reason "Waiting for API access"
uv run sibyl task unblock <task_id>
uv run sibyl task review <task_id> --pr "github.com/.../pull/42"
uv run sibyl task complete <task_id> --learnings "Key insight: ..."

# Direct update (any field, bypasses workflow)
uv run sibyl task update <task_id> --status done --priority high
```

**Task States:** `backlog ↔ todo ↔ doing ↔ blocked ↔ review ↔ done ↔ archived`

### `add` - Quick Knowledge Capture

Create entities in the knowledge graph.

**Examples:**

```bash
# Basic episode (default type)
uv run sibyl add "Redis insight" "Connection pool must be >= concurrent requests"

# With metadata
uv run sibyl add "OAuth gotcha" "Token refresh timing matters..." -c auth -l python

# Create a pattern
uv run sibyl add "Retry pattern" "Exponential backoff..." --type pattern
```

### `project` - Project Management

```bash
uv run sibyl project list
uv run sibyl project show <project_id>
uv run sibyl project create --name "New Project" --description "..."
```

### `entity` - Generic CRUD

```bash
uv run sibyl entity list --type pattern
uv run sibyl entity show <entity_id>
uv run sibyl entity related <entity_id>
```

---

## Behavioral Patterns

### Starting a Session

```bash
# Check for in-progress tasks
uv run sibyl task list --status doing

# Or find todo tasks
uv run sibyl task list --status todo --project <your_project>
```

### Before Any Implementation

```bash
# Search for relevant context
uv run sibyl search "topic you're implementing"
uv run sibyl search "error you encountered" --type error_pattern
```

### During Implementation

- Start the task: `uv run sibyl task start <id>`
- If blocked: `uv run sibyl task block <id> --reason "..."`
- Capture discoveries immediately: `uv run sibyl add "Title" "Learning..."`

### After Completing Work

```bash
# Complete with learnings
uv run sibyl task complete <id> --learnings "Key insight: ..."

# If you found a reusable pattern
uv run sibyl add "Pattern name" "Description..." --type pattern
```

---

## Knowledge Capture Guidelines

### What to Capture

- **Always capture:**
  - Solutions to non-obvious problems
  - Gotchas and edge cases
  - Configuration that took trial-and-error
  - Decisions and their rationale

- **Consider capturing:**
  - Useful code patterns
  - Performance optimizations
  - Integration approaches

- **Don't capture:**
  - Trivial or obvious information
  - Temporary workarounds (unless noting they're temporary)
  - Information already well-documented elsewhere

### Writing Good Episodes

**Bad:** "Fixed the bug" **Good:** "FalkorDB connection drops under concurrent writes. Root cause:
Graphiti's default SEMAPHORE_LIMIT=20 overwhelms single connection. Fix: Set SEMAPHORE_LIMIT=10 in
.env before importing graphiti."

Include:

- What the problem was
- Why it happened (root cause)
- How you fixed it
- Any caveats or related issues

---

## Anti-Patterns to Avoid

1. **Doing work without a task** - Creates untracked, unlinkable work
2. **Implementing before searching** - Risks reinventing/re-breaking things
3. **Completing tasks without capturing learnings** - Wastes knowledge
4. **Vague episode content** - "Fixed it" helps no one
5. **Ignoring search results** - The graph knows things; trust it
6. **Only consuming, never adding** - The graph must grow

---

## Quick Reference

| Situation                    | Command                                                           |
| ---------------------------- | ----------------------------------------------------------------- |
| Starting new work            | `uv run sibyl search "topic"` then `uv run sibyl task start <id>` |
| Need to understand something | `uv run sibyl search "topic"`                                     |
| Found a bug                  | `uv run sibyl search "error message"` first                       |
| Solved something tricky      | `uv run sibyl add "Title" "Details..."`                           |
| Completing a task            | `uv run sibyl task complete <id> --learnings "..."`               |
| Breaking down work           | `uv run sibyl task create --title "..." --project <id>`           |
| Check what's in progress     | `uv run sibyl task list --status doing`                           |

---

## Remember

Sibyl is your persistent memory. Without it, every session starts from zero. With it, you build on
everything that came before.

**Search often. Add generously. Track everything.**
