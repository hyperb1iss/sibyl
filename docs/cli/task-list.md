# task list

List tasks with optional filters. The workhorse command for task management.

## Synopsis

```bash
sibyl task list [options]
```

## Options

### Filtering

| Option         | Short | Description                                        |
| -------------- | ----- | -------------------------------------------------- |
| `--query`      | `-q`  | Search query (semantic search on name/description) |
| `--status`     | `-s`  | Filter by status (comma-separated)                 |
| `--priority`   |       | Filter by priority (comma-separated)               |
| `--complexity` |       | Filter by complexity (comma-separated)             |
| `--feature`    | `-f`  | Filter by feature area                             |
| `--tags`       |       | Filter by tags (comma-separated, matches ANY)      |
| `--project`    | `-p`  | Project ID                                         |
| `--epic`       | `-e`  | Epic ID                                            |
| `--no-epic`    |       | Tasks without an epic                              |
| `--assignee`   | `-a`  | Filter by assignee                                 |
| `--all`        | `-A`  | Ignore context, list from all projects             |

### Pagination

| Option     | Short | Default | Description                       |
| ---------- | ----- | ------- | --------------------------------- |
| `--limit`  | `-n`  | 50      | Max results (max: 200)            |
| `--offset` |       | 0       | Skip first N results              |
| `--page`   |       | (none)  | Page number (1-based, uses limit) |

### Output

| Option   | Short | Description                                  |
| -------- | ----- | -------------------------------------------- |
| `--json` | `-j`  | JSON output                                  |
| `--csv`  |       | CSV output                                   |
| `--wide` |       | Render a wide task table without title wrapping |

## Status Values

| Status     | Description             |
| ---------- | ----------------------- |
| `backlog`  | Not yet planned         |
| `todo`     | Planned, ready to start |
| `doing`    | In progress             |
| `blocked`  | Blocked by something    |
| `review`   | In review               |
| `done`     | Completed               |
| `archived` | Archived                |

## Priority Values

| Priority   | Description          |
| ---------- | -------------------- |
| `critical` | Production issues    |
| `high`     | Important work       |
| `medium`   | Normal priority      |
| `low`      | Nice to have         |
| `someday`  | Future consideration |

## Complexity Values

| Complexity | Description |
| ---------- | ----------- |
| `trivial`  | < 1 hour    |
| `simple`   | 1-4 hours   |
| `medium`   | 1-2 days    |
| `complex`  | 3-5 days    |
| `epic`     | > 1 week    |

## Examples

### Basic Listing

```bash
# All tasks in current project
sibyl task list

# All tasks across all projects
sibyl task list --all
```

### Filter by Status

```bash
# Single status
sibyl task list --status todo

# Multiple statuses (comma-separated)
sibyl task list --status todo,doing

# Active tasks (not done/archived)
sibyl task list --status todo,doing,blocked,review
```

### Filter by Priority

```bash
# High priority only
sibyl task list --priority high

# Critical and high
sibyl task list --priority critical,high
```

### Filter by Complexity

```bash
# Quick wins
sibyl task list --complexity trivial,simple

# Large tasks that need attention
sibyl task list --complexity complex,epic
```

### Semantic Search

Use `-q` for semantic search on task names and descriptions:

```bash
sibyl task list -q "authentication"
sibyl task list -q "database migration" --status todo
```

### Filter by Tags

Match tasks with ANY of the specified tags:

```bash
sibyl task list --tags "security"
sibyl task list --tags "security,performance"
```

### Filter by Epic

```bash
# Tasks in a specific epic
sibyl task list --epic epic_abc123

# Tasks without any epic (orphans)
sibyl task list --no-epic
```

### Filter by Assignee

```bash
sibyl task list --assignee "nova"
sibyl task list --assignee "bliss" --status doing
```

### Filter by Feature

```bash
sibyl task list --feature "api"
sibyl task list --feature "auth" --priority high
```

### Combined Filters

Filters are combined with AND logic:

```bash
# High priority todo tasks with security tag
sibyl task list --status todo --priority high --tags security

# My blocked tasks
sibyl task list --assignee "nova" --status blocked
```

### Pagination

```bash
# First 20 tasks
sibyl task list --limit 20

# Second page (tasks 21-40)
sibyl task list --limit 20 --page 2

# Or using offset
sibyl task list --limit 20 --offset 20
```

## Output Formats

### Table (Default)

```bash
sibyl task list --status todo
```

```
Tasks
ID          Title                          Status    Priority   Assignees
───────────────────────────────────────────────────────────────────────────
task_abc1.. Fix authentication bug         todo      high       nova
task_def2.. Update user documentation      todo      medium     -
task_ghi3.. Add rate limiting              todo      high       bliss, nova

Showing 3 task(s)
```

### JSON

```bash
sibyl task list --status todo --json
```

```json
[
  {
    "id": "task_abc123",
    "name": "Fix authentication bug",
    "type": "task",
    "metadata": {
      "status": "todo",
      "priority": "high",
      "assignees": ["nova"],
      "project_id": "proj_xyz789"
    }
  },
  ...
]
```

### CSV

```bash
sibyl task list --status todo --csv
```

```csv
id,title,status,priority,project,assignees
task_abc123,Fix authentication bug,todo,high,proj_xyz789,nova
task_def456,Update user documentation,todo,medium,proj_xyz789,
task_ghi789,Add rate limiting,todo,high,proj_xyz789,"bliss,nova"
```

## Common Workflows

### Daily Standup

```bash
# What am I working on?
sibyl task list --assignee "$(whoami)" --status doing

# What's blocked?
sibyl task list --status blocked

# What's ready for me?
sibyl task list --assignee "$(whoami)" --status todo --priority critical,high
```

### Sprint Planning

```bash
# Unassigned todo tasks
sibyl task list --status todo --assignee ""

# High priority backlog
sibyl task list --status backlog --priority critical,high

# Tasks without epics
sibyl task list --no-epic --status todo
```

### Review Tasks

```bash
# Tasks ready for review
sibyl task list --status review

# My tasks in review
sibyl task list --status review --assignee "nova"
```

### Export for Reports

```bash
# Export all done tasks this sprint
sibyl task list --status done --csv > sprint_completed.csv

# Export for analysis
sibyl task list --json | jq 'group_by(.metadata.status) | map({status: .[0].metadata.status, count: length})'
```

## Project Context

By default, `task list` is scoped to the current project context:

```bash
# Uses linked project or active context
sibyl task list

# Explicit project
sibyl task list --project proj_abc123

# All projects
sibyl task list --all
```

## Pagination Details

- Default limit: 50
- Maximum limit: 200
- Use `--page` for convenience or `--offset` for precise control

```
Showing 1-50 of 127+ task(s) (--page 2 for more)
```

## Related Commands

- [`sibyl task show`](./task-lifecycle.md) - View task details
- [`sibyl task create`](./task-create.md) - Create new task
- [`sibyl task start`](./task-lifecycle.md) - Start working on task
- [`sibyl search`](./search.md) - Broader semantic search
