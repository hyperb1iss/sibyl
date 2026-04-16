# Session Bundle

Package wake-up context for the current session. This command turns Sibyl's hook-era startup context
into a first-class CLI surface.

## Usage

```bash
sibyl session bundle
sibyl session bundle "archived entity paging"
sibyl session bundle --json
```

## What It Includes

- Current server and project context
- Doing or blocked tasks for the current project
- A few relevant memories derived from the current task titles or your explicit query
- One short "remember next" line

## Options

| Option           | Short | Default | Description                          |
| ---------------- | ----- | ------- | ------------------------------------ |
| `--task-limit`   | -     | `5`     | Maximum active tasks to include      |
| `--memory-limit` | -     | `3`     | Maximum relevant memories to include |
| `--all`          | `-a`  | false   | Search across all projects           |
| `--json`         | `-j`  | false   | Output machine-readable bundle JSON  |

## Examples

Wake up with current project context:

```bash
sibyl session bundle
```

Focus the bundle on a specific topic:

```bash
sibyl session bundle "backup job ids"
```

Use JSON from hooks or automation:

```bash
sibyl session bundle --json | jq '.remember_next'
```

Search across all projects:

```bash
sibyl session bundle --all
```
