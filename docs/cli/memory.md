# memory governance

Sibyl's memory loop is governed. Raw memories and reflection candidates are not written straight
into the shared graph; they move through review, promotion, and audit. Agents inspect and repair
source truth with `correct`; operators use the `admin memory` command family:

| Command                         | Description                                  |
| ------------------------------- | -------------------------------------------- |
| [`sibyl correct`](#correct)     | Inspect or correct a raw memory source       |
| [`sibyl cite`](#usage-feedback) | Record material citation or misleading usage |

| Command                                                     | Description                                                      |
| ----------------------------------------------------------- | ---------------------------------------------------------------- |
| [`sibyl admin memory audit`](#memory-audit)                 | Inspect memory audit receipts                                    |
| [`sibyl admin memory inspect`](#memory-inspect)             | Inspect a memory source and its audit trail                      |
| [`sibyl admin memory import-status`](#memory-import-status) | Inspect a source import receipt and its published raw memory IDs |
| [`sibyl admin memory promote`](#memory-promote)             | Preview or auto-review candidate promotion                       |
| [`sibyl admin memory share`](#memory-share)                 | Preview memory sharing across scopes                             |
| [`sibyl admin memory space`](#memory-space)                 | Memory-space inspection and preview                              |
| [`sibyl admin memory review`](#memory-review)               | Reflection review queue automation                               |

For the dream-cycle automation that drives much of this, see [`memory-review`](#memory-review).

## correct

Run `correct` without an action to inspect revisions, corrections, audits, derivations, and
supersession lineage:

```bash
sibyl correct raw_memory:abc123
```

Apply the smallest correction matching what happened and provide a durable reason:

```bash
sibyl correct raw_memory:abc123 --action wrong \
  --reason "Contradicted by the verified configuration"
sibyl correct raw_memory:abc123 --action stale \
  --reason "Valid only before the v2 migration"
sibyl correct raw_memory:abc123 --action duplicate \
  --duplicate-of raw_memory:def456 --reason "Same decision captured twice"
sibyl correct raw_memory:abc123 --action superseded \
  --replacement raw_memory:def456 --reason "The newer decision replaces this one"
printf '%s' "Corrected canonical body" | sibyl correct raw_memory:abc123 \
  --action revise --reason "The prior wording was misleading" --expected-revision 3
```

Every applied correction returns a mutation receipt with its operation ID, affected records, and
revision. Use `--preview` to validate a mutation without applying it. The hidden `blame` alias
remains available during migration, but new instructions should use `correct` for inspection.

## Usage Feedback

Record positive feedback only when memory materially shaped the result:

```bash
sibyl cite decision_abc raw_memory:def456
```

Use `--misled` only when the cited memory shaped the result incorrectly. Irrelevant or unused
context is not misleading:

```bash
sibyl cite raw_memory:def456 --misled
```

## Memory Scopes

Raw memories and artifacts carry a scope that controls who can recall them:

| Scope     | Visibility                             |
| --------- | -------------------------------------- |
| `private` | The capturing principal only (default) |
| `project` | Members working in a project           |
| `team`    | A named team                           |
| `org`     | Organization-wide memory               |

`--scope-key` pins a scope to a specific project, team, or organization bucket.

---

## memory-audit

Inspect memory audit receipts. Every governed memory action (capture, promotion, share preview,
denial) writes an audit event. `memory-audit` reads that trail.

### Synopsis

```bash
sibyl admin memory audit [options]
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
sibyl admin memory audit

# Only denied actions
sibyl admin memory audit --policy denied

# Promotions by a specific actor
sibyl admin memory audit --action promote --actor user_abc123 --json
```

---

## memory-inspect

Inspect a memory source and its audit trail. Given a raw memory source ID, this shows the source
record together with every audit event that touched it.

### Synopsis

```bash
sibyl admin memory inspect <source_id> [options]
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
sibyl admin memory inspect mem_abc123def456
```

---

## memory-import-status

Inspect a source import receipt and its published raw memory IDs. Given a source import ID, this
shows the import receipt together with the raw memory IDs the import published.

### Synopsis

```bash
sibyl admin memory import-status <import_id> [options]
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
sibyl admin memory import-status imp_abc123def456
```

---

## memory-promote

Preview, apply, or auto-review candidate promotion. The target is a raw memory or a reflection
candidate (a typed memory extracted by [`reflect`](./reflect.md) and routed to the review queue).
Promotion moves it into the shared graph.

### Synopsis

```bash
sibyl admin memory promote <candidate_id> [options]
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
sibyl admin memory promote cand_abc123 --preview

# Dry-run the auto-review decision
sibyl admin memory promote cand_abc123 --dry-run

# Auto-promote into a project scope when safe
sibyl admin memory promote cand_abc123 --auto \
  --scope project --scope-key proj_abc123 \
  --confidence-threshold 0.8
```

---

## memory-share

Preview or apply promotion-backed memory sharing. `memory-share` reports what sharing one or more
raw memories into another scope would entail; add `--apply` to perform the sharing writes.

### Synopsis

```bash
sibyl admin memory share <source_ids>... [options]
```

### Arguments

| Argument     | Required | Description                     |
| ------------ | -------- | ------------------------------- |
| `source_ids` | Yes      | Raw memory IDs to share-preview |

### Options

| Option            | Short | Description                             |
| ----------------- | ----- | --------------------------------------- |
| `--apply`         |       | Apply sharing writes                    |
| `--preview`       |       | Preview without sharing                 |
| `--target-scope`  |       | Intended target scope                   |
| `--target-key`    |       | Target scope key                        |
| `--recipient-org` |       | Future recipient organization ID        |
| `--project`       | `-p`  | Project ID                              |
| `--all-projects`  |       | Do not auto-scope to the linked project |
| `--json`          | `-j`  | Output as JSON                          |

### Examples

```bash
# Preview what sharing would entail
sibyl admin memory share mem_abc123 mem_def456 \
  --target-scope shared --preview

# Apply the sharing writes
sibyl admin memory share mem_abc123 mem_def456 \
  --target-scope shared --apply
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
sibyl admin memory space preview-agent <agent_id> --space <space_id> [options]
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
sibyl admin memory space preview-agent agent_abc123 \
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
sibyl admin memory review drain [options]
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
sibyl admin memory review drain

# Apply safe promotions and archive stale exceptions
sibyl admin memory review drain --apply --archive-exceptions
```

### memory-review dream

Queue the automatic reflection dream-cycle maintenance job. The dream cycle is the org-scoped
nightly pass that reflects raw sources, drains candidates, and records lifecycle findings. By
default it queues a dry run; `--apply` queues a run that commits safe promotions.

#### Synopsis

```bash
sibyl admin memory review dream [options]
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
sibyl admin memory review dream

# Queue an applying run with wider source coverage
sibyl admin memory review dream --apply --source-limit 50
```

### memory-review status

Show reflection dream-cycle runs and automatic decision receipts.

#### Synopsis

```bash
sibyl admin memory review status [options]
```

#### Options

| Option    | Short | Default | Description                |
| --------- | ----- | ------- | -------------------------- |
| `--limit` | `-l`  | 10      | Maximum runs/events (1-50) |
| `--json`  | `-j`  | false   | Output as JSON             |

#### Example

```bash
sibyl admin memory review status --limit 20
```

## Related Commands

- [`sibyl remember`](./remember.md) - Capture durable memory
- [`sibyl reflect`](./reflect.md) - Produce reviewable reflection candidates
- [`sibyl context`](./context.md) - Recall memory into a context pack
- [`sibyl synthesis`](./synthesis.md) - Source-grounded synthesis from memory
