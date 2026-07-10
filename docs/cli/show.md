# show

Show full content for a graph entity or raw memory ID.

Use this after `sibyl context`, `sibyl context`, or any command that prints an ID. The command
accepts graph entity IDs, raw memory IDs, and `raw_memory:<id>` references.

## Synopsis

```bash
sibyl show <id> [options]
```

## Arguments

| Argument | Required | Description                    |
| -------- | -------- | ------------------------------ |
| `id`     | Yes      | Entity or raw memory reference |

## Options

| Option   | Short | Description |
| -------- | ----- | ----------- |
| `--json` | `-j`  | JSON output |

## Examples

```bash
# Show a graph entity from search results
sibyl show plan_df925c5b6eed

# Show a raw memory from search or metadata
sibyl show raw_memory:1f08c55c-b67a-475b-b52b-922c675ff748

# Bare raw memory UUIDs work too
sibyl show 1f08c55c-b67a-475b-b52b-922c675ff748
```
