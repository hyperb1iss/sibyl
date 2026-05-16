---
title: The Memory Loop
description: Recall, act, remember, reflect - the cycle Sibyl is built around
---

# The Memory Loop 🔮

Sibyl is built around a durable cycle that both humans and AI agents follow:
**recall, act, remember, reflect.** Every interface (CLI, MCP tools, hooks, and the
web workspace) exists to support this loop. Learn it once and the rest of Sibyl falls
into place.

```
┌─────────────────────────────────────────────────────────────┐
│  1. RECALL                                                  │
│     Pull working context before you act.                    │
│     sibyl recall "what you're working on"                   │
└─────────────────────────┬───────────────────────────────────┘
                          ▼
┌─────────────────────────────────────────────────────────────┐
│  2. ACT                                                     │
│     Do the work with context in hand.                       │
│     sibyl task start <task_id>                              │
└─────────────────────────┬───────────────────────────────────┘
                          ▼
┌─────────────────────────────────────────────────────────────┐
│  3. REMEMBER                                                │
│     Capture decisions, learnings, and durable knowledge.     │
│     sibyl remember "Title" "What, why, how, caveats"         │
└─────────────────────────┬───────────────────────────────────┘
                          ▼
┌─────────────────────────────────────────────────────────────┐
│  4. REFLECT                                                 │
│     Distill raw notes into reviewable memory candidates.     │
│     sibyl reflect --persist --review                         │
└─────────────────────────────────────────────────────────────┘
```

Every completed loop makes the graph smarter. The next recall is sharper because the
last remember and reflect fed it.

## Recall

`sibyl recall` compiles a compact working context pack for a goal. It fuses semantic
search, raw memory, and one-hop graph context into a result sized for an agent's
prompt budget.

```bash
# Recall context for a goal
sibyl recall "implement OAuth refresh"

# Tell Sibyl what kind of work this is
sibyl recall "design the rate limiter" --intent plan

# Control how deep retrieval goes
sibyl recall "why is login flaky" --intent debug --layer deep_search
```

Key flags:

| Flag        | Purpose                                                            |
| ----------- | ------------------------------------------------------------------ |
| `--intent`  | `build`, `plan`, `ideate`, `research`, `review`, `debug`, `decide`, `learn`, `general` |
| `--layer`   | Context depth: `wake`, `recall`, `deep_search`                     |
| `--project` | Scope recall to a project                                          |
| `--limit`   | Maximum context items (1-50, default 12)                           |
| `--raw`     | Recall verbatim raw memories instead of synthesized context        |
| `--diary`   | Recall a private agent diary                                       |
| `--json`    | Full structured output                                             |

The `--layer` flag trades latency for depth. `wake` is a fast session-start pull,
`recall` is the everyday default, and `deep_search` runs a wider, slower scan for
hard questions.

## Act

The middle of the loop is the work itself. Sibyl tracks it through tasks so progress
survives the session:

```bash
sibyl task start <task_id>
sibyl task note <task_id> "Found the root cause in the token service"
```

See [Task Management](./task-management.md) for the full lifecycle.

## Remember

`sibyl remember` captures durable memory. Unlike a quick note, a remembered entry is
typed and source-grounded, so later recall and synthesis can cite it.

```bash
# Remember a learning (default kind: episode)
sibyl remember "Redis pool sizing" "Pool size must be >= concurrent requests"

# Remember a decision
sibyl remember "Chose SurrealDB" "One engine replaces three backends" --kind decision

# Remember a plan or an idea
sibyl remember "Q3 reflection rollout" "Phased per-org enablement" --kind plan
```

Useful kinds for the loop are `decision`, `plan`, `idea`, `claim`, `episode`, and
`session`. See [Entity Types](./entity-types.md) for the full set.

For a fast capture without separate title and content, use `sibyl capture`:

```bash
sibyl capture "Surreal embedded mode is single-writer, fine for local dev"
```

`capture` derives a title from the content and stores the entry as raw memory. It is
the lowest-friction way to get a thought into the graph mid-task.

## Reflect

`sibyl reflect` turns a pile of raw notes into structured memory candidates. Instead
of remembering one thing at a time, you hand Sibyl a session's worth of notes and it
extracts the durable pieces.

```bash
# Reflect notes piped from a file or scratchpad
cat session-notes.md | sibyl reflect

# Reflect and persist the candidates into the graph
sibyl reflect --persist < session-notes.md

# Persist into the review queue instead of promoting directly
sibyl reflect --persist --review < session-notes.md
```

`--review` routes the extracted candidates to a queue for inspection before they land
as graph entities. Without `--review`, `--persist` promotes them directly.

## The Reflection Dream-Cycle 🌙

Reflection also runs on its own. The dream-cycle is an org-scoped maintenance job
that drains pending reflection candidates through automatic review, applies lifecycle
findings, and records inspectable run receipts.

```bash
# Drain pending candidates through automatic review now
sibyl memory-review drain

# Queue the nightly dream-cycle maintenance job
sibyl memory-review dream

# Inspect dream-cycle runs and their decision receipts
sibyl memory-review status
```

The dream-cycle keeps the graph from accumulating unreviewed noise. Candidates that
pass review become durable memory; the rest are surfaced for a human or owner agent
to triage. Run receipts make every automatic decision auditable.

## Inspecting Memory

Memory is auditable end to end. These commands trace where a memory came from and
how it was reviewed:

```bash
sibyl memory-audit              # Inspect memory audit receipts
sibyl memory-inspect <source>   # Inspect a memory source and its audit trail
sibyl memory-promote            # Preview or auto-review candidate promotion
sibyl archive                   # Browse archived raw quick captures
```

## Offline Writes

When the server is unreachable, loop writes are buffered locally instead of lost. The
buffer is encrypted and keyed for idempotent replay:

```bash
sibyl pending-writes list       # List buffered writes (payload bodies hidden)
sibyl pending-writes flush      # Replay buffered writes when back online
sibyl pending-writes discard    # Drop buffered writes without replaying
```

## The Loop in Other Surfaces

The same loop runs everywhere:

- **MCP tools:** `context` recalls, `remember` captures, `reflect` distills. See
  [Claude Code Integration](./claude-code.md).
- **Hooks:** the SessionStart hook performs a wake-layer recall automatically. See
  [Skills & Hooks](./skills.md).
- **Web UI:** the [memory workspace](./memory-workspace.md) shows captures, imports,
  and synthesis for human oversight.

## Next Steps

- [Capturing Knowledge](./capturing-knowledge.md) - Quality patterns for remember
- [Memory Workspace](./memory-workspace.md) - The web surface for the loop
- [Synthesis](./synthesis.md) - Draft verified documents from remembered memory
- [Task Management](./task-management.md) - The act phase in detail
