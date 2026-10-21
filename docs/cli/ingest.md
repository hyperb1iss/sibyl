# ingest

Import what your coding agents already know. `ingest memories` brings in the memory files Claude
Code and Codex keep on this machine as typed memories. `ingest claude-code` and `ingest codex` read
exported transcripts as JSONL and stream each turn into Sibyl's raw memory store, where
[`sibyl reflect`](./reflect.md) can later promote it into durable graph memory.

For files, directories, and URLs of arbitrary documents, use [`sibyl docs`](./docs.md); for crawling
websites, use [`sibyl crawl`](./crawl.md).

## Commands

| Command                                           | Description                                        |
| ------------------------------------------------- | -------------------------------------------------- |
| [`sibyl ingest memories`](#ingest-memories)       | Import the memory files Claude Code and Codex keep |
| [`sibyl ingest claude-code`](#ingest-claude-code) | Import Claude Code transcript JSONL                |
| [`sibyl ingest codex`](#ingest-codex)             | Import Codex transcript JSONL                      |

The two transcript subcommands share the same options and the same import pipeline; only the source
adapter differs.

---

## ingest memories

Import the memory files your coding agents keep on this machine. They are already distilled by the
agent, so each file lands whole as one raw memory that carries its kind (rule, note, artifact,
session), rather than as transcript turns. Recall finds them right away, and
[`sibyl reflect`](./reflect.md) can promote them into graph memory. The files are read here and sent
through the API, so this works against any server, including a team server.

```bash
sibyl ingest memories [OPTIONS]
```

### What it reads

| Agent       | Files                                      | Kind                                                                               |
| ----------- | ------------------------------------------ | ---------------------------------------------------------------------------------- |
| Claude Code | `~/.claude/projects/*/memory/*.md`         | `feedback` → rule, `reference` → artifact, `project` → note, `user` → private note |
| Codex       | `~/.codex/memories/rollout_summaries/*.md` | One session per summary, attached to the repository it ran in                      |
| Codex       | `~/.codex/memories/memory_summary.md`      | Your Codex user profile, as a private note                                         |

Index files (Claude Code's `MEMORY.md`, Codex's `MEMORY.md` and `raw_memories.md`) only point at
these and are skipped, as are symlinks, empty files, and files over 256 KiB.

### Where memories go

Memories import into the server this CLI talks to (pass `-C <context>` to pick another), private to
you by default. Each one routes the way commands run in its repository would:

- A memory from a repository linked to a project on that server attaches to the project.
- A memory from a repository pinned to another context is left for that server; the summary prints
  the `sibyl -C <context> ingest memories` command that imports it.
- Personal memories (Claude Code `user` memories and the Codex profile) never attach to a project
  and always stay private.

### Re-running

Each file keeps a stable source ID, so running the import again is safe:

- An unchanged file is skipped.
- A changed file revises its memory in place; Sibyl keeps the earlier content in its revision
  history.
- After you link a repository, its memories import again attached to the project, and the unattached
  copies are marked superseded.
- A memory you retired in Sibyl (deleted, superseded, archived) is left alone.

### Options

| Option         | Short | Default     | Description                                                     |
| -------------- | ----- | ----------- | --------------------------------------------------------------- |
| `--from`       |       | both        | Agent to import from: `claude-code` or `codex` (repeatable)     |
| `--share`      |       | `false`     | Make memories from linked repositories visible to their project |
| `--dry-run`    |       | `false`     | Show what would change without writing                          |
| `--yes`        | `-y`  | `false`     | Don't ask before writing                                        |
| `--claude-dir` |       | `~/.claude` | Claude Code home                                                |
| `--codex-dir`  |       | `~/.codex`  | Codex home                                                      |
| `--json`       | `-j`  | `false`     | JSON summary for scripting                                      |

`--share` applies when a memory is first imported; share or promote an existing memory with the
memory sharing commands.

### Examples

```bash
# See what would import, without writing
sibyl ingest memories --dry-run

# Import from Claude Code only
sibyl ingest memories --from claude-code

# Import into the team server, sharing project memories with each project
sibyl -C team ingest memories --share
```

[`sibyl setup`](./setup.md) offers this import once a machine is connected.

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
- [`sibyl context`](./context.md) - Recall raw memories with `--raw`
- [`sibyl docs`](./docs.md) - Import files, directories, or URLs as documents
- [`sibyl crawl`](./crawl.md) - Crawl and ingest documentation sites
