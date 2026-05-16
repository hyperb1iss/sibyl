# entity

Generic entity CRUD operations. Use this for working with knowledge entities like patterns,
episodes, templates, and other types.

## Commands

- `sibyl entity list` - List entities by type
- `sibyl entity show` - Show entity details
- `sibyl entity create` - Create an entity
- `sibyl entity delete` - Delete an entity
- `sibyl entity related` - Show related entities
- `sibyl entity history` - Show entity version history

---

## Entity Types

Sibyl tracks around 29 entity types. The full set:

| Type               | Description                            |
| ------------------ | -------------------------------------- |
| `pattern`          | Code patterns, best practices          |
| `rule`             | Rules and constraints                  |
| `template`         | Code templates                         |
| `guide`            | Team guidance, standards               |
| `tool`             | Tools and utilities                    |
| `language`         | Programming languages                  |
| `topic`            | General topics                         |
| `episode`          | Knowledge episodes, learnings          |
| `knowledge_source` | External knowledge sources             |
| `config_file`      | Configuration files                    |
| `slash_command`    | Slash commands                         |
| `project`          | Projects (use `sibyl project` instead) |
| `epic`             | Epics (use `sibyl epic` instead)       |
| `task`             | Tasks (use `sibyl task` instead)       |
| `team`             | Team definitions                       |
| `error_pattern`    | Error patterns and solutions           |
| `milestone`        | Project milestones                     |
| `source`           | Web sources                            |
| `document`         | Crawled documents                      |
| `procedure`        | Procedures and processes               |
| `community`        | Community groupings                    |
| `note`             | Standalone notes                       |
| `domain`           | Knowledge domains                      |
| `artifact`         | Produced outputs (synthesis, docs)     |
| `decision`         | A choice made, with rationale          |
| `plan`             | An intended sequence of work           |
| `idea`             | An exploration or proposal             |
| `claim`            | An assertion to be verified or cited   |
| `session`          | Session-level memory or summary        |

---

## entity list

List entities by type with optional filters.

### Synopsis

```bash
sibyl entity list [options]
```

### Options

| Option       | Short | Default   | Description         |
| ------------ | ----- | --------- | ------------------- |
| `--type`     | `-T`  | `pattern` | Entity type to list |
| `--language` | `-l`  | (all)     | Filter by language  |
| `--category` | `-c`  | (all)     | Filter by category  |
| `--limit`    | `-n`  | 50        | Max results         |
| `--json`     | `-j`  | false     | JSON output         |
| `--csv`      |       | false     | CSV output          |

### Examples

```bash
# List patterns (default)
sibyl entity list

# List episodes
sibyl entity list --type episode

# List patterns for TypeScript
sibyl entity list --type pattern --language typescript

# List patterns in a category
sibyl entity list --type pattern --category "error-handling"

# JSON output
sibyl entity list --type pattern --json
```

Output (table):

```
Patterns
ID          Name                            Description
───────────────────────────────────────────────────────────────────
ent_abc1... JWT Authentication Pattern      Secure JWT token handling with...
ent_def2... Error Boundary Pattern          React error boundary for graceful...
ent_ghi3... Repository Pattern              Data access layer abstraction...

Showing 3 pattern(s)
```

---

## entity show

Show detailed entity information.

### Synopsis

```bash
sibyl entity show <entity_id> [options]
```

### Arguments

| Argument    | Required | Description |
| ----------- | -------- | ----------- |
| `entity_id` | Yes      | Entity ID   |

### Options

| Option   | Short | Description |
| -------- | ----- | ----------- |
| `--json` | `-j`  | JSON output |

### Example

```bash
sibyl entity show ent_abc123
```

Output:

```
Pattern Details
  Name: JWT Authentication Pattern
  Type: pattern
  ID:   ent_abc123def456789

  Description:
  Secure JWT token handling with refresh token rotation

  Content:
  Use short-lived access tokens (15 min) with longer refresh tokens (7 days).
  Store refresh tokens in httpOnly cookies. Implement token rotation on refresh.
  Blacklist tokens on logout using Redis with TTL matching token expiry.

  Metadata:
    category: authentication
    languages: ["typescript", "javascript"]
    tags: ["jwt", "security", "auth"]
```

---

## entity create

Create a new entity.

### Synopsis

```bash
sibyl entity create --type <type> --name <name> [options]
```

### Required Options

| Option   | Short | Description            |
| -------- | ----- | ---------------------- |
| `--type` | `-T`  | Entity type (required) |
| `--name` | `-n`  | Entity name (required) |

### Options

| Option        | Short | Description               |
| ------------- | ----- | ------------------------- |
| `--content`   | `-c`  | Entity content            |
| `--category`  |       | Category                  |
| `--languages` | `-l`  | Comma-separated languages |
| `--tags`      |       | Comma-separated tags      |
| `--json`      | `-j`  | JSON output               |

### Examples

