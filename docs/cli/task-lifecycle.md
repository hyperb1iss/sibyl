# Task Lifecycle Commands

Commands for managing task state transitions: show, start, block, unblock, review, complete,
archive, update.

## Task States

```
backlog -> todo -> doing -> review -> done
                     |
                     v
                  blocked -> doing (unblock)

done/any -> archived
```

---

## task show

Show detailed task information.

### Synopsis

```bash
sibyl task show <task_id> [options]
```

### Arguments

| Argument  | Required | Description              |
| --------- | -------- | ------------------------ |
| `task_id` | Yes      | Task ID (full or prefix) |

::: tip Short IDs You can use ID prefixes: `task_abc` instead of `task_abc123def456`. The CLI
resolves to the full ID. :::

### Options

| Option   | Short | Description |
| -------- | ----- | ----------- |
| `--json` | `-j`  | JSON output |

### Example

```bash
sibyl task show task_abc123
```

Output:

```
Task task_abc1
  Title:      Fix authentication bug
  Status:     doing
  Priority:   high

  Project:    proj_xyz7...
  Assignees:  nova, bliss

  Description:
  JWT token refresh fails silently after Redis TTL expires.

  Feature:    authentication
  Branch:     fix/auth-token-refresh
  Tech:       redis, express, jwt
```

---

## task start

Start working on a task. Moves status to `doing`.

### Synopsis

```bash
sibyl task start <task_id> [options]
```

### Options

| Option       | Short | Description           |
| ------------ | ----- | --------------------- |
| `--assignee` | `-a`  | Assign to this person |
| `--json`     | `-j`  | JSON output           |

### Example

```bash
sibyl task start task_abc123
```

Output:

```
Task started: task_abc1...
Branch: fix/auth-token-refresh
```

### With Assignee

```bash
sibyl task start task_abc123 --assignee "nova"
```

### Branch Name Generation

When a task is started, Sibyl automatically generates a branch name based on the task title:

- `Fix authentication bug` -> `fix/authentication-bug`
- `Add user profile page` -> `add/user-profile-page`

The branch name is stored in `metadata.branch_name`.

---

## task block

Mark a task as blocked with a reason.

### Synopsis

```bash
sibyl task block <task_id> --reason <reason> [options]
```

### Required Options

| Option     | Short | Description               |
| ---------- | ----- | ------------------------- |
| `--reason` | `-r`  | Blocker reason (required) |

### Options

| Option   | Short | Description |
| -------- | ----- | ----------- |
| `--json` | `-j`  | JSON output |

### Example

```bash
sibyl task block task_abc123 --reason "Waiting for API spec from backend team"
```

Output:

```
Task blocked: task_abc1...
```

### Common Block Reasons

```bash
sibyl task block task_abc --reason "Waiting for design review"
sibyl task block task_abc --reason "Depends on task_xyz"
sibyl task block task_abc --reason "Need clarification from PM"
sibyl task block task_abc --reason "Infrastructure not ready"
```

---

## task unblock

Resume a blocked task. Moves status back to `doing`.

### Synopsis

```bash
sibyl task unblock <task_id> [options]
```

### Options

| Option   | Short | Description |
| -------- | ----- | ----------- |
| `--json` | `-j`  | JSON output |

### Example

```bash
sibyl task unblock task_abc123
```

Output:

```
Task unblocked: task_abc1...
```

---

## task review

Submit a task for review. Moves status to `review`.

### Synopsis

```bash
sibyl task review <task_id> [options]
```

### Options

| Option      | Short | Description                 |
| ----------- | ----- | --------------------------- |
| `--pr`      |       | Pull request URL            |
| `--commits` | `-c`  | Comma-separated commit SHAs |
| `--json`    | `-j`  | JSON output                 |

### Example

```bash
sibyl task review task_abc123 --pr "https://github.com/org/repo/pull/42"
```

Output:

```
Task submitted for review: task_abc1...
```

### With Commits

```bash
sibyl task review task_abc123 \
  --pr "https://github.com/org/repo/pull/42" \
  --commits "abc123,def456,ghi789"
```

---

## task complete

Complete a task and optionally capture learnings.

### Synopsis

```bash
sibyl task complete <task_id> [options]
```

### Options

| Option        | Short | Description                        |
| ------------- | ----- | ---------------------------------- |
| `--hours`     | `-h`  | Actual hours spent                 |
| `--learnings` | `-l`  | Key learnings (creates an episode) |
| `--json`      | `-j`  | JSON output                        |

### Basic Completion

```bash
sibyl task complete task_abc123
```

Output:

```
Task completed: task_abc1...
```

### With Hours Tracking

```bash
sibyl task complete task_abc123 --hours 4.5
```

### With Learnings

```bash
sibyl task complete task_abc123 \
  --learnings "JWT refresh tokens fail silently when Redis TTL expires. Root cause: token service doesn't handle WRONGTYPE error. Fix: Add try/except with token regeneration fallback."
```

Output:

```
Task completed: task_abc1...
Learning episode created from task
```

::: tip Capture Knowledge Use `--learnings` to capture non-obvious solutions, gotchas, or insights.
This creates a linked episode in the knowledge graph. :::

