# Sibyl CLI Workflows

Detailed workflows for common scenarios using the Sibyl CLI.

---

## Multi-Session Work

Sibyl tracks work across coding sessions. Here's how to maintain continuity.

### Starting a New Session

```bash
# 1. Check what's in progress
sibyl task list --status doing

# 2. If nothing in progress, check todos
sibyl task list --status todo

# 3. Search for context on what you're working on
sibyl search "topic from last session"

# 4. Resume or start a task
sibyl task start task_xyz --assignee you
```

### Ending a Session

```bash
# If work is incomplete, leave task as "doing"
# The task stays in progress for next session

# If blocked, mark it with context for future you
sibyl task block task_xyz --reason "Need to investigate the timeout issue"

# If ready for review
sibyl task review task_xyz --pr "github.com/org/repo/pull/42"

# ALWAYS capture learnings before leaving
sibyl entity create \
  --type episode \
  --name "Session insight: Topic" \
  --content "What I discovered today..."
```

### Resuming Blocked Work

```bash
# List blocked tasks
sibyl task list --status blocked

# See what's blocking
sibyl task show task_xyz

# Unblock when ready
sibyl task unblock task_xyz
```

---

## Feature Development

### Phase 1: Research

```bash
# Find existing patterns
sibyl search "feature area" --type pattern

# Find past implementations
sibyl search "similar work" --type episode

# Look for gotchas
sibyl search "problems with X" --type episode

# Check for rules/constraints
sibyl search "requirements for X" --type rule
```

### Phase 2: Planning

```bash
# List existing projects
sibyl project list

# Create project if needed
sibyl project create \
  --name "Feature Name" \
  --description "What this feature does and its scope"

# Create tasks for the project
# (Use the MCP add tool or create via API for now)
# Tasks should break down the feature into steps
```

### Phase 3: Implementation

```bash
# Start the first task
sibyl task start task_xyz --assignee you

# Work on implementation...

# If blocked
sibyl task block task_xyz --reason "Specific issue"

# When unblocked
sibyl task unblock task_xyz

# When ready for review
sibyl task review task_xyz \
  --pr "github.com/org/repo/pull/123" \
  --commits "abc123,def456"
```

### Phase 4: Completion

```bash
# Complete with learnings
sibyl task complete task_xyz \
  --hours 8.5 \
  --learnings "Key insights from implementing this feature..."

# Move to next task
sibyl task list --project proj_abc --status todo
sibyl task start next_task_id --assignee you
```

---

## Debugging Workflow

### Finding Known Issues

```bash
# Search for similar errors
sibyl search "error message or symptom" --type episode

# Search for patterns in the problem area
sibyl search "component name" --type pattern

# Look for related gotchas
sibyl search "common issues with X" --type episode
```

### After Solving

```bash
# ALWAYS capture the solution
sibyl entity create \
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
sibyl explore related pattern_xyz

# Go deeper
sibyl explore traverse pattern_xyz --depth 2

# Find connections between entities
sibyl explore path entity_a entity_b
```

### Understanding Dependencies

```bash
# Get task dependency chain
sibyl explore dependencies task_xyz

# See all dependencies in a project
sibyl explore dependencies --project proj_abc
```

### Browsing by Category

```bash
# List all patterns
sibyl entity list --type pattern

# Filter by language
sibyl entity list --type pattern --language rust

# Filter by category
sibyl entity list --type rule --category security
```

---

## Project Overview

### Quick Status Check

```bash
# All projects
sibyl project list

# Tasks in a project
sibyl task list --project proj_abc

# Just the todos
sibyl task list --project proj_abc --status todo

# What's blocked?
sibyl task list --project proj_abc --status blocked
```

### Export for Reports

```bash
# Export tasks as JSON
sibyl task list --project proj_abc --json > tasks.json

# Export as CSV for spreadsheets
sibyl task list --project proj_abc --csv > tasks.csv
```

---

## Maintenance

### Health Checks

```bash
# Full health check
sibyl health

# Statistics
sibyl stats

# Config verification
sibyl config
```

### Database Operations

```bash
# Backup (if needed)
sibyl db backup

# Check graph integrity
sibyl db stats
```
