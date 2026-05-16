---
title: Project Organization
description: Projects, epics, and directory linking
---

# Project Organization

Sibyl organizes work hierarchically: **Projects** contain **Epics**, which contain **Tasks**. This
guide explains how to structure and navigate your work.

## Hierarchy Overview

```
Organization
└── Project
    ├── Epic
    │   ├── Task
    │   ├── Task
    │   └── Task
    ├── Epic
    │   └── Task
    └── Task (no epic - orphaned)
```

## Projects

### What is a Project?

A project is a container for related work - a major initiative, product feature, or system.

### Creating Projects

```bash
sibyl project create --name "Auth System" --description "Authentication and authorization"

# With more details
sibyl project create \
  --name "E-commerce API" \
  --description "Backend services for e-commerce platform" \
  --repo "https://github.com/org/ecommerce-api"
```

### Project Status

| Status      | Description        |
| ----------- | ------------------ |
| `planning`  | Not started yet    |
| `active`    | Active development |
| `on_hold`   | Paused             |
| `completed` | Finished           |
| `archived`  | Historical record  |

### Listing Projects

```bash
# List all projects
sibyl project list

# JSON for scripting
sibyl project list --json
```

### Project Details

```bash
sibyl project show proj_abc123

# Shows:
# - Project name and description
# - Status
# - Task counts (total, completed, in progress)
# - Tech stack
# - Repository URL
```

## Epics

### What is an Epic?

An epic groups related tasks within a project - a feature initiative or major deliverable.

### Creating Epics

```bash
sibyl epic create --title "OAuth Integration" --project proj_abc

# With details
sibyl epic create \
  --title "OAuth Integration" \
  --description "Add OAuth2 login with Google and GitHub" \
  --project proj_abc \
  --priority high
```

### Epic Status

| Status        | Description          |
| ------------- | -------------------- |
| `planning`    | Scoping, not started |
| `in_progress` | Active development   |
| `blocked`     | Waiting on something |
| `completed`   | All work done        |
| `archived`    | Historical record    |

### Listing Epics

```bash
# List epics in a project
sibyl epic list --project proj_abc

# Filter by status
sibyl epic list --project proj_abc --status in_progress
```

## Directory Linking

### Why Link Directories?

Link your working directory to a project so task commands auto-scope:

```bash
# Without linking
sibyl task list --project proj_abc  # Must specify every time

# With linking
cd ~/projects/auth-system
sibyl project link proj_abc

# Now commands auto-scope
sibyl task list  # Shows only auth-system tasks
```

### Linking a Directory

```bash
# Interactive picker
sibyl project link

# Direct link
sibyl project link proj_abc123
```

### Checking Current Context

```bash
# Show every directory-to-project link
sibyl project links

# Lists each linked directory and its project:
# /home/user/auth-system    -> proj_abc123
# /home/user/api            -> proj_xyz789
```

### Unlinking

```bash
sibyl project unlink
```

To repair a stale link after a directory moves, use `sibyl project relink`.

### Context Priority

When determining project context:

1. `--project` flag (highest priority)
2. `SIBYL_CONTEXT` environment variable
3. Linked directory
4. No context (shows all)

### Bypassing Context

```bash
# See all tasks regardless of context
sibyl task list --all

# Override context for one command
sibyl --context proj_xyz task list
```

## Task Organization

### Tasks Within Projects

Every task belongs to a project:

```bash
sibyl task create --title "Add login page" --project proj_abc
```

### Tasks Within Epics

Tasks can optionally belong to an epic:

```bash
# Create task in epic
sibyl task create --title "OAuth callback handler" --project proj_abc --epic epic_oauth

# Or via MCP
add(
    title="OAuth callback handler",
    content="...",
    entity_type="task",
    project="proj_abc",
    epic="epic_oauth"
)
```

### Finding Orphaned Tasks

Tasks without an epic might be unplanned work:

```bash
# Find tasks without epics
sibyl task list --no-epic
```

