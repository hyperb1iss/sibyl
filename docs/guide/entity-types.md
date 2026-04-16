---
title: Entity Types
description: All entity types available in Sibyl
---

# Entity Types

Sibyl supports various entity types for different kinds of knowledge. Understanding when to use each
type helps keep your knowledge graph organized and searchable.

## Core Knowledge Types

### Episode

Temporal knowledge snapshots - insights, learnings, and discoveries.

```bash
sibyl add "Redis timeout root cause" "Connection pool exhaustion happens when..."
```

**When to use:**

- Debugging discoveries
- One-time learnings
- Context-specific insights
- Time-bound observations

**Properties:**

| Field | Description |
|-------|-------------|
| `episode_type` | Type: "wisdom", "discovery", "debug" |
| `source_url` | Original source URL |
| `valid_from` | When knowledge became valid |
| `valid_to` | When knowledge was superseded |

### Pattern

Reusable coding patterns and best practices.

```bash
sibyl add "Retry with exponential backoff" "Implementation pattern for resilient..." \
  --type pattern \
  --languages python,typescript
```

**When to use:**

- Reusable code patterns
- Best practices
- Standard solutions to common problems
- Implementation templates

**Properties:**

| Field | Description |
|-------|-------------|
| `category` | Pattern category (e.g., "error-handling") |
| `languages` | Applicable programming languages |
| `confidence` | Confidence score (0-1) |

### Rule

Sacred constraints and invariants that must be followed.

```bash
sibyl entity create --type rule \
  --name "Never commit secrets" \
  --content "API keys and passwords must never be committed to git"
```

**When to use:**

- Security requirements
- Code review checklist items
- Architectural constraints
- Team agreements

**Properties:**

| Field | Description |
|-------|-------------|
| `severity` | Violation severity: error, warning, info |
| `enforcement` | How the rule is enforced |
| `exceptions` | Known valid exceptions |

### Template

Code or configuration templates.

**When to use:**

- Boilerplate code
- Configuration examples
- Project scaffolds
- Standard file structures

**Properties:**

| Field | Description |
|-------|-------------|
| `template_type` | Type: code, config, project |
| `file_extension` | Expected file extension |
| `variables` | Template variables to replace |

### Tool

Development tools and utilities.

**When to use:**

- CLI tool documentation
- Library usage notes
- Service configurations
- Integration guides

**Properties:**

| Field | Description |
|-------|-------------|
| `tool_type` | Type: cli, library, service |
| `installation` | Installation instructions |
| `version` | Recommended version |

### Topic

High-level concepts and knowledge areas.

**When to use:**

- Domain concepts
- Technology areas
- Organizational knowledge
- Taxonomy building

**Properties:**

| Field | Description |
|-------|-------------|
| `parent_topic` | Parent topic for hierarchy |

## Task Management Types

### Task

Work items with full lifecycle management.

```bash
sibyl task create --title "Implement OAuth" --project proj_abc --priority high
```

**When to use:**

- Trackable work items
- Features to implement
- Bugs to fix
- Improvements to make

**Properties:**

| Field | Description |
|-------|-------------|
| `status` | Workflow state (see below) |
| `priority` | critical, high, medium, low, someday |
| `project_id` | Parent project UUID |
| `epic_id` | Parent epic UUID (optional) |
| `assignees` | List of assignees |
| `due_date` | Due date |
| `estimated_hours` | Effort estimate |
| `actual_hours` | Time spent |
| `learnings` | What was learned |
| `branch_name` | Git branch |
| `pr_url` | Pull request URL |

**Task Status Flow:**

```
backlog <-> todo <-> doing <-> blocked <-> review <-> done -> archived
```

### Project

Container for tasks and epics.

```bash
sibyl project create --name "Auth System" --description "Authentication and authorization"
```

**When to use:**

- Major initiatives
- Product features
- Standalone systems
- Team efforts

**Properties:**

| Field | Description |
|-------|-------------|
| `status` | planning, active, on_hold, completed, archived |
| `repository_url` | GitHub repo URL |
| `features` | Major feature areas |
| `tech_stack` | Technologies used |
| `total_tasks` | Task count |
| `completed_tasks` | Completed count |

### Epic

Feature initiative grouping related tasks.

```bash
sibyl epic create --name "OAuth Integration" --project proj_abc
```

