# project

Project management commands. Projects are the top-level container for tasks and epics.

## Commands

- `sibyl project list` - List all projects
- `sibyl project show` - Show project details
- `sibyl project create` - Create a project
- `sibyl project progress` - Show project progress
- `sibyl project link` - Link directory to project
- `sibyl project relink` - Repair the link for the current directory
- `sibyl project unlink` - Remove directory link
- `sibyl project links` - List all directory links

---

## project list

List all projects.

### Synopsis

```bash
sibyl project list [options]
```

### Options

| Option    | Short | Default | Description |
| --------- | ----- | ------- | ----------- |
| `--limit` | `-n`  | 20      | Max results |
| `--json`  | `-j`  | false   | JSON output |
| `--csv`   |       | false   | CSV output  |

### Example

```bash
sibyl project list
```

Output:

```
Projects
ID          Name                Status    Description
───────────────────────────────────────────────────────────────────
proj_abc1.. Backend API         active    REST API for mobile and web clients
proj_def2.. Mobile App          active    iOS and Android app
proj_ghi3.. Documentation       active    Technical documentation site

Showing 3 project(s)
```

---

## project show

Show project details with task summary.

### Synopsis

```bash
sibyl project show <project_id> [options]
```

### Options

| Option   | Short | Description |
| -------- | ----- | ----------- |
| `--json` | `-j`  | JSON output |

### Example

```bash
sibyl project show proj_abc123
```

Output:

```
Project proj_abc1
  Name:    Backend API
  Status:  active

  Description:
  REST API for mobile and web clients

  Task Summary:
    todo: 12
    doing: 3
    blocked: 1
    review: 2
    done: 45

  Progress: ████████████░░░░░░░░ 71%

  Tech Stack: typescript, express, postgresql
```

### JSON Output

```bash
sibyl project show proj_abc123 --json
```

```json
{
  "id": "proj_abc123",
  "name": "Backend API",
  "entity_type": "project",
  "description": "REST API for mobile and web clients",
  "metadata": {
    "status": "active",
    "tech_stack": ["typescript", "express", "postgresql"]
  },
  "task_summary": {
    "total": 63,
    "by_status": {
      "todo": 12,
      "doing": 3,
      "blocked": 1,
      "review": 2,
      "done": 45
    }
  }
}
```

---

## project create

Create a new project.

### Synopsis

```bash
sibyl project create --name <name> [options]
```

### Required Options

| Option   | Short | Description             |
| -------- | ----- | ----------------------- |
| `--name` | `-n`  | Project name (required) |

### Options

| Option          | Short | Description         |
| --------------- | ----- | ------------------- |
| `--description` | `-d`  | Project description |
| `--repo`        | `-r`  | Repository URL      |
| `--json`        | `-j`  | JSON output         |

### Example

```bash
sibyl project create \
  --name "Backend API" \
  --description "REST API for mobile and web clients" \
  --repo "https://github.com/org/backend-api"
```

Output:

```
Project created: proj_abc123def456
```

---

## project progress

Show project progress with visual breakdown.

### Synopsis

```bash
sibyl project progress <project_id> [options]
```

### Options

| Option   | Short | Description |
| -------- | ----- | ----------- |
| `--json` | `-j`  | JSON output |

### Example

```bash
sibyl project progress proj_abc123
```

Output:

```
Project Progress

  ████████████████████████████░░░░░░░░░░░░ 71.4% (45/63)

Status Breakdown:
  backlog    ██ 2
  todo       ████████████ 12
  doing      ███ 3
  blocked    █ 1
  review     ██ 2
  done       █████████████████████████████████████████████ 45
```

### JSON Output

```bash
sibyl project progress proj_abc123 --json
```

```json
{
  "project_id": "proj_abc123",
  "total_tasks": 63,
  "completed": 45,
  "progress_percent": 71.4,
  "by_status": {
    "backlog": 2,
    "todo": 12,
    "doing": 3,
    "blocked": 1,
    "review": 2,
    "done": 45
  }
}
```

---

## project link

Link a directory to a project for automatic context. This is the key to automatic project-scoped
operations.

### Synopsis

```bash
sibyl project link <project_id> [options]
```

### Options

| Option   | Short | Default | Description            |
| -------- | ----- | ------- | ---------------------- |
| `--path` | `-p`  | cwd     | Directory path to link |

### Example

```bash
cd ~/dev/backend-api
sibyl project link proj_abc123
```

Output:

```
Linked /Users/bliss/dev/backend-api
  -> Backend API (proj_abc123def456...)
Task commands in this directory will now auto-scope to this project
```

### Link Another Directory

```bash
sibyl project link proj_xyz789 --path ~/dev/mobile-app
```

### How It Works

Once linked:

```bash
cd ~/dev/backend-api
sibyl task list              # Only shows tasks for proj_abc123
sibyl search "auth"          # Only searches proj_abc123
sibyl task create --title "Fix bug"  # Creates in proj_abc123
```

The link is stored in `~/.sibyl/config.toml`:

```toml
[paths]
"/Users/bliss/dev/backend-api" = "proj_abc123"
"/Users/bliss/dev/mobile-app" = "proj_xyz789"
```

---

## project relink

Repair the project link for the current directory. Use this when a link points at a stale or
renamed project and you want to retarget it without unlinking first.

### Synopsis

```bash
sibyl project relink [options]
```

### Options

| Option   | Short | Default | Description                                       |
| -------- | ----- | ------- | ------------------------------------------------- |
| `--id`   |       | (none)  | Project ID, UUID, name, or slug to relink to      |
| `--path` | `-p`  | cwd     | Directory path                                    |

### Example

```bash
cd ~/dev/backend-api
sibyl project relink --id backend-api
```

---

## project unlink

Remove project link from a directory.

### Synopsis

```bash
sibyl project unlink [options]
```

### Options

| Option   | Short | Default | Description              |
| -------- | ----- | ------- | ------------------------ |
| `--path` | `-p`  | cwd     | Directory path to unlink |

### Example

```bash
cd ~/dev/backend-api
sibyl project unlink
```

Output:

```
Unlinked /Users/bliss/dev/backend-api
```

### Unlink Specific Path

```bash
sibyl project unlink --path ~/dev/old-project
```

---

## project links

List all directory-to-project links.

### Synopsis

```bash
sibyl project links
```

### Example

```bash
sibyl project links
```

Output:

```
Project Links:

* /Users/bliss/dev/backend-api
    -> proj_abc123def456789
  /Users/bliss/dev/mobile-app
    -> proj_xyz789abc123456

* = current context
```

---

## Common Workflows

### Setup New Project

```bash
# 1. Create project
sibyl project create --name "My New Project" --description "Project description"
# Returns: proj_abc123

# 2. Link directory
cd ~/dev/my-new-project
sibyl project link proj_abc123

# 3. Create initial tasks
sibyl task create --title "Setup repository"
sibyl task create --title "Configure CI/CD"
sibyl task create --title "Write initial documentation"
```

### Multi-Project Workflow

```bash
# Work on backend
cd ~/dev/backend
sibyl task list --status doing
sibyl task complete task_abc --learnings "..."

# Switch to frontend
cd ~/dev/frontend
sibyl task list --status todo
sibyl task start task_xyz
```

### Cross-Project Operations

```bash
# Search across all projects
sibyl search "authentication" --all

# List all tasks (all projects)
sibyl task list --all

# Override context for single command
sibyl task list --context proj_other
```

## Related Commands

- [`sibyl task list`](./task-list.md) - List tasks (respects project context)
- [`sibyl context`](./context.md) - Manage CLI contexts
- [`sibyl epic list`](./epic.md) - List epics in project