```bash
# Create a pattern
sibyl entity create \
  --type pattern \
  --name "Repository Pattern" \
  --content "Abstract data access behind interfaces. Each aggregate root gets its own repository." \
  --category "architecture" \
  --languages "typescript,java" \
  --tags "ddd,clean-architecture"

# Create an episode
sibyl entity create \
  --type episode \
  --name "Redis Connection Pooling Fix" \
  --content "Connection timeout was caused by exhausted pool. Increased max connections to 20." \
  --category "debugging"

# Create an error pattern
sibyl entity create \
  --type error_pattern \
  --name "ECONNREFUSED on localhost" \
  --content "Service not running or wrong port. Check docker-compose and port mappings." \
  --tags "docker,networking,debugging"
```

Output:

```
Entity created: ent_xyz789abc123
```

---

## entity delete

Delete an entity.

### Synopsis

```bash
sibyl entity delete <entity_id> [options]
```

### Arguments

| Argument    | Required | Description         |
| ----------- | -------- | ------------------- |
| `entity_id` | Yes      | Entity ID to delete |

### Options

| Option   | Short | Description       |
| -------- | ----- | ----------------- |
| `--yes`  | `-y`  | Skip confirmation |
| `--json` | `-j`  | JSON output       |

### Example

```bash
sibyl entity delete ent_abc123 --yes
```

Output:

```
Entity deleted: ent_abc1...
```

---

## entity related

Show entities related to a given entity (1-hop connections).

### Synopsis

```bash
sibyl entity related <entity_id> [options]
```

### Arguments

| Argument    | Required | Description |
| ----------- | -------- | ----------- |
| `entity_id` | Yes      | Entity ID   |

### Options

| Option    | Short | Default | Description |
| --------- | ----- | ------- | ----------- |
| `--limit` | `-n`  | 20      | Max results |
| `--json`  | `-j`  | false   | JSON output |

### Example

```bash
sibyl entity related ent_abc123
```

Output:

```
Related Entities
ID          Name                      Type         Relationship
────────────────────────────────────────────────────────────────────
ent_def4... OAuth2 Integration        pattern      RELATES_TO
ent_ghi5... Session Management        pattern      RELATES_TO
task_jkl... Implement auth flow       task         USED_IN
proj_mno... Backend API               project      BELONGS_TO

Found 4 related entity(ies)
```

---

## entity history

Show version history for an entity, including changes over time.

### Synopsis

```bash
sibyl entity history <entity_id> [options]
```

### Arguments

| Argument    | Required | Description |
| ----------- | -------- | ----------- |
| `entity_id` | Yes      | Entity ID   |

### Options

| Option              | Short | Default   | Description                                         |
| ------------------- | ----- | --------- | --------------------------------------------------- |
| `--as-of`           | `-d`  | (now)     | Point-in-time (ISO date, e.g. `2025-03-15`)         |
| `--mode`            | `-m`  | `history` | Display mode: `history`, `timeline`, or `conflicts` |
| `--include-expired` | `-e`  | false     | Include expired edges                               |
| `--limit`           | `-n`  | 50        | Max results                                         |
| `--json`            | `-j`  | false     | JSON output                                         |

The history modes operate on edges over time:

- `history` shows edges as they existed at a point in time (use `--as-of`).
- `timeline` shows every version of edges over time, surfacing evolution.
- `conflicts` finds invalidated or superseded facts.

### Examples

```bash
# Show full version history
sibyl entity history ent_abc123

# Show state at a specific point in time
sibyl entity history ent_abc123 --as-of "2024-06-01T00:00:00Z"

# Show timeline view
sibyl entity history ent_abc123 --mode timeline

# Show conflicting versions
sibyl entity history ent_abc123 --mode conflicts

# Include expired versions
sibyl entity history ent_abc123 --include-expired
```

---

## Common Workflows

### Knowledge Management

```bash
# Search for existing patterns
sibyl search "error handling" --type pattern

# If not found, create one
sibyl entity create \
  --type pattern \
  --name "Global Error Handler Pattern" \
  --content "Centralize error handling with middleware. Log errors, sanitize for clients."

# Find related knowledge
sibyl entity related ent_abc123
```

### Documenting Solutions

```bash
# After solving a bug, document it
sibyl entity create \
  --type error_pattern \
  --name "TypeORM Connection Lost" \
  --content "Connection lost after idle timeout. Fix: Enable keepalive in connection options." \
  --tags "typeorm,postgresql,connection"
```

### Browsing Knowledge

```bash
# List all error patterns
sibyl entity list --type error_pattern

# List patterns by language
sibyl entity list --type pattern --language rust

# Get details on a specific pattern
sibyl entity show ent_abc123
```

## Related Commands

- [`sibyl add`](./add.md) - Quick knowledge capture (simpler interface)
- [`sibyl remember`](./remember.md) - Typed memory-loop writes
- [`sibyl search`](./search.md) - Semantic search
- [`sibyl explore related`](./explore.md) - Graph exploration
