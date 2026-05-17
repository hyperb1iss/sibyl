# explore

Graph traversal and exploration commands. Navigate the knowledge graph to discover relationships and
dependencies.

## Commands

- `sibyl explore related` - Find directly connected entities (1-hop)
- `sibyl explore traverse` - Multi-hop graph traversal
- `sibyl explore dependencies` - Task dependency graph
- `sibyl explore path` - Find shortest path between entities

---

## explore related

Find entities directly connected to a given entity.

### Synopsis

```bash
sibyl explore related <entity_id> [options]
```

### Arguments

| Argument    | Required | Description        |
| ----------- | -------- | ------------------ |
| `entity_id` | Yes      | Starting entity ID |

### Options

| Option    | Short | Default | Description                                    |
| --------- | ----- | ------- | ---------------------------------------------- |
| `--rel`   | `-r`  | (all)   | Relationship types to follow (comma-separated) |
| `--limit` | `-n`  | 20      | Maximum results                                |
| `--json`  | `-j`  | false   | JSON output                                    |

### Examples

```bash
# Find all related entities
sibyl explore related task_abc123

# Filter by relationship type
sibyl explore related task_abc123 --rel "DEPENDS_ON,BLOCKS"

# JSON output
sibyl explore related task_abc123 --json
```

Output (table):

```
Related Entities
ID          Name                      Type      Relationship
────────────────────────────────────────────────────────────
ent_xyz7... Update authentication     task      DEPENDS_ON
ent_def4... Auth service refactor     task      BLOCKS
proj_abc... Backend API               project   BELONGS_TO
```

---

## explore traverse

Multi-hop graph traversal from an entity. Discovers entities up to N hops away.

### Synopsis

```bash
sibyl explore traverse <entity_id> [options]
```

### Arguments

| Argument    | Required | Description        |
| ----------- | -------- | ------------------ |
| `entity_id` | Yes      | Starting entity ID |

### Options

| Option    | Short | Default | Description           |
| --------- | ----- | ------- | --------------------- |
| `--depth` | `-d`  | 2       | Traversal depth (1-3) |
| `--limit` | `-n`  | 50      | Maximum results       |
| `--json`  | `-j`  | false   | JSON output           |

### Examples

```bash
# Default 2-hop traversal
sibyl explore traverse task_abc123

# Shallow traversal (direct connections only)
sibyl explore traverse task_abc123 --depth 1

# Deep traversal
sibyl explore traverse task_abc123 --depth 3 --limit 100
```

Output (tree):

```
Traversal from task_abc1...
  Hop 1 (5 entities)
    task Fix authentication bug
    task Update user model
    project Backend API
    pattern JWT handling
    epic Security improvements
  Hop 2 (12 entities)
    task Add logging
    task Database migration
    ... and 10 more

Total: 17 entities across 2 hop(s)
```

::: warning Depth Limit Maximum depth is 3 to prevent performance issues. For deep graph
exploration, use multiple targeted traversals. :::

---

## explore dependencies

Show task dependency graph with topological ordering. Essential for understanding task execution
order.

### Synopsis

```bash
sibyl explore dependencies [entity_id] [options]
```

### Arguments

| Argument    | Required | Description                             |
| ----------- | -------- | --------------------------------------- |
| `entity_id` | No       | Task or Project ID (or use `--project`) |

### Options

| Option      | Short | Default | Description                     |
| ----------- | ----- | ------- | ------------------------------- |
| `--project` | `-p`  | (none)  | Project ID for all dependencies |
| `--json`    | `-j`  | false   | JSON output                     |

### Examples

```bash
# Dependencies for a specific task
sibyl explore dependencies task_abc123

# All dependencies in a project
sibyl explore dependencies --project proj_xyz789

# JSON output for CI/CD
sibyl explore dependencies --project proj_xyz --json
```

Output (table):

```
Dependency Order (execute top to bottom):

    1. task_abc1 Setup database schema    todo     (blocks: 3)
    2. task_def2 Create user model        todo     (deps: 1, blocks: 2)
    3. task_ghi3 Add authentication       doing    (deps: 1, blocks: 1)
    4. task_jkl4 Write API tests          todo     (deps: 2)
    5. task_mno5 Deploy to staging        todo     (deps: 3)

Total: 5 task(s) in dependency order
```

### Circular Dependencies

The command detects circular dependencies:

```
Warning: Circular dependencies detected!

Dependency Order (execute top to bottom):
...
```

JSON output includes a `has_cycles` flag:

```json
{
  "entities": [...],
  "has_cycles": true
}
```

---

## explore path

Find the shortest path between two entities in the graph.

### Synopsis

```bash
sibyl explore path <from_id> <to_id> [options]
```

### Arguments

| Argument  | Required | Description        |
| --------- | -------- | ------------------ |
| `from_id` | Yes      | Starting entity ID |
| `to_id`   | Yes      | Target entity ID   |

### Options

| Option    | Short | Default | Description         |
| --------- | ----- | ------- | ------------------- |
| `--depth` | `-d`  | 5       | Maximum path length |
| `--json`  | `-j`  | false   | JSON output         |

### Examples

```bash
# Find path between two entities
sibyl explore path task_abc123 task_xyz789

# Limit search depth
sibyl explore path ent_start ent_end --depth 3
```

Output:

```
Path Found (length: 3)

  task_abc1...
      |
  hop 1
      |
  hop 2
      |
  hop 3
      |
  task_xyz7...
```

### No Path Found

```
No path found between task_abc1 and task_xyz7 (max depth: 5)
```

---

## Common Patterns

### Discovering Impact

Find what depends on a task before making changes:

```bash
sibyl explore related task_abc --rel "BLOCKED_BY"
```

### Understanding Context

Traverse from a task to understand its context:

```bash
sibyl explore traverse task_abc --depth 2
```

### Sprint Planning

Get dependency-ordered tasks for a project:

```bash
sibyl explore dependencies --project proj_sprint01 --json | jq '.entities'
```

### Finding Connections

Check if two entities are connected:

```bash
sibyl explore path ent_a ent_b --depth 4
```

## Related Commands

- [`sibyl entity related`](./entity.md) - Entity-specific related lookup
- [`sibyl task list`](./task-list.md) - Filter tasks
- [`sibyl search`](./search.md) - Semantic search
