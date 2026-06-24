# recall

Recall a compact working context pack for an agent. `recall` is the read side of the Sibyl memory
loop. Given a goal, it compiles the most relevant tasks, decisions, plans, and graph neighbors into
a single pack an agent can drop into context.

## Synopsis

```bash
sibyl recall <goal> [options]
```

## Arguments

| Argument | Required | Description             |
| -------- | -------- | ----------------------- |
| `goal`   | Yes      | Agent goal or user task |

## Options

| Option        | Short | Default   | Description                                            |
| ------------- | ----- | --------- | ------------------------------------------------------ |
| `--intent`    | `-i`  | `build`   | Agent intent (see below)                               |
| `--layer`     |       | `recall`  | Context depth: `wake`, `recall`, `deep_search`         |
| `--domain`    | `-d`  | (none)    | Domain/category to bias retrieval                      |
| `--project`   | `-p`  | (auto)    | Project ID                                             |
| `--agent`     |       | (none)    | Agent diary identity to include                        |
| `--all`       | `-a`  | false     | Use all accessible projects                            |
| `--limit`     | `-l`  | 12        | Maximum context items (1-50)                           |
| `--related`   |       | on        | Include one-hop related graph context (`--no-related`) |
| `--audit`     |       | false     | Include full retrieval metadata per item               |
| `--budget`    |       | (none)    | Cap rendered markdown at ~N tokens (100-8000)          |
| `--json`      | `-j`  | false     | Output full JSON                                       |
| `--raw`       |       | false     | Recall verbatim raw memories                           |
| `--diary`     |       | false     | Recall a private agent diary                           |
| `--scope`     |       | `private` | Raw memory scope                                       |
| `--scope-key` |       | (none)    | Project/team/shared scope key                          |

The following flags filter raw imports and apply only with `--raw` or `--diary`:

| Option              | Short | Default | Description                                      |
| ------------------- | ----- | ------- | ------------------------------------------------ |
| `--participant`     |       | (none)  | Filter raw imports by participant (repeatable)   |
| `--label`           |       | (none)  | Filter raw imports by adapter label (repeatable) |
| `--thread`          |       | (none)  | Filter raw imports by thread                     |
| `--occurred-after`  |       | (none)  | Filter raw imports after an ISO timestamp        |
| `--occurred-before` |       | (none)  | Filter raw imports before an ISO timestamp       |
| `--as-of`           |       | (now)   | Filter raw memory by validity timestamp          |

## Intent

The `--intent` flag biases what kind of memory the pack favors:

| Intent     | Bias                                      |
| ---------- | ----------------------------------------- |
| `build`    | Implementation patterns and active tasks  |
| `plan`     | Decisions, plans, roadmap context         |
| `ideate`   | Ideas, open questions, prior exploration  |
| `research` | Claims, sources, documents                |
| `review`   | Recent changes, prior review notes        |
| `debug`    | Error patterns, gotchas, related failures |
| `decide`   | Decisions, constraints, tradeoffs         |
| `learn`    | Guides, procedures, durable knowledge     |
| `general`  | Balanced mix                              |

## Layers

`--layer` controls how deep the recall reaches:

| Layer         | Behavior                                                       |
| ------------- | -------------------------------------------------------------- |
| `wake`        | Fast startup pack: active tasks and a few high-signal memories |
| `recall`      | Default working pack scoped to the goal                        |
| `deep_search` | Wider semantic sweep across the graph                          |

## Examples

### Working Context Pack

```bash
sibyl recall "wire up the password reset endpoint"
```

### Debug Intent

```bash
sibyl recall "auth token refresh fails intermittently" --intent debug
```

### Deep Search

```bash
sibyl recall "how synthesis verification works" --layer deep_search --limit 25
```

### Recall Raw Memories

By default `recall` returns graph-extracted entities. Use `--raw` to pull verbatim raw memory
sources captured with `remember --raw`:

```bash
sibyl recall "deployment runbook" --raw --scope project
```

### Recall an Agent Diary

```bash
sibyl recall "what nova was working on" --diary --agent nova
```

### JSON for Agent Injection

```bash
sibyl recall "implement OAuth2" --json | jq '.items[].title'
```

## Related Commands

- [`sibyl remember`](./remember.md) - Capture durable memory (the write side)
- [`sibyl reflect`](./reflect.md) - Turn raw notes into memory candidates
- [`sibyl context pack`](./context.md) - Lower-level context compilation
- [`sibyl session`](./session.md) - Wake-up bundle for a new session
