# add

Quick knowledge capture. Adds episodes, patterns, and other knowledge to the graph.

## Synopsis

```bash
sibyl add <title> <content> [options]
```

## Arguments

| Argument  | Required | Description                 |
| --------- | -------- | --------------------------- |
| `title`   | Yes      | Title/name of the knowledge |
| `content` | Yes      | Content/description         |

## Options

| Option              | Short | Default   | Description                                                |
| ------------------- | ----- | --------- | ---------------------------------------------------------- |
| `--type`            | `-t`  | `episode` | Entity type to create                                      |
| `--category`        | `-c`  | (none)    | Category for organization                                  |
| `--language`        | `-l`  | (none)    | Programming language                                       |
| `--tags`            |       | (none)    | Comma-separated tags                                       |
| `--wait-searchable` |       | false     | Wait for the episode to become searchable before returning |
| `--json`            | `-j`  | false     | Output as JSON                                             |

## Entity Types

| Type            | Use Case                                      |
| --------------- | --------------------------------------------- |
| `episode`       | General knowledge, learnings, notes (default) |
| `pattern`       | Reusable code patterns, best practices        |
| `error_pattern` | Error patterns and solutions                  |
| `convention`    | Team conventions, coding standards            |
| `rule`          | Rules and constraints                         |
| `template`      | Code templates                                |

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
sibyl add "Graphiti Node Labels" \
  "Graphiti creates two node types: Episodic (from add_episode) and Entity (extracted). Queries must handle both: WHERE (n:Episodic OR n:Entity)" \
  --type pattern --tags "graphiti,falkordb"

# Configuration quirk
sibyl add "FalkorDB Port Conflict" \
  "FalkorDB uses port 6380 by default to avoid Redis conflicts on 6379. Check docker-compose.yml if connections fail." \
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

- [`sibyl search`](./search.md) - Find existing knowledge
- [`sibyl entity create`](./entity.md) - More detailed entity creation
- [`sibyl task complete`](./task-lifecycle.md) - Complete task with learnings
