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

| Argument  | Required | Description                       |
| --------- | -------- | --------------------------------- |
| `task_id` | Yes      | Task ID or unambiguous prefix     |

### Options

| Option   | Short | Description |
| -------- | ----- | ----------- |
| `--json` | `-j`  | JSON output |

Task ID arguments accept an unambiguous prefix, so `sibyl task show task_abc1` resolves as long
as only one task matches.

### Example

```bash
sibyl task show <task_id>
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

| Option                  | Short | Default | Description                                     |
| ----------------------- | ----- | ------- | ----------------------------------------------- |
| `--hours`               | `-h`  | (none)  | Actual hours spent                              |
| `--learnings` / `--note` | `-l` | (none)  | Key learnings (creates an episode)              |
| `--learnings-file`      |       | (none)  | Read learnings from a file                      |
| `--max-size`            |       | 1048576 | Maximum learnings file size in bytes            |
| `--follow-symlinks`     |       | false   | Allow `--learnings-file` to read through symlinks |
| `--json`                | `-j`  | false   | JSON output                                     |

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

### Learnings from a File

For longer write-ups, read learnings from a file instead of an inline string:

```bash
sibyl task complete task_abc123 --hours 6.5 --learnings-file ./task-notes.md
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
| `--status`      | `-s`  | Status: todo, doing, blocked, review, done                    |
| `--priority`    | `-p`  | Priority: critical, high, medium, low, someday                |
| `--complexity`  |       | Complexity: trivial, simple, medium, complex, epic            |
| `--title`       |       | Task title                                                    |
| `--description` | `-d`  | Task description/content                                      |
| `--assignee`    | `-a`  | Assignee                                                      |
| `--epic`        | `-e`  | Epic ID to group under                                        |
| `--feature`     | `-f`  | Feature area                                                  |
| `--tags`        |       | Comma-separated tags (replaces existing)                      |
| `--tech`        |       | Comma-separated technologies (replaces existing)              |
| `--add-dep`     |       | Comma-separated task IDs to add as dependencies               |
| `--remove-dep`  |       | Comma-separated task IDs to remove as dependencies            |
| `--json`        | `-j`  | JSON output                                                   |

To archive a task, use [`task archive`](#task-archive) rather than `task update --status`.

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

Add a note to a task. Content can be passed positionally, piped via `-`, or read from a file.

### Synopsis

```bash
sibyl task note <task_id> [content] [options]
```

### Arguments

| Argument  | Required | Description                       |
| --------- | -------- | --------------------------------- |
| `task_id` | Yes      | Task ID or unambiguous prefix     |
| `content` | No       | Note content or `-` for stdin     |

### Options

| Option                   | Short | Default | Description                                     |
| ------------------------ | ----- | ------- | ----------------------------------------------- |
| `--content-file`         |       | (none)  | Read note content from a file                   |
| `--max-size`             |       | 1048576 | Maximum content file size in bytes              |
| `--follow-symlinks`      |       | false   | Allow `--content-file` to read through symlinks |
| `--assistant` / `--agent` |      | false   | Mark as assistant-authored (default: user)      |
| `--author`               | `-a`  | (none)  | Author name/identifier                          |
| `--json`                 | `-j`  | false   | JSON output                                     |

### Examples

```bash
# Add user note
sibyl task note task_abc123 "Found the root cause - Redis connection timeout"

# Add assistant note
sibyl task note task_abc123 "Implementing the fix now" --assistant --author claude

# Note from stdin
git log -1 --format=%B | sibyl task note task_abc123 -
```

The top-level [`sibyl note`](./remember.md) command wraps this: pass a task ID and it adds a task
note, pass free text and it captures a note memory.

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
- [`sibyl recall`](./recall.md) - Recall a context pack before starting a task
- [`sibyl search`](./search.md) - Find tasks semantically
