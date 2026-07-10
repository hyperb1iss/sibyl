# synthesis

Source-grounded synthesis commands. `synthesis` turns authorized memory into structured artifacts
(documentation, summaries, briefs) where every claim traces back to a cited source. The pipeline is
plan, draft, verify, and remember.

## Commands

| Command                                           | Description                                           |
| ------------------------------------------------- | ----------------------------------------------------- |
| [`sibyl synthesis plan`](#synthesis-plan)         | Plan source-grounded synthesis from authorized memory |
| [`sibyl synthesis draft`](#synthesis-draft)       | Draft a verified synthesis artifact                   |
| [`sibyl synthesis verify`](#synthesis-verify)     | Verify citation, freshness, redaction, gap coverage   |
| [`sibyl synthesis remember`](#synthesis-remember) | Draft, verify, and remember an artifact               |

## The Synthesis Pipeline

```
plan  ->  draft  ->  verify  ->  remember
 |          |          |           |
 sections   artifact   checks      stored artifact
```

`plan` resolves which sources are authorized and proposes a section outline. `draft` produces the
artifact text. `verify` runs the quality gates. `remember` chains draft, verify, and a `remember`
write so a passing artifact lands in memory in one step.

---

## synthesis plan

Plan source-grounded synthesis from authorized memory.

### Synopsis

```bash
sibyl synthesis plan <goal> [options]
```

### Arguments

| Argument | Required | Description    |
| -------- | -------- | -------------- |
| `goal`   | Yes      | Synthesis goal |

### Options

| Option            | Short | Default         | Description                                                |
| ----------------- | ----- | --------------- | ---------------------------------------------------------- |
| `--type`          |       | `documentation` | Output type                                                |
| `--audience`      |       | (none)          | Intended audience                                          |
| `--depth`         |       | `standard`      | `brief`, `standard`, or `deep`                             |
| `--seed`          |       | (none)          | Search seed query                                          |
| `--project`       | `-p`  | (auto)          | Project ID                                                 |
| `--all-projects`  |       | false           | Skip cwd project scope                                     |
| `--domain`        | `-d`  | (none)          | Domain/category                                            |
| `--entity`        |       | (none)          | Comma-separated entity IDs                                 |
| `--decision`      |       | (none)          | Comma-separated decision IDs                               |
| `--task`          |       | (none)          | Comma-separated task IDs                                   |
| `--artifact`      |       | (none)          | Comma-separated artifact IDs                               |
| `--section`       |       | (none)          | Pipe-separated `Title::Prompt::sources` specs              |
| `--constraint`    |       | (none)          | Comma-separated constraints                                |
| `--max-sections`  |       | 6               | Maximum sections (1-12)                                    |
| `--neighborhoods` |       | on              | Include one-hop graph neighborhoods (`--no-neighborhoods`) |
| `--json`          | `-j`  | false           | Output full JSON                                           |

### Examples

```bash
# Plan documentation from memory
sibyl synthesis plan "How Sibyl handles org isolation"

# Deep brief for a specific audience
sibyl synthesis plan "Synthesis verification model" \
  --depth deep --audience "new engineers"

# Seed retrieval and pin specific sources
sibyl synthesis plan "Auth hardening summary" \
  --seed "api key scopes" \
  --decision dec_abc123,dec_def456
```

---

## synthesis draft

Draft a verified synthesis artifact. Runs `plan` then produces the artifact body.

### Synopsis

```bash
sibyl synthesis draft <goal> [options]
```

### Options

`draft` accepts every [`synthesis plan`](#synthesis-plan) option plus:

| Option     | Default    | Description          |
| ---------- | ---------- | -------------------- |
| `--format` | `markdown` | `markdown` or `json` |

### Examples

```bash
# Draft a markdown artifact
sibyl synthesis draft "How Sibyl handles org isolation"

# Draft as structured JSON
sibyl synthesis draft "Synthesis verification model" --format json
```

---

## synthesis verify

Verify synthesis citation, freshness, redaction, and gap coverage. Use this to gate an artifact
before it is published or remembered.

### Synopsis

```bash
sibyl synthesis verify <goal> [options]
```

### Options

`verify` accepts the same options as [`synthesis plan`](#synthesis-plan).

### Verification Checks

| Check     | What it confirms                                   |
| --------- | -------------------------------------------------- |
| Citation  | Every claim is backed by a cited source            |
| Freshness | Cited sources are recent enough for the goal       |
| Redaction | No hidden-context or out-of-scope memory leaked in |
| Gap       | Planned sections are actually covered by the draft |

### Example

```bash
sibyl synthesis verify "How Sibyl handles org isolation" --json | jq '.checks'
```

---

## synthesis remember

Draft, verify, and remember a synthesis artifact in one step. The artifact is stored as an
`artifact` memory only when verification passes.

### Synopsis

```bash
sibyl synthesis remember <goal> [options]
```

### Options

`remember` accepts every [`synthesis draft`](#synthesis-draft) option plus:

| Option        | Default   | Description                   |
| ------------- | --------- | ----------------------------- |
| `--scope`     | `private` | Artifact memory scope         |
| `--scope-key` | (none)    | Artifact scope key            |
| `--tags`      | (none)    | Comma-separated artifact tags |

### Example

```bash
sibyl synthesis remember "Auth hardening summary" \
  --depth deep \
  --scope project --scope-key proj_abc123 \
  --tags "auth,summary"
```

## Related Commands

- [`sibyl remember`](./remember.md) - Capture durable memory
- [`sibyl context`](./context.md) - Recall memory into a context pack
- [Memory governance](./memory.md) - Audit and inspect memory sources
- [`sibyl context`](./context.md) - Semantic search across the graph
