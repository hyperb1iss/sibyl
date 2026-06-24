# search

Semantic search across the knowledge graph. Uses vector similarity to find relevant entities based
on meaning, not just keywords.

## Synopsis

```bash
sibyl search <query> [options]
```

## Arguments

| Argument | Required | Description                     |
| -------- | -------- | ------------------------------- |
| `query`  | Yes      | Search query (natural language) |

## Options

| Option         | Short | Default | Description                           |
| -------------- | ----- | ------- | ------------------------------------- |
| `--type`       | `-t`  | (all)   | Filter by entity type                 |
| `--limit`      | `-l`  | 10      | Maximum results to return             |
| `--all`        | `-a`  | false   | Search all projects (bypass context)  |
| `--graph-only` |       | false   | Search graph memory only              |
| `--docs-only`  |       | false   | Search crawled docs only              |
| `--as-of`      |       | (now)   | Filter graph memory as of a timestamp |
| `--json`       | `-j`  | false   | Output as JSON                        |

Search spans both graph memory and crawled documents by default. Use `--graph-only` or `--docs-only`
to restrict the surface.

## Entity Types

Common types to filter by:

- `task` - Tasks in projects
- `project` - Projects
- `epic` - Feature groups
- `pattern` - Code patterns and best practices
- `episode` - Knowledge episodes
- `error_pattern` - Error patterns and solutions
- `document` - Crawled documents
- `decision`, `plan`, `idea`, `claim` - Memory-loop reasoning artifacts

See [`sibyl entity`](./entity.md) for the full list of 33 entity types.

## Examples

### Basic Search

```bash
sibyl search "authentication flow"
```

Output:

```
Found 5 results:

  JWT Authentication Pattern (pattern) 0.92
  Auth middleware implementation (episode) 0.87
  OAuth2 integration guide (document) 0.85
  Fix auth token refresh (task) 0.81
  Session management (pattern) 0.78
```

### Filter by Type

Search only for tasks:

```bash
sibyl search "database migration" --type task
```

Search for patterns:

```bash
sibyl search "error handling" -t pattern
```

### Increase Results

```bash
sibyl search "React hooks" --limit 20
```

### Search All Projects

By default, search is scoped to the current project context. Use `--all` to search globally:

```bash
sibyl search "deployment" --all
```

### Restrict the Search Surface

```bash
# Graph memory only (skip crawled docs)
sibyl search "auth pattern" --graph-only

# Crawled documentation only
sibyl search "proto config" --docs-only
```

### JSON Output

For automation and scripts:

```bash
sibyl search "testing" --json
```

```json
{
  "results": [
    {
      "id": "ent_abc123",
      "name": "Unit Testing Patterns",
      "type": "pattern",
      "score": 0.94,
      "description": "Best practices for unit testing React components..."
    },
    {
      "id": "task_xyz789",
      "name": "Add integration tests",
      "type": "task",
      "score": 0.87,
      "metadata": {
        "status": "todo",
        "priority": "high"
      }
    }
  ],
  "total": 2
}
```

### Pipe to jq

Extract specific fields:

```bash
sibyl search "API" --json | jq -r '.results[] | "\(.name): \(.score)"'
```

Get the top result's ID:

```bash
sibyl search "auth" --json | jq -r '.results[0].id'
```

## Search Quality Tips

### Be Specific

```bash
# Better
sibyl search "JWT token refresh handling in Express middleware"

# Less effective
sibyl search "tokens"
```

### Use Domain Terms

The search understands technical terminology:

```bash
sibyl search "React useEffect cleanup memory leak"
sibyl search "PostgreSQL connection pooling"
sibyl search "GraphQL N+1 query problem"
```

### Combine with Type Filter

When looking for specific entity types:

```bash
# Find tasks about a topic
sibyl search "performance optimization" --type task

# Find patterns about a topic
sibyl search "caching strategies" --type pattern
```

## Project Context

Search respects project context:

1. **With linked directory**: Searches only that project
2. **With `--context`**: Searches the specified project
3. **With `--all`**: Searches all projects

```bash
# Search in linked project
cd ~/dev/my-project
sibyl search "auth"

# Search in specific project
sibyl search "auth" --context proj_abc123

# Search everywhere
sibyl search "auth" --all
```

## Related Commands

- [`sibyl recall`](./recall.md) - Compile a working context pack for a goal
- [`sibyl add`](./add.md) - Add new knowledge
- [`sibyl explore related`](./explore.md) - Find related entities
- [`sibyl show`](./show.md) - View full graph entity or raw memory content
- [`sibyl crawl documents`](./crawl.md) - Read crawled documents in full
