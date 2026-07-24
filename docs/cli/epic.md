# epic

Epic lifecycle management. Epics are feature groups that organize related tasks within a project.

::: warning Deprecated An epic is just a task with subtasks. Prefer `sibyl task` and group children
with `sibyl task create --epic <id>`. These commands keep working as sugar over the task tree. :::

## Epic States

```
planning -> in_progress -> completed
               |
               v
            blocked -> in_progress

any -> archived
```

## Commands

- `sibyl epic list` - List epics
- `sibyl epic show` - Show epic details
- `sibyl epic create` - Create an epic
- `sibyl epic start` - Start an epic
- `sibyl epic complete` - Complete an epic
- `sibyl epic archive` - Archive an epic
- `sibyl epic update` - Update epic fields
- `sibyl epic tasks` - List tasks in an epic
- `sibyl epic roadmap` - Generate markdown roadmap document

---

## epic list

List epics with optional filters.

### Synopsis

```bash
sibyl epic list [options]
```

### Options

| Option      | Short | Default | Description                                       |
| ----------- | ----- | ------- | ------------------------------------------------- |
| `--project` | `-p`  | (auto)  | Project ID                                        |
| `--status`  | `-s`  | (all)   | Filter: planning, in_progress, blocked, completed |
| `--limit`   | `-n`  | 50      | Max results                                       |
| `--json`    | `-j`  | false   | JSON output                                       |
| `--all`     | `-A`  | false   | Ignore context, list from all projects            |

### Examples

```bash
# List epics in current project
sibyl epic list

# List all in_progress epics
sibyl epic list --status in_progress

# List epics across all projects
sibyl epic list --all
```

Output:

```
Epics
ID              Title                    Status        Priority   Progress
───────────────────────────────────────────────────────────────────────────
epic_abc123...  Authentication System    in_progress   high       5/12
epic_def456...  API v2 Migration         planning      medium     0/8
epic_ghi789...  Performance Optimization in_progress   high       3/7

Showing 3 epic(s)
```

---

## epic show

Show detailed epic information including progress.

### Synopsis

```bash
sibyl epic show <epic_id> [options]
```

### Arguments

| Argument  | Required | Description  |
| --------- | -------- | ------------ |
| `epic_id` | Yes      | Full epic ID |

### Options

| Option   | Short | Description |
| -------- | ----- | ----------- |
| `--json` | `-j`  | JSON output |

### Example

```bash
sibyl epic show <epic_id>
```

Output:

```
Epic epic_abc123de
  Title:     Authentication System
  Status:    in_progress
  Priority:  high
  Progress:  5/12 tasks (41.7%)

  Project:   proj_xyz7...
  Leads:     nova, bliss

  Description:
  Complete authentication overhaul including OAuth2, MFA, and session management.

  Tags: security, auth, oauth
```

---

## epic create

Create a new epic in a project.

### Synopsis

```bash
sibyl epic create --title <title> [options]
```

### Required Options

| Option           | Description           |
| ---------------- | --------------------- |
| `--title` / `-n` | Epic title (required) |

### Options

| Option          | Short | Default  | Description                                    |
| --------------- | ----- | -------- | ---------------------------------------------- |
| `--project`     | `-p`  | (auto)   | Project ID (auto-resolves from linked path)    |
| `--description` | `-d`  | (none)   | Epic description                               |
| `--priority`    |       | `medium` | Priority: critical, high, medium, low, someday |
| `--assignee`    | `-a`  | (none)   | Epic lead/owner                                |
| `--tags`        |       | (none)   | Comma-separated tags                           |
| `--sync`        |       | false    | Wait for creation                              |
| `--json`        | `-j`  | false    | JSON output                                    |

### Examples

```bash
# Basic epic
sibyl epic create --title "Authentication System"

# Full example
sibyl epic create \
  --title "Authentication System" \
  --description "OAuth2, MFA, session management overhaul" \
  --priority high \
  --assignee "nova" \
  --tags "security,auth,oauth"
```

Output:

```
Epic created: epic_abc123def456
Lead: nova
```

---

## epic start

Start working on an epic. Moves status to `in_progress`.

### Synopsis

```bash
sibyl epic start <epic_id> [options]
```

### Options

| Option       | Short | Description |
| ------------ | ----- | ----------- |
| `--assignee` | `-a`  | Epic lead   |
| `--json`     | `-j`  | JSON output |

### Example

```bash
sibyl epic start epic_abc123
```

Output:

```
Epic started: epic_abc123...
```

---

## epic complete

Complete an epic.

### Synopsis

```bash
sibyl epic complete <epic_id> [options]
```

### Options

| Option        | Short | Description                 |
| ------------- | ----- | --------------------------- |
| `--learnings` | `-l`  | Key learnings from the epic |
| `--json`      | `-j`  | JSON output                 |

### Example

```bash
sibyl epic complete epic_abc123 \
  --learnings "OAuth2 integration was smoother than expected. Key insight: use passport.js for strategy abstraction."
```

Output:

```
Epic completed: epic_abc123...
Learnings captured
```

---

## epic archive

Archive an epic (terminal state).

### Synopsis

```bash
sibyl epic archive <epic_id> [options]
```

### Options

| Option     | Short | Description       |
| ---------- | ----- | ----------------- |
| `--reason` | `-r`  | Archive reason    |
| `--yes`    | `-y`  | Skip confirmation |
| `--json`   | `-j`  | JSON output       |

### Example

```bash
sibyl epic archive epic_abc123 --reason "Superseded by epic_xyz" --yes
```

Output:

```
Epic archived: epic_abc123def456...
```

---

## epic update

