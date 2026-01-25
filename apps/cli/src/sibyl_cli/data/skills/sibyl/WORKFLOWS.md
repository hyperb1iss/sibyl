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
sibyl task start task_a1b2c3d4e5f6
```

### Ending a Session

```bash
# If work is incomplete, leave task as "doing"
# The task stays in progress for next session

# If blocked, mark it with context for future you
sibyl task block task_a1b2c3d4e5f6 --reason "Need to investigate the timeout issue"

# If ready for review
sibyl task review task_a1b2c3d4e5f6 --pr "github.com/org/repo/pull/42"

# ALWAYS capture learnings before leaving
sibyl add "Session insight: Topic" "What I discovered today..."
```

### Resuming Blocked Work

```bash
# List blocked tasks
sibyl task list --status blocked

# See what's blocking
sibyl task show task_a1b2c3d4e5f6

# Unblock when ready
sibyl task unblock task_a1b2c3d4e5f6
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

# Get full content for any result (use ID from search output)
sibyl entity show "pattern:abc123-uuid-here"
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
sibyl task start task_a1b2c3d4e5f6

# Work on implementation...

# If blocked
sibyl task block task_a1b2c3d4e5f6 --reason "Specific issue"

# When unblocked
sibyl task unblock task_a1b2c3d4e5f6

# When ready for review
sibyl task review task_a1b2c3d4e5f6 \
  --pr "github.com/org/repo/pull/123" \
  --commits "abc123,def456"
```

### Phase 4: Completion

```bash
# Complete with learnings
sibyl task complete task_a1b2c3d4e5f6 \
  --hours 8.5 \
  --learnings "Key insights from implementing this feature..."

# Move to next task
sibyl task list --project proj_a1b2c3d4e5f6 --status todo
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

# Read full details from any match (use ID from search output)
sibyl entity show "episode:abc123-uuid-here"
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
sibyl explore related ptrn_a1b2c3d4e5f6

# Go deeper
sibyl explore traverse ptrn_a1b2c3d4e5f6 --depth 2

# Find connections between entities
sibyl explore path entity_a entity_b
```

### Understanding Dependencies

```bash
# Get task dependency chain
sibyl explore dependencies task_a1b2c3d4e5f6

# See all dependencies in a project
sibyl explore dependencies --project proj_a1b2c3d4e5f6
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
sibyl task list --project proj_a1b2c3d4e5f6

# Just the todos
sibyl task list --project proj_a1b2c3d4e5f6 --status todo

# What's blocked?
sibyl task list --project proj_a1b2c3d4e5f6 --status blocked
```

### Export for Reports

```bash
# Export tasks as JSON
sibyl task list --project proj_a1b2c3d4e5f6 --json > tasks.json

# Export as CSV for spreadsheets
sibyl task list --project proj_a1b2c3d4e5f6 --csv > tasks.csv
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

---

## Project Management

### Task Audit

Verify tasks against code reality and clean up stale work.

```bash
# 1. Get the landscape - all open work
sibyl task list --status todo,doing,blocked,review

# 2. For each task, verify against codebase
grep -r "function_name" apps/api/
grep -r "@router.post" apps/api/routes/

# 3. Archive completed tasks with evidence
sibyl task archive task_a1b2c3d4e5f6 --reason "Completed: implemented at apps/api/routes/auth.py:42"
sibyl task archive task_d4e5f6a1b2c3 --reason "Irrelevant: superseded by new design"
```

**Classify and Act:**

| Finding                        | Action                           |
| ------------------------------ | -------------------------------- |
| Implementation exists, working | Archive with evidence            |
| Partially done                 | Update description, keep open    |
| No longer relevant             | Archive as irrelevant            |
| Still needed                   | Keep, verify priority is correct |

### Sprint Planning

```bash
# 1. Review high priority items
sibyl task list --status todo --priority critical,high

# 2. Check for blockers
sibyl task list --status blocked

# 3. Scope the sprint
# 6-day cycle = ~4-6 meaningful tasks
# Mix of high impact + quick wins
# Dependencies resolved first
```

### Priority Rebalancing

```bash
# High priority should be small set
sibyl task list --status todo --priority critical,high

# Bulk of work should be here
sibyl task list --status todo --priority medium,low

# Update misclassified tasks
sibyl task update task_a1b2c3d4e5f6 --priority medium
```

**Priority Decision Matrix:**

| Impact | Urgency | Priority |
| ------ | ------- | -------- |
| High   | High    | critical |
| High   | Low     | high     |
| Low    | High    | medium   |
| Low    | Low     | low      |

### Weekly Housekeeping

```bash
# Stale work check (doing/blocked should be small)
sibyl task list --status doing,blocked

# Clean up done tasks (archive with learnings)
sibyl task list --status done

# Priority sanity (critical should be rare)
sibyl task list --status todo --priority critical
```

### Verification Patterns

| Task Area | How to Verify                          |
| --------- | -------------------------------------- |
| Backend   | `grep -r "def func" apps/api/`         |
| Frontend  | `ls apps/web/src/components/`          |
| CLI       | `grep -r "@app.command" apps/cli/src/` |
| MCP Tools | `ls apps/api/src/sibyl/tools/`         |
