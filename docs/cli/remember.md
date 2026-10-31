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

| Option                | Short | Default   | Description                                              |
| --------------------- | ----- | --------- | -------------------------------------------------------- |
| `--content`           |       | (none)    | Memory body (alternative to positional)                  |
| `--content-file`      |       | (none)    | Read content from a file                                 |
| `--max-size`          |       | 1048576   | Maximum content file size in bytes                       |
| `--follow-symlinks`   |       | false     | Allow `--content-file` to read through symlinks          |
| `--kind`              | `-k`  | `episode` | Entity type to create (see [entity](./entity.md))        |
| `--domain`            | `-d`  | (none)    | Domain/category                                          |
| `--project`           | `-p`  | (auto)    | Project ID                                               |
| `--all-projects`      |       | false     | Do not auto-scope to the linked project                  |
| `--tags`              |       | (none)    | Comma-separated tags                                     |
| `--related-to`        |       | (none)    | Comma-separated entity IDs to connect with `RELATED_TO`  |
| `--task`              |       | (none)    | Comma-separated task IDs to connect with `RELATED_TO`    |
| `--active-task`       |       | on        | Auto-link to the single active task (`--no-active-task`) |
| `--surface`           |       | `cli`     | Capture surface metadata                                 |
| `--wait-searchable`   |       | false     | Wait until the memory is persisted and retrievable       |
| `--json`              | `-j`  | false     | Output as JSON                                           |
| `--raw`               |       | false     | Store verbatim raw memory only                           |
| `--diary`             |       | false     | Store a private agent diary entry                        |
| `--agent`             |       | (none)    | Agent identity for diary entries                         |
| `--source-id`         |       | (none)    | Raw memory source ID                                     |
| `--scope`             |       | (project) | Memory scope; default is the project's default scope     |
| `--scope-key`         |       | (none)    | Project or team scope key                                |
| `--pin`               |       | false     | Exempt the memory from ordinary decay                    |
| `--basis`             |       | (none)    | `observed`, `inferred`, `told`, or `assumed`             |
| `--propose-scope`     |       | (none)    | Nominate the memory for audited team promotion           |
| `--propose-scope-key` |       | (none)    | Team UUID the nomination targets                         |
| `--expires`           |       | (none)    | Retire the memory after a duration (`30m`, `2h`, `1d`)   |

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

### Choose Who Sees It

Without `--scope`, the memory lands in the linked project's default write scope, which is private
unless the project owner shared the project. The receipt reports where it landed. Name a scope to
decide yourself:

```bash
# Share with everyone who can read the project
sibyl remember "Checkout 500s" "Payments pool exhausts under retry storms." --kind error_pattern --scope project

# Keep it yours, even in a shared project
sibyl remember "My shell alias" "alias k=kubectl" --kind note --scope private
```

See [Sharing Memory](../users/sharing-memory.md) for every scope and who reads it.

### Nominate a Memory for a Team

`--propose-scope team` asks the audited promotion pipeline to consider the memory for team scope. It
does not share anything immediately. Add `--propose-scope-key` with the team's UUID (from
`sibyl team list`) so the nomination names its team. Promotion still checks team membership; the key
is a hint, never a grant.

```bash
sibyl remember "Review ritual" "Verifier passes are mandatory." --kind rule \
  --propose-scope team --propose-scope-key 0f8b6c52-4d2e-4a7a-9c1d-6e5b2a9f3d10
```

`--scope org` and `--scope organization` are refused before any request is sent: organization-wide
memory has no read or write policy yet. Use `project` or `team` instead.

### Post a Time-Boxed Team Notice

`--expires` retires the memory after a duration between one minute and 30 days. It is meant for live
state the team needs while it is true, like an active incident, never for durable knowledge. The
memory is stored verbatim like `--raw`, surfaces in team members' packs when it is relevant to their
goal, with an `expires=` marker, and when the deadline passes a scheduled sweep marks it stale with
the reason `expired`. Resolve it early with
`sibyl correct <raw-memory-id> --action stale --reason resolved`. See
[time-boxed team notices](../users/sharing-memory.md#time-boxed-team-notices).

```bash
sibyl remember "Checkout outage" "Checkout is returning 502s; hold all deploys." \
  --scope team --scope-key <team-uuid> --expires 2h
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
