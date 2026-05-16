# capture

Capture a quick memory without separate title and content fields. `capture` is the fastest way
into the memory loop: pass a single blob of text (or pipe it in) and Sibyl derives a title for you.

## Synopsis

```bash
sibyl capture [content] [options]
```

Content is read from stdin when the positional argument is omitted.

## Arguments

| Argument  | Required | Description                                |
| --------- | -------- | ------------------------------------------ |
| `content` | No       | What to capture. Reads stdin if omitted    |

## Options

| Option              | Short | Default   | Description                                              |
| ------------------- | ----- | --------- | -------------------------------------------------------- |
| `--title`           | `-t`  | (derived) | Optional title. Derived from content when omitted        |
| `--type`            |       | `episode` | Entity type to create (see [entity](./entity.md))        |
| `--tags`            |       | (none)    | Comma-separated tags                                     |
| `--project`         | `-p`  | (auto)    | Project ID                                               |
| `--all-projects`    |       | false     | Do not auto-scope to the linked project                  |
| `--related-to`      |       | (none)    | Comma-separated entity IDs to connect with `RELATED_TO`  |
| `--task`            |       | (none)    | Comma-separated task IDs to connect with `RELATED_TO`    |
| `--active-task`     |       | on        | Auto-link to the single active task (`--no-active-task`) |
| `--content-file`    |       | (none)    | Read content from a file                                 |
| `--max-size`        |       | 1048576   | Maximum content file size in bytes                       |
| `--follow-symlinks` |       | false     | Allow `--content-file` to read through symlinks          |
| `--wait-searchable` |       | false     | Wait until the entity is persisted and retrievable       |
| `--json`            | `-j`  | false     | Output as JSON                                           |

## When to Use

Reach for `capture` when you have a thought to record and do not want to stop to phrase a title.
Use [`remember`](./remember.md) when the memory has a clear name or a specific kind (decision,
plan, claim). Use [`add`](./add.md) when you want explicit title and content fields.

## Examples

### Quick Capture

```bash
sibyl capture "PgBouncer transaction mode fixed the connection timeout under load"
```

### Pipe Content In

```bash
echo "Remember to backfill embeddings after the cutover" | sibyl capture
```

### Capture with a Type and Tags

```bash
sibyl capture "Always clone the Surreal driver per org before writing" \
  --type pattern --tags "surreal,concurrency"
```

### Capture from a File

```bash
sibyl capture --content-file ./scratch-notes.md --type episode
```

### Link to the Active Task

When a task is in `doing`, captures auto-link to it. Disable with `--no-active-task`:

```bash
sibyl capture "Found the root cause in the token refresh path"
sibyl capture "Unrelated idea for the docs site" --no-active-task
```

## Related Commands

- [`sibyl remember`](./remember.md) - Capture a titled, typed memory
- [`sibyl note`](./remember.md) - Add a task note or free note memory
- [`sibyl add`](./add.md) - Add knowledge with explicit title and content
- [`sibyl archive`](./archive.md) - Browse raw quick captures
