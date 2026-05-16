---
title: Task Management
description: Task lifecycle and workflow in Sibyl
---

# Task Management

Sibyl provides full task lifecycle management with workflow states, priorities, dependencies, and
learning capture. This guide covers how to use tasks effectively.

## Task Workflow

### Status States

Tasks move through these states:

```
backlog <-> todo <-> doing <-> blocked <-> review <-> done -> archived
                     ^_________________________|
```

| Status     | Description                    |
| ---------- | ------------------------------ |
| `backlog`  | Future work, not yet committed |
| `todo`     | Committed to sprint/milestone  |
| `doing`    | Active development             |
| `blocked`  | Waiting on something           |
| `review`   | In code review                 |
| `done`     | Completed and merged           |
| `archived` | Closed without completion      |

### State Transitions

Most transitions are bi-directional:

```bash
# Start working
sibyl task start task_xyz  # todo -> doing

# Block if stuck
sibyl task block task_xyz --reason "Waiting on API keys"  # doing -> blocked

# Unblock when resolved
sibyl task unblock task_xyz  # blocked -> doing

# Submit for review
sibyl task review task_xyz  # doing -> review

# Complete
sibyl task complete task_xyz --learnings "..."  # review/doing -> done

# Archive without completing
sibyl task archive task_xyz --reason "No longer needed"  # any -> archived
```

## Creating Tasks

### Via CLI

```bash
# Basic task (requires project)
sibyl task create --title "Implement OAuth" --project proj_abc

# With all options
sibyl task create \
  --title "Add rate limiting" \
  --project proj_abc \
  --priority high \
  --description "Add rate limiting to API endpoints"
```

### Via MCP

```python
add(
    title="Implement OAuth",
    content="Add OAuth2 login flow with Google and GitHub providers",
    entity_type="task",
    project="proj_abc",
    priority="high",
    technologies=["python", "fastapi"],
    assignees=["alice"]
)
```

::: warning Project Required Tasks **must** have a project. Use `sibyl project list` to find
available projects. :::

## Priority Levels

| Priority   | When to Use                                           |
| ---------- | ----------------------------------------------------- |
| `critical` | Production bugs, security issues, blocking everything |
| `high`     | Important features, blocking other work               |
| `medium`   | Normal priority (default)                             |
| `low`      | Nice to have, polish                                  |
| `someday`  | Future consideration, parking lot                     |

```bash
# Set priority
sibyl task create --title "..." --priority high

# Update priority
sibyl task update task_xyz --priority critical
```

## Task Complexity

Estimate effort with complexity levels:

| Complexity | Time Estimate                    |
| ---------- | -------------------------------- |
| `trivial`  | < 30 minutes                     |
| `simple`   | 30m - 2 hours                    |
| `medium`   | 2 - 8 hours (1 day)              |
| `complex`  | 1 - 3 days                       |
| `epic`     | > 3 days (should be broken down) |

## Listing Tasks

### Basic Listing

```bash
# All tasks in current project context
sibyl task list

# Filter by status (comma-separated)
sibyl task list --status todo,doing,blocked

# Filter by priority
sibyl task list --priority critical,high

# Filter by tags
sibyl task list --tags bug,urgent
```

### Project and Epic Filtering

```bash
# Tasks in specific project
sibyl task list --project proj_abc

# Tasks in specific epic
sibyl task list --epic epic_xyz

# Tasks without an epic (orphaned)
sibyl task list --no-epic
```

### Semantic Search Within Tasks

```bash
# Search by meaning
sibyl task list -q "authentication"
```

## Task Lifecycle Commands

### Starting Work

```bash
sibyl task start task_xyz

# With assignee
sibyl task start task_xyz --assignee alice
```

This:

- Sets status to `doing`
- Records `started_at` timestamp
- Can generate suggested branch name

### Blocking

```bash
sibyl task block task_xyz --reason "Waiting on API access from vendor"
```

The reason is stored in `blockers_encountered` for future reference.

### Unblocking

```bash
sibyl task unblock task_xyz
```

Returns to `doing` status.

### Review

```bash
sibyl task review task_xyz --pr "https://github.com/org/repo/pull/123"
```

Sets status to `review` and stores PR URL.

### Completing

```bash
sibyl task complete task_xyz --learnings "OAuth redirect URIs must match exactly"

# With hours spent
sibyl task complete task_xyz --hours 4.5 --learnings "..."
```

::: tip Always Include Learnings The `--learnings` flag is where value accumulates. Be specific
about what you learned. :::

### Archiving

```bash
sibyl task archive task_xyz --reason "Superseded by new approach"
```

Use for tasks that won't be completed.

## Direct Updates

Update any field directly:

