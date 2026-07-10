# remember

Remember a decision, plan, idea, claim, artifact, session, or learning. `remember` is the write side
of the Sibyl memory loop. It captures a titled memory and routes it into the graph, or stores it
verbatim as a raw memory or private agent diary entry.

## Synopsis

```bash
sibyl remember <title> [content] [options]
```

Content is read from stdin when the positional argument is omitted.

## Arguments

| Argument  | Required | Description                         |
| --------- | -------- | ----------------------------------- |
| `title`   | Yes      | Title/name of the memory            |
| `content` | No       | Memory body. Reads stdin if omitted |

## Options

| Option              | Short | Default   | Description                                              |
| ------------------- | ----- | --------- | -------------------------------------------------------- |
| `--content`         |       | (none)    | Memory body (alternative to positional)                  |
| `--content-file`    |       | (none)    | Read content from a file                                 |
| `--max-size`        |       | 1048576   | Maximum content file size in bytes                       |
| `--follow-symlinks` |       | false     | Allow `--content-file` to read through symlinks          |
| `--kind`            | `-k`  | `episode` | Entity type to create (see [entity](./entity.md))        |
| `--domain`          | `-d`  | (none)    | Domain/category                                          |
| `--project`         | `-p`  | (auto)    | Project ID                                               |
| `--all-projects`    |       | false     | Do not auto-scope to the linked project                  |
| `--tags`            |       | (none)    | Comma-separated tags                                     |
| `--related-to`      |       | (none)    | Comma-separated entity IDs to connect with `RELATED_TO`  |
| `--task`            |       | (none)    | Comma-separated task IDs to connect with `RELATED_TO`    |
| `--active-task`     |       | on        | Auto-link to the single active task (`--no-active-task`) |
| `--surface`         |       | `cli`     | Capture surface metadata                                 |
| `--wait-searchable` |       | false     | Wait until the memory is persisted and retrievable       |
| `--json`            | `-j`  | false     | Output as JSON                                           |
| `--raw`             |       | false     | Store verbatim raw memory only                           |
| `--diary`           |       | false     | Store a private agent diary entry                        |
| `--agent`           |       | (none)    | Agent identity for diary entries                         |
| `--source-id`       |       | (none)    | Raw memory source ID                                     |
| `--scope`           |       | `private` | Raw memory scope                                         |
| `--scope-key`       |       | (none)    | Project/team/org scope key                               |
| `--pin`             |       | false     | Exempt the memory from ordinary decay                    |
| `--basis`           |       | (none)    | `observed`, `inferred`, `told`, or `assumed`             |
| `--propose-scope`   |       | (none)    | Nominate the memory for audited team promotion           |

## Memory Kinds

`remember` defaults to `episode`, but the memory loop adds first-class kinds for durable reasoning
artifacts:

| Kind            | Use Case                                    |
| --------------- | ------------------------------------------- |
| `episode`       | General learning or knowledge (default)     |
| `decision`      | A choice that was made, with rationale      |
| `procedure`     | A repeatable implementation or runbook      |
| `error_pattern` | A recurring failure and its verified fix    |
| `rule`          | A durable constraint or invariant           |
| `plan`          | An intended sequence of work                |
| `idea`          | An exploration or proposal not yet acted on |
| `claim`         | An assertion to be verified or cited later  |
| `artifact`      | A produced output (synthesis, doc, summary) |
| `session`       | A session-level memory or summary           |
| `note`          | A durable observation or breadcrumb         |

Compatibility kinds remain accepted for existing automation, but new agent instructions should use
the compact set above.

## Examples

### Remember a Decision

```bash
sibyl remember "Use SurrealDB for the unified runtime" \
  "Chose SurrealDB over a Postgres+graph split. One store for graph, content, and auth removes sidecar drift and simplifies org isolation." \
  --kind decision
```

### Remember a Plan

```bash
sibyl remember "Synthesis rollout plan" \
  "Ship plan/draft/verify behind a flag, dogfood on docs, then expose synthesis_* MCP tools." \
  --kind plan --domain synthesis
```

### Remember from a File

```bash
sibyl remember "Incident 2026-05 postmortem" --content-file ./postmortem.md --kind episode
```

### Store a Raw Memory

`--raw` skips graph extraction and stores the payload verbatim in the raw memory store. Raw memories
are read back with `context --raw` and reviewed before promotion.

```bash
sibyl remember "Deploy runbook" --raw --scope project --scope-key proj_abc123
```

### Store an Agent Diary Entry

```bash
sibyl remember "Picked up the auth refactor" \
  "Started on token rotation. Blocker: Redis WRONGTYPE on refresh." \
  --diary --agent nova
```

### Pipe Content from stdin

```bash
git log -5 --oneline | sibyl remember "Recent auth commits" --kind episode
```

## Related Commands

- [`sibyl context`](./context.md) - Load memory back into an agent context
- [`sibyl reflect`](./reflect.md) - Turn raw notes into reviewable candidates
- [`sibyl capture`](./capture.md) - Quick capture without a separate title
- [Memory governance](./memory.md) - Inspect, audit, and promote raw memory
