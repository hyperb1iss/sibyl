# ingest

Import local source archives into raw memory. `ingest` reads exported agent transcripts as JSONL and
streams each turn into Sibyl's raw memory store, where [`sibyl reflect`](./reflect.md) can later
promote it into durable graph memory.

This is the transcript path. For files, directories, and URLs of arbitrary documents, use
[`sibyl docs`](./docs.md); for crawling websites, use [`sibyl crawl`](./crawl.md).

## Commands

| Command                                           | Description                         |
| ------------------------------------------------- | ----------------------------------- |
| [`sibyl ingest claude-code`](#ingest-claude-code) | Import Claude Code transcript JSONL |
| [`sibyl ingest codex`](#ingest-codex)             | Import Codex transcript JSONL       |

Both subcommands share the same options and the same import pipeline; only the source adapter
differs.

---

## ingest claude-code

Import a Claude Code transcript JSONL file or a directory of them.

```bash
sibyl ingest claude-code <source> [options]
```

| Argument | Required | Description                         |
| -------- | -------- | ----------------------------------- |
| `source` | Yes      | Claude Code JSONL file or directory |

| Option              | Default   | Description                                     |
| ------------------- | --------- | ----------------------------------------------- |
| `--scope`           | `private` | Target memory scope                             |
| `--scope-key`       | (none)    | Target scope key for project/team/shared scopes |
| `--source-identity` | (none)    | Stable identity for moved transcript exports    |
| `--batch-size`      | 100       | Import batch size                               |
| `--drain`           | false     | Wait for the background drain to finish         |
| `--poll-interval`   | 1.0       | Seconds between drain status checks             |
| `--timeout`         | (none)    | Maximum seconds to wait when draining           |
| `--json` / `-j`     | false     | JSON output for scripting                       |

### Examples

```bash
# Import a single session export
sibyl ingest claude-code ~/.claude/projects/sibyl/session.jsonl

# Import a directory and wait for it to finish
sibyl ingest claude-code ~/.claude/projects/sibyl/ --drain

# Import into a shared project scope as JSON
sibyl ingest claude-code session.jsonl --scope project --scope-key sibyl --json
```

---

## ingest codex

Import a Codex transcript JSONL file or a directory of them. Identical to `claude-code` apart from
the source format.

```bash
sibyl ingest codex <source> [options]
```

| Argument | Required | Description                   |
| -------- | -------- | ----------------------------- |
| `source` | Yes      | Codex JSONL file or directory |

| Option              | Default   | Description                                     |
| ------------------- | --------- | ----------------------------------------------- |
| `--scope`           | `private` | Target memory scope                             |
| `--scope-key`       | (none)    | Target scope key for project/team/shared scopes |
| `--source-identity` | (none)    | Stable identity for moved transcript exports    |
| `--batch-size`      | 100       | Import batch size                               |
| `--drain`           | false     | Wait for the background drain to finish         |
| `--poll-interval`   | 1.0       | Seconds between drain status checks             |
| `--timeout`         | (none)    | Maximum seconds to wait when draining           |
| `--json` / `-j`     | false     | JSON output for scripting                       |

### Examples

```bash
sibyl ingest codex ~/.codex/sessions/today.jsonl
sibyl ingest codex ~/.codex/sessions/ --drain --timeout 120
```

## Notes

- The source must be a real file or directory, not a symlink, and the path is resolved before
  upload.
- Imports queue by default and drain in the background. Pass `--drain` to block until the import
  reaches a terminal state (`completed`, `failed`, or `canceled`).
- Each imported turn lands as a raw memory. Use `--source-identity` to keep dedupe stable when you
  move or rename the same export between runs.

## Related Commands

- [`sibyl reflect`](./reflect.md) - Promote raw memories into graph memory
- [`sibyl recall`](./recall.md) - Recall raw memories with `--raw`
- [`sibyl docs`](./docs.md) - Import files, directories, or URLs as documents
- [`sibyl crawl`](./crawl.md) - Crawl and ingest documentation sites
