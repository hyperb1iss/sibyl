# memory governance

Sibyl's memory loop is governed. Raw memories and reflection candidates are not written straight
into the shared graph; they move through review, promotion, and audit. This page covers the
governance command family:

| Command                                               | Description                                                      |
| ----------------------------------------------------- | ---------------------------------------------------------------- |
| [`sibyl memory-audit`](#memory-audit)                 | Inspect memory audit receipts                                    |
| [`sibyl memory-inspect`](#memory-inspect)             | Inspect a memory source and its audit trail                      |
| [`sibyl memory-import-status`](#memory-import-status) | Inspect a source import receipt and its published raw memory IDs |
| [`sibyl memory-promote`](#memory-promote)             | Preview or auto-review candidate promotion                       |
| [`sibyl memory-share`](#memory-share)                 | Preview memory sharing across scopes                             |
| [`sibyl memory-space`](#memory-space)                 | Memory-space inspection and preview                              |
| [`sibyl memory-review`](#memory-review)               | Reflection review queue automation                               |

For the dream-cycle automation that drives much of this, see [`memory-review`](#memory-review).

## Memory Scopes

Raw memories and artifacts carry a scope that controls who can recall them:

| Scope     | Visibility                             |
| --------- | -------------------------------------- |
| `private` | The capturing principal only (default) |
| `project` | Members working in a project           |
| `team`    | A named team                           |
| `shared`  | Org-wide shared memory                 |

`--scope-key` pins a scope to a specific project, team, or shared bucket.

---

## memory-audit

Inspect memory audit receipts. Every governed memory action (capture, promotion, share preview,
denial) writes an audit event. `memory-audit` reads that trail.

### Synopsis

```bash
sibyl memory-audit [options]
```

### Options

| Option         | Short | Default | Description                   |
| -------------- | ----- | ------- | ----------------------------- |
| `--action`     | `-a`  | (all)   | Filter by audit action        |
| `--actor`      |       | (all)   | Filter by actor user ID       |
| `--source-id`  |       | (all)   | Filter by source ID           |
| `--derived-id` |       | (all)   | Filter by derived ID          |
| `--scope`      |       | (all)   | Filter by memory scope        |
| `--project`    | `-p`  | (all)   | Filter by project ID          |
| `--policy`     |       | (all)   | Filter: `allowed` or `denied` |
| `--limit`      | `-l`  | 50      | Maximum events (1-200)        |
| `--json`       | `-j`  | false   | Output as JSON                |

### Examples

```bash
# Recent governed memory events
sibyl memory-audit

# Only denied actions
sibyl memory-audit --policy denied

# Promotions by a specific actor
sibyl memory-audit --action promote --actor user_abc123 --json
```

---

## memory-inspect

Inspect a memory source and its audit trail. Given a raw memory source ID, this shows the source
record together with every audit event that touched it.

### Synopsis

```bash
sibyl memory-inspect <source_id> [options]
```

### Arguments

| Argument    | Required | Description          |
| ----------- | -------- | -------------------- |
| `source_id` | Yes      | Raw memory source ID |

### Options

| Option   | Short | Description    |
| -------- | ----- | -------------- |
| `--json` | `-j`  | Output as JSON |

### Example

```bash
sibyl memory-inspect mem_abc123def456
```

---

## memory-import-status

Inspect a source import receipt and its published raw memory IDs. Given a source import ID, this
shows the import receipt together with the raw memory IDs the import published.

### Synopsis

```bash
sibyl memory-import-status <import_id> [options]
```

### Arguments

| Argument    | Required | Description      |
| ----------- | -------- | ---------------- |
| `import_id` | Yes      | Source import ID |

### Options

| Option   | Short | Description    |
| -------- | ----- | -------------- |
| `--json` | `-j`  | Output as JSON |

### Example

```bash
sibyl memory-import-status imp_abc123def456
```

---

## memory-promote

Preview, apply, or auto-review candidate promotion. The target is a raw memory or a reflection
candidate (a typed memory extracted by [`reflect`](./reflect.md) and routed to the review queue).
Promotion moves it into the shared graph.

### Synopsis

```bash
sibyl memory-promote <candidate_id> [options]
```

### Arguments

| Argument       | Required | Description                           |
| -------------- | -------- | ------------------------------------- |
| `candidate_id` | Yes      | Raw memory or reflection candidate ID |

### Options

| Option                   | Short | Description                                             |
| ------------------------ | ----- | ------------------------------------------------------- |
| `--preview`              |       | Preview without promoting                               |
| `--apply`                |       | Apply the promotion now                                 |
| `--auto`                 |       | Auto-review and promote when safe                       |
| `--dry-run`              |       | Evaluate auto-review without applying                   |
| `--confidence-threshold` |       | Override the auto-review confidence threshold (0.0-1.0) |
| `--scope`                |       | Target memory scope                                     |
| `--scope-key`            |       | Target scope key                                        |
| `--domain`               | `-d`  | Domain/category                                         |
| `--project`              | `-p`  | Project ID                                              |
| `--all-projects`         |       | Do not auto-scope to the linked project                 |
| `--related-to`           |       | Comma-separated graph IDs to relate after promotion     |
| `--task`                 |       | Comma-separated task IDs to relate after promotion      |
| `--json`                 | `-j`  | Output as JSON                                          |

### Promotion Modes

Exactly one of `--preview`, `--apply`, or `--auto` is required.

- `--preview`: show what promotion would produce; write nothing.
- `--apply`: apply the promotion now.
- `--auto`: auto-review the candidate and promote it when it clears the confidence threshold.
- `--dry-run`: with `--auto`, run the auto-review scoring and report the decision without applying
  it.

### Examples

```bash
# Preview a candidate before promoting
sibyl memory-promote cand_abc123 --preview

# Dry-run the auto-review decision
sibyl memory-promote cand_abc123 --dry-run

# Auto-promote into a project scope when safe
sibyl memory-promote cand_abc123 --auto \
  --scope project --scope-key proj_abc123 \
  --confidence-threshold 0.8
```

---

## memory-share

Preview memory sharing without enabling share writes. `memory-share` reports what sharing one or
more raw memories into another scope would entail. It is preview-only: it never enables a share.

### Synopsis

```bash
sibyl memory-share <source_ids>... [options]
```

### Arguments

| Argument     | Required | Description                     |
| ------------ | -------- | ------------------------------- |
| `source_ids` | Yes      | Raw memory IDs to share-preview |

### Options

| Option            | Short | Description                             |
| ----------------- | ----- | --------------------------------------- |
| `--preview`       |       | Preview without sharing                 |
| `--target-scope`  |       | Intended target scope                   |
| `--target-key`    |       | Target scope key                        |
| `--recipient-org` |       | Future recipient organization ID        |
| `--project`       | `-p`  | Project ID                              |
| `--all-projects`  |       | Do not auto-scope to the linked project |
| `--json`          | `-j`  | Output as JSON                          |

### Example

```bash
sibyl memory-share mem_abc123 mem_def456 \
  --target-scope shared --preview
```

---

## memory-space

Memory-space inspection and preview commands. A memory space groups raw memory under an access
boundary an agent or API key can be scoped to.

### memory-space preview-agent

Preview what an agent could recall from selected memory spaces. Use this to confirm an agent's reach
before granting it.

#### Synopsis

```bash
sibyl memory-space preview-agent <agent_id> --space <space_id> [options]
```

#### Arguments

| Argument   | Required | Description        |
| ---------- | -------- | ------------------ |
| `agent_id` | Yes      | Agent principal ID |

#### Options

| Option         | Short | Required | Description                                 |
| -------------- | ----- | -------- | ------------------------------------------- |
| `--space`      |       | Yes      | Primary memory space ID                     |
| `--also-space` |       | No       | Comma-separated additional memory space IDs |
| `--limit`      | `-l`  | No       | Maximum sources (1-200, default 50)         |
| `--json`       | `-j`  | No       | Output as JSON                              |

#### Example

```bash
sibyl memory-space preview-agent agent_abc123 \
  --space space_main \
  --also-space space_shared,space_team \
  --limit 100
```

---

## memory-review

Memory review queue automation commands. This is the reflection dream-cycle: the automation that
drains pending candidates, runs the org-scoped nightly maintenance job, and records decision
receipts.

| Subcommand                                      | Description                                                  |
| ----------------------------------------------- | ------------------------------------------------------------ |
| [`memory-review drain`](#memory-review-drain)   | Drain pending reflection candidates through automatic review |
| [`memory-review dream`](#memory-review-dream)   | Queue the automatic reflection dream-cycle job               |
| [`memory-review status`](#memory-review-status) | Show dream-cycle runs and automatic decision receipts        |

### memory-review drain

Drain pending reflection candidates through automatic review. By default this previews the drain;
`--apply` commits safe promotions.

#### Synopsis

```bash
sibyl memory-review drain [options]
```

#### Options

| Option                   | Short | Description                                               |
| ------------------------ | ----- | --------------------------------------------------------- |
| `--apply`                |       | Apply safe promotions instead of only previewing          |
| `--limit`                |       | Candidates to process (1-200, default 50)                 |
| `--confidence-threshold` |       | Override the auto-review confidence threshold (0.0-1.0)   |
| `--scope`                |       | Target memory scope                                       |
| `--scope-key`            |       | Target scope key                                          |
| `--domain`               | `-d`  | Domain/category                                           |
| `--project`              | `-p`  | Project ID                                                |
| `--all-projects`         |       | Do not auto-scope to the linked project                   |
| `--related-to`           |       | Comma-separated graph IDs to relate after promotion       |
| `--task`                 |       | Comma-separated task IDs to relate after promotion        |
| `--archive-exceptions`   |       | Archive terminal duplicate/stale exceptions when applying |
| `--archive-reasons`      |       | Comma-separated exception reasons eligible for archive    |
| `--json`                 | `-j`  | Output as JSON                                            |

#### Examples

```bash
# Preview the drain
sibyl memory-review drain

# Apply safe promotions and archive stale exceptions
sibyl memory-review drain --apply --archive-exceptions
```

### memory-review dream

Queue the automatic reflection dream-cycle maintenance job. The dream cycle is the org-scoped
nightly pass that reflects raw sources, drains candidates, and records lifecycle findings. By
default it queues a dry run; `--apply` queues a run that commits safe promotions.

#### Synopsis

```bash
sibyl memory-review dream [options]
```

#### Options

| Option                 | Description                                                                                            |
| ---------------------- | ------------------------------------------------------------------------------------------------------ |
| `--apply`              | Apply safe automatic promotions instead of a dry run                                                   |
| `--source-limit`       | Raw sources to process (0-100, default 20)                                                             |
| `--candidate-limit`    | Pending reflection candidates (0-200, default 50)                                                      |
| `--archive-exceptions` | Archive terminal duplicate/stale exceptions when applying (`--keep-exceptions` to disable, default on) |
| `--json` / `-j`        | Output as JSON                                                                                         |

#### Examples

```bash
# Queue a dry-run dream cycle
sibyl memory-review dream

# Queue an applying run with wider source coverage
sibyl memory-review dream --apply --source-limit 50
```

### memory-review status

Show reflection dream-cycle runs and automatic decision receipts.

#### Synopsis

```bash
sibyl memory-review status [options]
```

#### Options

| Option    | Short | Default | Description                |
| --------- | ----- | ------- | -------------------------- |
| `--limit` | `-l`  | 10      | Maximum runs/events (1-50) |
| `--json`  | `-j`  | false   | Output as JSON             |

#### Example

```bash
sibyl memory-review status --limit 20
```

## Related Commands

- [`sibyl remember`](./remember.md) - Capture durable memory
- [`sibyl reflect`](./reflect.md) - Produce reviewable reflection candidates
- [`sibyl recall`](./recall.md) - Recall memory into a context pack
- [`sibyl synthesis`](./synthesis.md) - Source-grounded synthesis from memory
