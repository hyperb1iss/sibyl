# reflect

Reflect raw notes into memory candidates, optionally persisting them. `reflect` takes unstructured
session notes and runs them through Sibyl's extractor to produce typed memory candidates (decisions,
plans, ideas, claims, and learnings) you can review or commit.

## Synopsis

```bash
sibyl reflect [content] [options]
```

Content is read from stdin when the positional argument is omitted.

## Arguments

| Argument  | Required | Description                                  |
| --------- | -------- | -------------------------------------------- |
| `content` | No       | Raw notes to reflect. Reads stdin if omitted |

## Options

| Option           | Short | Default              | Description                                                                   |
| ---------------- | ----- | -------------------- | ----------------------------------------------------------------------------- |
| `--title`        | `-t`  | `Session reflection` | Source/session title                                                          |
| `--intent`       | `-i`  | `general`            | Intent: build, plan, review, debug, general                                   |
| `--domain`       | `-d`  | (none)               | Domain/category                                                               |
| `--project`      | `-p`  | (auto)               | Project ID                                                                    |
| `--all-projects` |       | false                | Do not auto-scope to the linked project                                       |
| `--related-to`   |       | (none)               | Comma-separated entity IDs to link persisted candidates to                    |
| `--task`         |       | (none)               | Comma-separated task IDs to link persisted output to                          |
| `--active-task`  |       | on                   | When persisting, auto-link to the active task (`--no-active-task`)            |
| `--persist`      |       | false                | Persist candidates into the graph                                             |
| `--source`       |       | on                   | When persisting, also store the raw notes as a session memory (`--no-source`) |
| `--review`       |       | false                | Store persisted output in the raw review queue instead of graph promotion     |
| `--limit`        | `-l`  | 12                   | Maximum candidates (1-25)                                                     |
| `--json`         | `-j`  | false                | Output as JSON                                                                |

## How It Works

`reflect` runs in three modes depending on flags:

1. **Preview** (default): extract candidates and print them. Nothing is written.
2. **Persist** (`--persist`): commit candidates straight into the graph as typed memories.
3. **Review** (`--persist --review`): route candidates into the raw review queue for governed
   promotion instead of writing directly to the graph.

With `--persist`, the `--source` flag (on by default) also stores the original notes as a session
memory so the extraction stays traceable to its input.

## Examples

### Preview Candidates from a Session

```bash
sibyl reflect "Decided to drop the Postgres sidecar. Idea: add a freshness score to synthesis verify. Still unsure how to scope shared memory across orgs."
```

### Reflect from stdin

```bash
cat session-notes.md | sibyl reflect --title "Auth refactor session" --intent debug
```

### Persist Candidates into the Graph

```bash
sibyl reflect --persist --project proj_abc123 < notes.txt
```

### Route Candidates to the Review Queue

Use `--review` when candidates should be governed before they land in the graph. They become pending
reflection candidates that `memory-promote` or `memory-review` can act on.

```bash
sibyl reflect --persist --review --domain synthesis < notes.txt
```

### JSON Output

```bash
sibyl reflect "..." --json | jq '.candidates[] | {kind, title}'
```

## Related Commands

- [`sibyl remember`](./remember.md) - Capture a single durable memory directly
- [`sibyl context`](./context.md) - Recall memory into an agent context
- [Memory governance](./memory.md) - Promote and audit reflection candidates
- [`sibyl synthesis`](./synthesis.md) - Source-grounded synthesis from memory