Update epic fields directly.

### Synopsis

```bash
sibyl epic update <epic_id> [options]
```

### Options

| Option       | Short | Description                                       |
| ------------ | ----- | ------------------------------------------------- |
| `--status`   | `-s`  | Status: planning, in_progress, blocked, completed |
| `--priority` | `-p`  | Priority: critical, high, medium, low, someday    |
| `--title`    |       | Epic title                                        |
| `--assignee` | `-a`  | Epic lead                                         |
| `--tags`     |       | Comma-separated tags                              |
| `--json`     | `-j`  | JSON output                                       |

### Examples

```bash
# Change priority
sibyl epic update epic_abc123 --priority critical

# Update multiple fields
sibyl epic update epic_abc123 \
  --status in_progress \
  --assignee "bliss" \
  --tags "security,urgent"
```

Output:

```
Epic updated: epic_abc123def456...
Fields: status, assignees, tags
```

---

## epic tasks

List tasks belonging to an epic.

### Synopsis

```bash
sibyl epic tasks <epic_id> [options]
```

### Options

| Option     | Short | Default | Description           |
| ---------- | ----- | ------- | --------------------- |
| `--status` | `-s`  | (all)   | Filter by task status |
| `--limit`  | `-n`  | 50      | Max results           |
| `--json`   | `-j`  | false   | JSON output           |

### Examples

```bash
# All tasks in epic
sibyl epic tasks epic_abc123

# Only todo tasks
sibyl epic tasks epic_abc123 --status todo

# JSON for scripting
sibyl epic tasks epic_abc123 --json
```

Output:

```
Tasks
ID              Title                      Status    Priority   Assignees
───────────────────────────────────────────────────────────────────────────
task_abc12...   Setup OAuth2 provider      done      high       nova
task_def45...   Implement MFA              doing     high       bliss
task_ghi78...   Add session management     todo      medium     -
task_jkl01...   Write auth tests           todo      medium     nova

Showing 4 task(s) for epic
```

---

## Common Workflows

### Epic Planning

```bash
# Create epic
sibyl epic create \
  --title "User Dashboard" \
  --description "New user dashboard with analytics and settings" \
  --priority high

# Create tasks under epic
sibyl task create --title "Design dashboard layout" --epic epic_abc123
sibyl task create --title "Implement analytics widgets" --epic epic_abc123
sibyl task create --title "Add user settings panel" --epic epic_abc123
sibyl task create --title "Write dashboard tests" --epic epic_abc123

# Check progress
sibyl epic show epic_abc123
```

### Epic Execution

```bash
# Start the epic
sibyl epic start epic_abc123 --assignee "nova"

# Check task status
sibyl epic tasks epic_abc123 --status todo

# Start first task
sibyl task start task_design --assignee "nova"

# Monitor progress
sibyl epic show epic_abc123
```

### Completing Epics

```bash
# Check remaining tasks
sibyl epic tasks epic_abc123 --status todo,doing,blocked

# If all done, complete epic
sibyl epic complete epic_abc123 \
  --learnings "Dashboard component architecture worked well. Consider extracting widget framework for reuse."
```

### Moving Tasks to Epic

```bash
# Find orphan tasks
sibyl task list --no-epic

# Assign to epic
sibyl task update task_xyz --epic epic_abc123
```

## epic roadmap

Generate a markdown roadmap document from epics and tasks.

### Synopsis

```bash
sibyl epic roadmap [options]
```

### Options

| Option           | Short | Default | Description                                    |
| ---------------- | ----- | ------- | ---------------------------------------------- |
| `--project`      | `-p`  | (auto)  | Project ID                                     |
| `--status`       | `-s`  | (all)   | Filter epics: planning, in_progress, completed |
| `--include-done` |       | false   | Include completed tasks in output              |
| `--output`       | `-o`  | stdout  | Output file path                               |

### Output Includes

- **Overview table**: Epic counts by status
- **Each epic**: Title, priority badge, status, description, progress bar
- **Tasks by status**: To Do, In Progress, Blocked, Review, Done
- **Priority markers**: 🔴 critical, 🟠 high on task names
- **Key Learnings**: Aggregated learnings from completed tasks

### Examples

```bash
# Generate roadmap to stdout
sibyl epic roadmap

# Save to file
sibyl epic roadmap -o roadmap.md

# Include completed tasks
sibyl epic roadmap --include-done

# Only in-progress epics
sibyl epic roadmap --status in_progress

# Specific project
sibyl epic roadmap --project proj_abc123
```

### Example Output

```markdown
# Sibyl Roadmap

_Generated: 2026-01-04 11:08_

## Overview

| Status      | Count  |
| ----------- | ------ |
| Planning    | 11     |
| In Progress | 0      |
| Completed   | 2      |
| **Total**   | **13** |

## Epics

### 📋 Server Setup Wizard

**ID:** `epic_b3145...` | **Priority:** 🔴 critical | **Status:** planning

> Web-based first-run wizard that guides users through setup...

**Progress:** `[████████████████░░░░]` 9/11 (82%)

#### 📝 To Do

- [ ] 🔴Backend: First-user-is-admin logic (`task_daab...`)

**Done:** 8 task(s) _(use --include-done to show)_

---

## Key Learnings

- **Replace inline SVGs** (Server Setup Wizard)
  > Iconoir icons are already exported from @/components/ui/icons.tsx...
```

---

## Related Commands

- [`sibyl task create`](./task-create.md) - Create task (with `--epic`)
- [`sibyl task list`](./task-list.md) - List tasks (with `--epic` filter)
- [`sibyl project show`](./project.md) - Project overview