**When to use:**

- Multi-task features
- Sprint themes
- Major milestones
- Grouped deliverables

**Properties:**

| Field | Description |
|-------|-------------|
| `status` | planning, in_progress, blocked, completed, archived |
| `priority` | critical, high, medium, low, someday |
| `project_id` | Parent project UUID (required) |
| `total_tasks` | Tasks in epic |
| `completed_tasks` | Completed tasks |

### Note

Timestamped notes on tasks.

```bash
sibyl task note task_xyz "Found the root cause of the bug"
```

**When to use:**

- Progress updates
- Findings during work
- Observations
- Agent/user communication

**Properties:**

| Field | Description |
|-------|-------------|
| `task_id` | Parent task UUID |
| `author_type` | "agent" or "user" |
| `author_name` | Author identifier |

## Documentation Types

### Source

A documentation source to be crawled.

```bash
sibyl crawl add https://react.dev --name "React Docs" --depth 3
```

**When to use:**

- External documentation
- API references
- Library guides
- Internal wikis

**Properties:**

| Field | Description |
|-------|-------------|
| `url` | Source URL |
| `source_type` | website, github, local, api_docs |
| `crawl_status` | pending, in_progress, completed, failed, partial |
| `document_count` | Crawled document count |

### Document

A crawled document/page from a source.

**When to use:**

- Created automatically by crawler
- Represents individual pages
- Contains chunked content

**Properties:**

| Field | Description |
|-------|-------------|
| `url` | Document URL |
| `title` | Page title |
| `headings` | Extracted headings |
| `has_code` | Contains code blocks |
| `language` | Primary language detected |

## Specialized Types

### Error Pattern

Recurring errors and their solutions.

**When to use:**

- Common error messages
- Debugging solutions
- Root cause analysis
- Prevention strategies

**Properties:**

| Field | Description |
|-------|-------------|
| `error_message` | Error message pattern |
| `root_cause` | Why it happens |
| `solution` | How to fix it |
| `prevention` | How to prevent it |
| `occurrence_count` | Times encountered |

### Team

Team information and membership.

**When to use:**

- Team composition
- Responsibility areas
- Contact information

**Properties:**

| Field | Description |
|-------|-------------|
| `members` | Team member IDs |
| `focus_areas` | Areas of responsibility |

### Milestone

Project timeline markers.

**When to use:**

- Release targets
- Sprint boundaries
- Deadline tracking

**Properties:**

| Field | Description |
|-------|-------------|
| `project_id` | Parent project |
| `start_date` | Milestone start |
| `end_date` | Milestone end |
| `target_date` | Target completion |

### Community

Entity clusters from graph analysis.

**When to use:**

- Auto-generated by community detection
- Represents related entity groups
- Used for knowledge organization

**Properties:**

| Field | Description |
|-------|-------------|
| `key_concepts` | Main concepts in community |
| `member_count` | Entities in community |
| `level` | Hierarchy level |

## Type Selection Guide

| Scenario                       | Recommended Type |
| ------------------------------ | ---------------- |
| "I just figured something out" | `episode`        |
| "This is how we always do X"   | `pattern`        |
| "This must never happen"       | `rule`           |
| "I need to implement X"        | `task`           |
| "X is a major feature area"    | `epic`           |
| "X is a big initiative"        | `project`        |
| "Here's useful external docs"  | `source`         |
| "This error keeps happening"   | `error_pattern`  |
| "Template for new services"    | `template`       |

## Creating Entities

### Via CLI

```bash
# Episodes (default type)
sibyl add "Title" "Content"

# Specific types
sibyl add "Title" "Content" --type pattern
sibyl entity create --type rule --name "..." --content "..."
```

### Via MCP Tools

```python
# Using the add tool
add(
    title="Pattern name",
    content="Pattern description",
    entity_type="pattern",
    languages=["python"],
    category="error-handling"
)
```

### Via REST API

```bash
curl -X POST http://localhost:3334/api/entities \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Entity name",
    "content": "Entity content",
    "entity_type": "pattern"
  }'
```

## Next Steps

- [Task Management](./task-management.md) - Task lifecycle details
- [Capturing Knowledge](./capturing-knowledge.md) - Best practices for adding knowledge
- [Semantic Search](./semantic-search.md) - Finding entities
