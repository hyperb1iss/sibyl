# Sibyl CLI Workflows

Detailed workflows for common scenarios using the Sibyl CLI.

---

## Multi-Session Work

Sibyl tracks work across coding sessions. Here's how to maintain continuity.

### Starting a New Session

```bash
# 1. Check what's in progress
uv run sibyl task list --status doing

# 2. If nothing in progress, check todos
uv run sibyl task list --status todo

# 3. Search for context on what you're working on
uv run sibyl search "topic from last session"

# 4. Resume or start a task
uv run sibyl task start task_xyz --assignee you
```

### Ending a Session

```bash
# If work is incomplete, leave task as "doing"
# The task stays in progress for next session

# If blocked, mark it with context for future you
uv run sibyl task block task_xyz --reason "Need to investigate the timeout issue"

# If ready for review
uv run sibyl task review task_xyz --pr "github.com/org/repo/pull/42"

# ALWAYS capture learnings before leaving
uv run sibyl entity create \
  --type episode \
  --name "Session insight: Topic" \
  --content "What I discovered today..."
```

### Resuming Blocked Work

```bash
# List blocked tasks
uv run sibyl task list --status blocked

# See what's blocking
uv run sibyl task show task_xyz

# Unblock when ready
uv run sibyl task unblock task_xyz
```

---

## Feature Development

### Phase 1: Research

```bash
# Find existing patterns
uv run sibyl search "feature area" --type pattern

# Find past implementations
uv run sibyl search "similar work" --type episode

# Look for gotchas
uv run sibyl search "problems with X" --type episode

# Check for rules/constraints
uv run sibyl search "requirements for X" --type rule
```

### Phase 2: Planning

```bash
# List existing projects
uv run sibyl project list

# Create project if needed
uv run sibyl project create \
  --name "Feature Name" \
  --description "What this feature does and its scope"

# Create tasks for the project
# (Use the MCP add tool or create via API for now)
# Tasks should break down the feature into steps
```

### Phase 3: Implementation

```bash
# Start the first task
uv run sibyl task start task_xyz --assignee you

# Work on implementation...

# If blocked
uv run sibyl task block task_xyz --reason "Specific issue"

# When unblocked
uv run sibyl task unblock task_xyz

# When ready for review
uv run sibyl task review task_xyz \
  --pr "github.com/org/repo/pull/123" \
  --commits "abc123,def456"
```

### Phase 4: Completion

```bash
# Complete with learnings
uv run sibyl task complete task_xyz \
  --hours 8.5 \
  --learnings "Key insights from implementing this feature..."

# Move to next task
uv run sibyl task list --project proj_abc --status todo
uv run sibyl task start next_task_id --assignee you
```

---

## Debugging Workflow

### Finding Known Issues

```bash
# Search for similar errors
uv run sibyl search "error message or symptom" --type episode

# Search for patterns in the problem area
uv run sibyl search "component name" --type pattern

# Look for related gotchas
uv run sibyl search "common issues with X" --type episode
```

### After Solving

```bash
# ALWAYS capture the solution
uv run sibyl entity create \
  --type episode \
  --name "Fixed: Descriptive title" \
  --content "Root cause: ...
Solution: ...
Prevention: ..." \
  --category debugging \
  --languages python
```

---

## Knowledge Graph Exploration

### Finding Related Knowledge

```bash
# Start from a known entity
uv run sibyl explore related pattern_xyz

# Go deeper
uv run sibyl explore traverse pattern_xyz --depth 2

# Find connections between entities
uv run sibyl explore path entity_a entity_b
```

### Understanding Dependencies

```bash
# Get task dependency chain
uv run sibyl explore dependencies task_xyz

# See all dependencies in a project
uv run sibyl explore dependencies --project proj_abc
```

### Browsing by Category

```bash
# List all patterns
uv run sibyl entity list --type pattern

# Filter by language
uv run sibyl entity list --type pattern --language rust

# Filter by category
uv run sibyl entity list --type rule --category security
```

---

## Project Overview

### Quick Status Check

```bash
# All projects
uv run sibyl project list

# Tasks in a project
uv run sibyl task list --project proj_abc

# Just the todos
uv run sibyl task list --project proj_abc --status todo

# What's blocked?
uv run sibyl task list --project proj_abc --status blocked
```

### Export for Reports

```bash
# Export tasks as JSON
uv run sibyl task list --project proj_abc --format json > tasks.json

# Export as CSV for spreadsheets
uv run sibyl task list --project proj_abc --format csv > tasks.csv
```

---

## Maintenance

### Health Checks

```bash
# Full health check
uv run sibyl health

# Statistics
uv run sibyl stats

# Config verification
uv run sibyl config
```

### Database Operations

```bash
# Backup (if needed)
uv run sibyl db backup

# Check graph integrity
uv run sibyl db stats
```
