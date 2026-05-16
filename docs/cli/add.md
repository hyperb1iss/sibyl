# add

Add knowledge to the graph. `add` creates episodes, patterns, and any other entity type with
explicit title and content fields.

For a single-blob quick capture, see [`capture`](./capture.md). For typed memory-loop writes
(decisions, plans, claims), see [`remember`](./remember.md).

## Synopsis

```bash
sibyl add [title] [content] [options]
```

Title and content can be passed positionally or with the `--title` / `--content` flags. Content
can also be read from a file with `--content-file`.

## Arguments

| Argument  | Required | Description                 |
| --------- | -------- | --------------------------- |
| `title`   | No       | Title/name of the knowledge |
| `content` | No       | Content/description         |

## Options

| Option              | Short | Default   | Description                                              |
| ------------------- | ----- | --------- | -------------------------------------------------------- |
| `--title`           |       | (none)    | Title (alternative to the positional argument)           |
| `--content`         |       | (none)    | Content (alternative to the positional argument)         |
| `--content-file`    |       | (none)    | Read content from a file                                 |
| `--max-size`        |       | 1048576   | Maximum content file size in bytes                       |
| `--follow-symlinks` |       | false     | Allow `--content-file` to read through symlinks          |
| `--type`            | `-t`  | `episode` | Entity type to create (see below)                        |
| `--category`        | `-c`  | (none)    | Category for organization                                |
| `--language`        | `-l`  | (none)    | Programming language                                     |
| `--tags`            |       | (none)    | Comma-separated tags                                     |
| `--project`         | `-p`  | (auto)    | Project ID                                               |
| `--all-projects`    |       | false     | Do not auto-scope to the linked project                  |
| `--related-to`      |       | (none)    | Comma-separated entity IDs to connect with `RELATED_TO`  |
| `--task`            |       | (none)    | Comma-separated task IDs to connect with `RELATED_TO`    |
| `--active-task`     |       | on        | Auto-link to the single active task (`--no-active-task`) |
| `--wait-searchable` |       | false     | Wait until the entity is persisted and retrievable       |
| `--skip-conflicts`  |       | false     | Skip semantic duplicate/conflict detection               |
| `--json`            | `-j`  | false     | Output as JSON                                           |

## Entity Types

`--type` accepts any of around 29 entity types. Common ones:

| Type            | Use Case                                      |
| --------------- | --------------------------------------------- |
| `episode`       | General knowledge, learnings, notes (default) |
| `pattern`       | Reusable code patterns, best practices        |
| `error_pattern` | Error patterns and solutions                  |
| `guide`         | Team guidance, coding standards               |
| `rule`          | Rules and constraints                         |
| `template`      | Code templates                                |
| `decision`      | A choice made, with rationale                 |
| `plan`          | An intended sequence of work                  |
| `procedure`     | A repeatable process                          |

See [`sibyl entity`](./entity.md) for the complete list.

## Examples

### Add an Episode (Default)

```bash
sibyl add "JWT Refresh Bug Fix" "Token refresh was failing silently when Redis TTL expired. Root cause: token service doesn't handle WRONGTYPE error. Fix: Add try/except with token regeneration fallback."
```

Output:

```
Queued episode: JWT Refresh Bug Fix
  ID: ent_abc123def456
```

### Add a Pattern

```bash
sibyl add "React Error Boundary Pattern" \
  "Wrap components with ErrorBoundary to catch rendering errors. Include fallback UI and error reporting. Reset error state on navigation." \
  --type pattern
```

### With Category and Language

```bash
sibyl add "PostgreSQL Connection Pooling" \
  "Use PgBouncer for connection pooling in production. Set pool_mode to transaction for web apps. Monitor active connections with pg_stat_activity." \
  --type pattern \
  --category database \
  --language sql
```

### With Tags

```bash
sibyl add "Kubernetes Health Check Pattern" \
  "Implement both liveness and readiness probes. Liveness checks if the process is alive, readiness checks if it can accept traffic." \
  --type pattern \
  --tags "kubernetes,devops,health-checks"
```

### Content from a File

```bash
sibyl add "Migration runbook" --content-file ./runbook.md --type procedure
```

### Link to Tasks and Entities

```bash
sibyl add "Why we dropped the Postgres sidecar" \
  "Surreal now holds graph, content, and auth in one store." \
  --type decision \
  --task task_abc123 \
  --related-to ent_def456,ent_ghi789
```

### JSON Output

```bash
sibyl add "Quick Note" "Remember to update the docs" --json
```

```json
{
  "id": "ent_xyz789abc123",
  "name": "Quick Note",
  "entity_type": "episode",
  "created_at": "2024-01-15T10:30:00Z"
}
```

## What to Capture

### Good Candidates

- **Non-obvious solutions**: Fixes that took time to figure out
- **Gotchas**: Things that surprised you or caused bugs
- **Configuration quirks**: Settings that aren't well documented
- **Architecture decisions**: Why you chose a particular approach
- **Performance findings**: What worked or didn't work
- **Integration approaches**: How to connect different systems

### Examples of Good Knowledge

```bash
# Gotcha
sibyl add "Surreal driver per-org cloning" \
  "The SurrealDB driver serializes queries through a per-client asyncio.Lock. Clone the driver per org with driver.clone(group_id) instead of sharing one instance." \
  --type pattern --tags "surreal,concurrency"

# Configuration quirk
sibyl add "Surreal embedded mode storage" \
  "Embedded SurrealDB uses RocksDB at .moon/cache/surreal-dev and is single-writer. Memory mode (memory://) is test-only." \
  --type episode --category config

# Performance finding
sibyl add "React Query Stale Time" \
  "Set staleTime to at least 5 minutes for dashboard data. Default 0 causes unnecessary refetches on every focus." \
  --type pattern --language typescript --tags "react-query,performance"
```

### Skip

- Trivial information
- Well-documented basics
- Temporary hacks that will be removed
- Code snippets without context

## Integration with Tasks

When completing a task with learnings, the learnings are automatically captured:

```bash
sibyl task complete task_abc --learnings "Discovered that..."
```

This creates an episode linked to the task.

## Related Commands

- [`sibyl capture`](./capture.md) - Quick capture with an auto-derived title
- [`sibyl remember`](./remember.md) - Typed memory-loop writes (decisions, plans, claims)
- [`sibyl search`](./search.md) - Find existing knowledge
- [`sibyl entity create`](./entity.md) - More detailed entity creation
- [`sibyl task complete`](./task-lifecycle.md) - Complete task with learnings