```bash
# Update status
sibyl task update task_xyz --status done

# Update priority
sibyl task update task_xyz --priority high

# Update description
sibyl task update task_xyz --description "Updated task details..."
```

## Task Notes

Add timestamped notes to tasks:

```bash
# Add a human note
sibyl task note task_xyz "Found the root cause of the bug"

# Add an assistant note
sibyl task note task_xyz "Implemented fix" --assistant --author claude

# List notes
sibyl task notes task_xyz
sibyl task notes task_xyz -n 10  # Limit to 10 notes
```

## Dependencies

Tasks can depend on other tasks:

```bash
# Create task with dependencies (via MCP)
add(
    title="Deploy auth service",
    content="...",
    entity_type="task",
    project="proj_abc",
    depends_on=["task_oauth", "task_jwt"]
)
```

### Viewing Dependencies

```bash
# Get dependency chain
sibyl explore dependencies task_xyz
```

This shows tasks in topological order (dependencies before dependents).

## Project Context

### Linking Directories

Link your working directory to a project:

```bash
# In your project directory
sibyl project link proj_abc

# Now task commands auto-scope
sibyl task list --status todo  # Only shows tasks for linked project
```

### Context Priority

1. `--project` flag (highest)
2. `SIBYL_CONTEXT` environment variable
3. Linked directory context
4. No filter (shows all tasks)

### Bypassing Context

```bash
# See all tasks regardless of context
sibyl task list --all
```

## Auto-Tagging

Tasks are automatically tagged based on content:

```bash
sibyl task create --title "Fix OAuth redirect bug" --project proj_abc
# Auto-tags: bug, backend, security
```

Tags are derived from:

- Title keywords
- Description content
- Technologies specified
- Domain category

## Git Integration

Tasks can track Git integration:

| Field         | Description                   |
| ------------- | ----------------------------- |
| `branch_name` | Associated Git branch         |
| `commit_shas` | Commits implementing the task |
| `pr_url`      | Pull request URL              |

```bash
sibyl task review task_xyz --pr "https://github.com/..."
```

## Learning Capture

The most important part of task completion:

```bash
sibyl task complete task_xyz --learnings "OAuth tokens must be refreshed
before expiry, not after. The 'exp' claim is in seconds, not milliseconds."
```

### What Makes Good Learnings

**Bad:**

```
"Fixed the bug"
```

**Good:**

```
"JWT refresh fails when Redis TTL expires. Root cause: token service doesn't
handle WRONGTYPE error. Fix: Add try/except with token regeneration fallback."
```

### Learning Guidelines

1. **What** - The specific issue or discovery
2. **Why** - Root cause or reasoning
3. **How** - The solution or approach
4. **Caveats** - Edge cases or gotchas

## MCP Task Operations

### Using `manage` Tool

```python
# Start task
manage("start_task", entity_id="task_xyz")

# Complete with learnings
manage("complete_task", entity_id="task_xyz",
       data={"learnings": "Key insight..."})

# Block task
manage("block_task", entity_id="task_xyz",
       data={"reason": "Waiting on API access"})

# Unblock
manage("unblock_task", entity_id="task_xyz")

# Submit for review
manage("submit_review", entity_id="task_xyz",
       data={"pr_url": "https://github.com/..."})

# Update fields
manage("update_task", entity_id="task_xyz",
       data={"priority": "high", "assignees": ["alice"]})
```

### Using `explore` Tool

```python
# List project tasks
explore(mode="list", types=["task"], project="proj_abc", status="todo")

# Task dependencies
explore(mode="dependencies", entity_id="task_xyz")
```

## Concurrency

Task updates are serialized with `entity_lock()` in the API layer. When two writers attempt to
modify the same task simultaneously, the second receives a `409 Conflict` response. Callers should
implement retry with backoff when contention is expected.

## Best Practices

### 1. Work in Task Context

Never do significant work without a task. Tasks provide:

- Traceability
- Progress tracking
- Knowledge linking
- Learning capture

### 2. Keep Tasks Focused

Break large tasks into smaller, completable units. Use epics to group related tasks.

### 3. Capture Learnings Always

The `--learnings` flag is not optional - it's where organizational knowledge accumulates.

### 4. Use Dependencies

Model blocking relationships explicitly for better planning.

### 5. Review Blockers

Blocked tasks should have clear reasons and be actively triaged.

## Next Steps

- [The Memory Loop](./memory-loop.md) - Tasks as the act phase
- [Project Organization](./project-organization.md) - Projects and epics
- [Capturing Knowledge](./capturing-knowledge.md) - Learning capture patterns
- [Claude Code Integration](./claude-code.md) - Agent task workflows
