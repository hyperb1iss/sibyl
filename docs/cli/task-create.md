# task create

Create a new task in a project.

## Synopsis

```bash
sibyl task create --title <title> [options]
```

## Required Options

| Option    | Description           |
| --------- | --------------------- |
| `--title` | Task title (required) |

::: warning Note Unlike some CLIs, the title is passed as `--title`, not as a positional argument.
This is intentional for clarity and to avoid ambiguity with descriptions. :::

## Options

| Option          | Short | Default    | Description                                        |
| --------------- | ----- | ---------- | -------------------------------------------------- |
| `--title`       |       | (required) | Task title                                         |
| `--project`     | `-p`  | (auto)     | Project ID (auto-resolves from linked path)        |
| `--description` | `-d`  | (none)     | Task description                                   |
| `--priority`    |       | `medium`   | Priority: critical, high, medium, low, someday     |
| `--complexity`  |       | `medium`   | Complexity: trivial, simple, medium, complex, epic |
| `--assignee`    | `-a`  | (none)     | Initial assignee                                   |
| `--epic`        | `-e`  | (none)     | Epic ID to group under                             |
| `--feature`     | `-f`  | (none)     | Feature area                                       |
| `--tags`        |       | (none)     | Comma-separated tags                               |
| `--tech`        |       | (none)     | Comma-separated technologies                       |
| `--depends-on`  |       | (none)     | Comma-separated task IDs this task depends on      |
| `--json`        | `-j`  | false      | JSON output                                        |

## Priority Levels

| Priority   | Use Case                             |
| ---------- | ------------------------------------ |
| `critical` | Production issues, blocking bugs     |
| `high`     | Important features, significant bugs |
| `medium`   | Normal priority work (default)       |
| `low`      | Nice to have, minor improvements     |
| `someday`  | Ideas for later, backlog parking     |

## Complexity Levels

| Complexity | Typical Effort                    |
| ---------- | --------------------------------- |
| `trivial`  | < 1 hour, config changes          |
| `simple`   | 1-4 hours, well-understood        |
| `medium`   | 1-2 days, some unknowns (default) |
| `complex`  | 3-5 days, significant unknowns    |
| `epic`     | > 1 week, should be broken down   |

## Examples

### Basic Task

```bash
sibyl task create --title "Fix login button alignment"
```

Output:

```
Task created: task_abc123def456
```

### With Description and Priority

```bash
sibyl task create \
  --title "Implement password reset flow" \
  --description "Add forgot password endpoint, email service integration, and reset form" \
  --priority high
```

### Full Example

```bash
sibyl task create \
  --title "Add rate limiting to API" \
  --description "Implement rate limiting using Redis. Start with 100 req/min per user." \
  --priority high \
  --complexity medium \
  --assignee "nova" \
  --epic epic_security \
  --feature "api" \
  --tags "security,performance" \
  --tech "redis,express"
```

### Specify Project

If not in a linked directory, specify the project:

```bash
sibyl task create \
  --title "Update documentation" \
  --project proj_abc123
```

### JSON Output

```bash
sibyl task create --title "New feature" --json
```

```json
{
  "id": "task_xyz789abc123",
  "name": "New feature",
  "entity_type": "task",
  "metadata": {
    "status": "todo",
    "priority": "medium",
    "complexity": "medium",
    "project_id": "proj_abc123"
  }
}
```

## Project Resolution

The project is determined in this order:

1. `--project` / `-p` option
2. Linked directory (`sibyl project link`)
3. Error if none found

```bash
# Link directory first (once)
sibyl project link proj_abc123

# Now create without --project
sibyl task create --title "Fix bug"  # Uses proj_abc123
```

## Dependencies

Create tasks that depend on other tasks:

```bash
sibyl task create \
  --title "Deploy to production" \
  --depends-on "task_abc123,task_def456"
```

## Integration with Epics

Create tasks under an epic:

```bash
# First, create or find your epic
sibyl epic list --project proj_abc

# Create task under epic
sibyl task create \
  --title "Implement OAuth2" \
  --epic epic_security_improvements
```

## Scripting Example

Create multiple tasks from a file:

```bash
# tasks.txt (one title per line)
# Fix login bug
# Update user model
# Add tests

while read title; do
  sibyl task create --title "$title" --project proj_abc
done < tasks.txt
```

## Common Pitfalls

### Missing --title

```bash
# Wrong - positional arguments not supported
sibyl task create "Fix bug"

# Correct
sibyl task create --title "Fix bug"
```

### No Project Context

```bash
# Error: No project specified and no linked project for current directory
sibyl task create --title "Fix bug"

# Solutions:
sibyl task create --title "Fix bug" --project proj_abc  # Explicit
sibyl project link proj_abc  # Link directory first
```

## Related Commands

- [`sibyl task list`](./task-list.md) - List tasks
- [`sibyl task show`](./task-lifecycle.md) - View task details
- [`sibyl task start`](./task-lifecycle.md) - Start working on task
- [`sibyl epic create`](./epic.md) - Create epics to group tasks
- [`sibyl project link`](./project.md) - Link directory to project