### Full Example

```bash
sibyl task complete task_abc123 \
  --hours 6.5 \
  --learnings "PostgreSQL connection pooling was the root cause. PgBouncer with transaction mode resolved the issue. Key insight: always check pool_mode when debugging connection timeouts."
```

---

## task archive

Archive task(s). Supports bulk operations via stdin.

### Synopsis

```bash
sibyl task archive <task_id> [options]
sibyl task archive --stdin [options]
```

### Options

| Option     | Short | Description                           |
| ---------- | ----- | ------------------------------------- |
| `--reason` | `-r`  | Archive reason                        |
| `--yes`    | `-y`  | Skip confirmation (required for bulk) |
| `--stdin`  |       | Read task IDs from stdin              |
| `--json`   | `-j`  | JSON output                           |

### Single Task

```bash
sibyl task archive task_abc123 --yes
```

### With Reason

```bash
sibyl task archive task_abc123 --reason "Duplicate of task_xyz" --yes
```

### Bulk Archive

```bash
# Archive all done tasks
sibyl task list --status done --json | jq -r '.[].id' | sibyl task archive --stdin --yes

# Archive old todo tasks
sibyl task list --status todo -q "deprecated" --json | jq -r '.[].id' | sibyl task archive --stdin --yes
```

::: warning Bulk Safety Bulk archive requires `--yes` flag for safety. :::

---

## task update

Update task fields directly.

### Synopsis

```bash
sibyl task update <task_id> [options]
```

### Options

| Option          | Short | Description                                                   |
| --------------- | ----- | ------------------------------------------------------------- |
| `--status`      | `-s`  | Status: backlog, todo, doing, blocked, review, done, archived |
| `--priority`    | `-p`  | Priority: critical, high, medium, low, someday                |
| `--complexity`  |       | Complexity: trivial, simple, medium, complex, epic            |
| `--title`       |       | Task title                                                    |
| `--description` | `-d`  | Task description                                              |
| `--assignee`    | `-a`  | Assignee                                                      |
| `--epic`        | `-e`  | Epic ID                                                       |
| `--feature`     | `-f`  | Feature area                                                  |
| `--tags`        |       | Comma-separated tags (replaces existing)                      |
| `--tech`        |       | Comma-separated technologies (replaces existing)              |
| `--json`        | `-j`  | JSON output                                                   |

### Examples

```bash
# Change priority
sibyl task update task_abc123 --priority critical

# Reassign
sibyl task update task_abc123 --assignee "bliss"

# Update multiple fields
sibyl task update task_abc123 \
  --priority high \
  --complexity complex \
  --tags "security,urgent"

# Move to epic
sibyl task update task_abc123 --epic epic_security

# Update title
sibyl task update task_abc123 --title "Fix JWT token refresh (URGENT)"
```

---

## task note

Add a note to a task.

### Synopsis

```bash
sibyl task note <task_id> <content> [options]
```

### Options

| Option          | Short | Description                                 |
| --------------- | ----- | ------------------------------------------- |
| `--assistant`   |       | Mark as assistant-authored (default: user)  |
| `--agent`       |       | Backward-compatible alias for `--assistant` |
| `--author`      | `-a`  | Author name/identifier                      |
| `--json`        | `-j`  | JSON output                                 |

### Examples

```bash
# Add user note
sibyl task note task_abc123 "Found the root cause - Redis connection timeout"

# Add assistant note
sibyl task note task_abc123 "Implementing the fix now" --assistant --author claude
```

---

## task notes

List notes for a task.

### Synopsis

```bash
sibyl task notes <task_id> [options]
```

### Options

| Option    | Short | Default | Description |
| --------- | ----- | ------- | ----------- |
| `--limit` | `-n`  | 20      | Max results |
| `--json`  | `-j`  | false   | JSON output |

### Example

```bash
sibyl task notes task_abc123
```

Output:

```
user 2024-01-15 10:30:00
  Found the root cause - Redis connection timeout

assistant claude 2024-01-15 10:35:00
  Implementing the fix now. Will add retry logic.

user 2024-01-15 11:00:00
  Fix deployed to staging, testing now.

3 note(s)
```

---

## Workflow Example

A typical task workflow:

```bash
# 1. Pick a task to work on
sibyl task list --status todo --priority high
sibyl task show task_abc123

# 2. Start the task
sibyl task start task_abc123

# 3. Add progress notes
sibyl task note task_abc123 "Investigated the issue, found root cause"

# 4. If blocked
sibyl task block task_abc123 --reason "Need API spec from backend"

# 5. When unblocked
sibyl task unblock task_abc123

# 6. Submit for review
sibyl task review task_abc123 --pr "https://github.com/org/repo/pull/42"

# 7. Complete with learnings
sibyl task complete task_abc123 \
  --hours 4 \
  --learnings "Key insight: Always check Redis connection pool settings"
```

## Related Commands

- [`sibyl task list`](./task-list.md) - List tasks
- [`sibyl task create`](./task-create.md) - Create new task
- [`sibyl search`](./search.md) - Find tasks semantically