## Workflow Patterns

### Project-First Pattern

Always start with project context:

```bash
# 1. List projects
sibyl project list

# 2. Link to relevant project
sibyl project link proj_abc

# 3. Now work within project context
sibyl task list --status todo
sibyl task start task_xyz
```

### Epic-Based Planning

Use epics for sprint planning:

```bash
# 1. Create epic for feature
sibyl epic create --title "Q1 Auth Improvements" --project proj_abc

# 2. Add tasks to epic
sibyl task create --title "..." --project proj_abc --epic epic_q1auth

# 3. Track epic progress
sibyl epic show epic_q1auth
```

### Feature Area Pattern

Use the `feature` field for lightweight grouping without epics:

```bash
# Create tasks with feature area
sibyl task create --title "..." --project proj_abc --feature backend
sibyl task create --title "..." --project proj_abc --feature frontend

# Filter by feature
sibyl task list --feature backend
```

## MCP Operations

### Explore Projects

```python
# List all projects
explore(mode="list", types=["project"])

# Get project details
explore(mode="related", entity_id="proj_abc")
```

### Create with Hierarchy

```python
# Create project
add(
    title="Auth System",
    content="Authentication and authorization",
    entity_type="project",
    repository_url="https://github.com/..."
)

# Create epic in project
add(
    title="OAuth Integration",
    content="Add OAuth2 providers",
    entity_type="epic",
    project="proj_abc"
)

# Create task in epic
add(
    title="OAuth callback handler",
    content="Handle OAuth callbacks",
    entity_type="task",
    project="proj_abc",
    epic="epic_oauth"
)
```

## Progress Tracking

### Project Progress

```bash
sibyl project show proj_abc

# Shows:
# - total_tasks: 25
# - completed_tasks: 15
# - in_progress_tasks: 5
```

### Epic Progress

```bash
sibyl epic show epic_oauth

# Shows:
# - total_tasks: 8
# - completed_tasks: 3
# - completion_pct: 37.5%
```

## Graph Relationships

The hierarchy is stored as graph relationships:

```
Task --[BELONGS_TO]--> Epic --[BELONGS_TO]--> Project
```

Query related entities:

```bash
# Find all tasks in an epic
sibyl explore related epic_oauth --relationship-type BELONGS_TO

# Or use filter
sibyl task list --epic epic_oauth
```

## Best Practices

### 1. One Project Per Repository

Match projects to repositories for clear ownership:

```bash
sibyl project create --name "Auth API" --repo "https://github.com/org/auth-api"
```

### 2. Meaningful Epic Names

Name epics after deliverables, not sprints:

```bash
# GOOD
sibyl epic create --title "OAuth Integration"
sibyl epic create --title "Admin Dashboard"

# LESS GOOD
sibyl epic create --title "Sprint 5"
```

### 3. Link Your Directories

Set up linking for each project you work on:

```bash
# Auth API project
cd ~/projects/auth-api
sibyl project link proj_auth

# E-commerce project
cd ~/projects/ecommerce
sibyl project link proj_ecom
```

### 4. Review Orphaned Tasks

Periodically check for tasks without epics:

```bash
sibyl task list --no-epic
```

### 5. Archive Completed Work

Keep the active task list clean:

```bash
# Archive individual tasks
sibyl task archive task_old --reason "Completed: deployed to production"

# Archive a finished epic
sibyl epic archive epic_old
```

Project status is managed through the web UI or the REST API.

## Configuration File

Project links are stored in `~/.sibyl/config.toml`:

```toml
[server]
url = "http://localhost:3334/api"

[paths]
"/home/user/auth-api" = "proj_abc123"
"/home/user/ecommerce" = "proj_xyz789"

[context]
active = "proj_abc123"
```

## Next Steps

- [Task Management](./task-management.md) - Task lifecycle details
- [Capturing Knowledge](./capturing-knowledge.md) - Knowledge within projects
- [Claude Code Integration](./claude-code.md) - AI agent workflows
