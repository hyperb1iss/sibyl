# Export Memory to Files

Materialize a project's memory into `.sibyl/memory/`, a directory of Markdown files that any
filesystem-native agent can read and grep without talking to a Sibyl server.

The graph stays the source of truth. This is a curated projection of it: structured substrate,
flat-file surface.

## Usage

```bash
sibyl export memory
sibyl export memory --project sibyl
sibyl export memory --output docs/memory --notes 100
```

With no `--project`, the command uses the project linked to the current directory
(see [`sibyl project link`](./project.md)).

## Why Files

Every major coding agent in 2026 reads Markdown from the repository it is working in. A repo-local
`.sibyl/memory/` is legible to all of them with zero integration work, costs nothing per read, and
keeps working when the server does not. Recording usage still goes over the API
(`sibyl cite <id>`), so the citation signal stays honest.

## Layout

```
.sibyl/memory/
├── README.md         # what this is, how to read it, how to cite it
├── index.md          # every materialized memory, grouped by type, with ids and paths
├── handbook.md       # distilled project handbook (written only when one exists)
├── recent.md         # recency-ordered digest with previews
├── tasks.md          # open work with task ids
├── notes/            # one file per memory, full text
│   └── <type>-<title-slug>.md
└── manifest.json     # per-file SHA-256 plus a digest over the whole projection
```

Three tiers, deliberately: `README.md` and `handbook.md` orient, `index.md` / `recent.md` /
`tasks.md` are the greppable middle, and `notes/` holds the detail an agent opens once grep points
at it.

`handbook.md` is a reserved slot. It appears when a distilled handbook has been built for the
project and is simply absent otherwise, so nothing downstream has to special-case a placeholder.

## File Format

Every Markdown file opens with YAML frontmatter carrying a `type`, which makes the projection
OKF v0.1-compatible: Markdown with YAML frontmatter in a git-shippable directory. Sibyl-specific
keys are namespaced under `sibyl_`.

```markdown
---
type: "Sibyl Pattern"
title: "Pool Surreal connections per org"
description: "Each org gets a dedicated connection-pooled client scoped to its namespace…"
timestamp: "2026-07-22T09:30:00Z"
okf_version: "0.1"
sibyl_schema_version: "sibyl-memory-files-v1"
sibyl_kind: "memory_note"
sibyl_id: "pattern_a1b2c3d4"
sibyl_entity_type: "pattern"
sibyl_project_id: "project_05eb5c8c782a"
---

# Pool Surreal connections per org

...full memory body...

## Provenance

- Sibyl id: `pattern_a1b2c3d4`
- Entity type: `pattern`
- Cite with: `sibyl cite pattern_a1b2c3d4`
```

This differs from `sibyld export okf`, which embeds a lossless JSON payload in frontmatter so an
archive can round-trip back into a graph. That format is for backup and portability. This one is
for reading, so it carries only the fields a reader needs.

## Determinism

Re-running against an unchanged graph rewrites **byte-identical files**. The directory is meant to
live in git, and a projection that churned on every run would bury real changes in noise.

Three rules make that hold:

- **No wall-clock values.** Nothing samples `datetime.now()`. Timestamps in the output come from the
  data. `manifest.json` carries a content digest instead of an export time.
- **Whitelisted fields.** Retrieval counters, last-recalled stamps, embedding metadata, and internal
  record handles all change when memory is merely *read*. None of them reach the files.
- **Total ordering.** Records sort by id for file placement and by recency (id as tiebreak) for
  reading order, so the order the server returned them in cannot leak into the bytes.

Verify a materialized directory at any time:

```bash
sibyl export memory --output /tmp/check --json | jq -r .content_digest
jq -r .content_digest .sibyl/memory/manifest.json
```

## Options

| Option           | Short | Default          | Description                                            |
| ---------------- | ----- | ---------------- | ------------------------------------------------------ |
| `--project`      | `-p`  | linked project   | Project id or name to materialize                      |
| `--output`       | `-o`  | `.sibyl/memory`  | Directory to write into                                |
| `--notes`        | -     | `60`             | Maximum memories to include                            |
| `--tasks`        | -     | `25`             | Maximum open tasks to include                          |
| `--task-status`  | -     | `doing,blocked`  | Comma-separated task statuses to materialize           |
| `--type`         | `-T`  | ten memory types | Memory entity type to include (repeatable)             |
| `--hydrate`      | -     | true             | Refetch each entity for its full body                  |
| `--writable`     | -     | false            | Leave files writable instead of read-only              |
| `--force`        | -     | false            | Write into a populated directory that is not an export |
| `--json`         | `-j`  | false            | Print the export receipt as JSON                       |

The note budget is spread across the requested types in round-robin rather than spent on whichever
type the backend happens to order first, so a 40-item export covers decisions, patterns, procedures,
claims, and the rest instead of forty decisions.

`--hydrate` costs one extra request per memory. Turning it off is faster and yields
500-character previews, which defeats the point for anything but a smoke test.

## Reading the Result

```bash
# what does this project know about connection pooling?
rg -i 'connection pool' .sibyl/memory/notes

# every decision, by file
rg -l 'type: "Sibyl Decision"' .sibyl/memory/notes

# what is open right now?
cat .sibyl/memory/tasks.md
```

Files are written read-only. Edit the graph, not the directory: the next export overwrites
everything Sibyl manages.

`manifest.json` is the only authority on what Sibyl owns, which makes the write path safe in two
ways. A file this exporter never wrote is never deleted, wherever it sits, so your own notes under
`notes/` survive a re-export. And a populated directory holding no `manifest.json` is refused rather
than overwritten, so `--output .` in a repo root cannot eat that repo's `README.md`. Pass `--force`
when you genuinely mean to write into one.

## Related Commands

- [`sibyl context`](./context.md) - Live, goal-scoped context pack from the server
- [`sibyl session`](./session.md) - Wake-up bundle for the current session
- [`sibyl project`](./project.md) - Link a directory to a project
