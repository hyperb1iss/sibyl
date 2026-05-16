---
title: Source-Grounded Synthesis
description: Draft verified documents from your own remembered memory
---

# Source-Grounded Synthesis 🧪

Synthesis turns the memory you have already captured into a finished artifact:
documentation, a decision record, a runbook, a summary. Unlike asking a model to
write from nothing, every synthesis artifact is grounded in your authorized memory
and carries citations, freshness checks, and gap coverage.

The point is trust. A synthesized document is only as good as the memory behind it,
so Sibyl makes that link explicit and checkable.

## The Three Stages

Synthesis runs in three stages, each available as a CLI command and an MCP tool.

| Stage      | CLI                     | MCP tool           | What it produces                          |
| ---------- | ----------------------- | ------------------ | ------------------------------------------ |
| **Plan**   | `sibyl synthesis plan`  | `synthesis_plan`   | A section outline grounded in memory       |
| **Draft**  | `sibyl synthesis draft` | `synthesis_draft`  | A drafted, verified artifact               |
| **Verify** | `sibyl synthesis verify`| `synthesis_verify` | Citation, freshness, redaction, gap report |

`sibyl synthesis remember` runs draft, verify, and remember in one step, persisting
the verified artifact back into memory.

## Plan

Planning compiles the relevant memory and proposes a section outline before any prose
is written. It is the cheap, fast way to see what the synthesis will be built from.

```bash
sibyl synthesis plan "How our auth system handles token refresh"

# Aim it at a specific audience and depth
sibyl synthesis plan "Onboarding guide for the payments service" \
  --audience "new engineers" \
  --depth deep
```

The plan shows which entities, decisions, tasks, and artifacts were pulled in. If the
plan looks thin, capture more memory before drafting.

## Draft

Drafting writes the artifact and verifies it in the same pass.

```bash
sibyl synthesis draft "How our auth system handles token refresh"

# JSON output for tooling
sibyl synthesis draft "Release runbook for sibyld" --format json
```

Useful flags across the synthesis commands:

| Flag             | Purpose                                                     |
| ---------------- | ----------------------------------------------------------- |
| `--type`         | Output type (default `documentation`)                       |
| `--audience`     | Intended reader                                             |
| `--depth`        | `brief`, `standard`, or `deep`                              |
| `--seed`         | Search seed query to focus memory retrieval                 |
| `--project`      | Scope source memory to a project                            |
| `--entity`       | Comma-separated entity IDs to ground on explicitly          |
| `--decision`     | Comma-separated decision IDs to ground on                   |
| `--section`      | Pipe-separated `Title::Prompt::spec` section overrides      |
| `--max-sections` | Section cap (1-12, default 6)                               |

## Verify

Verification is the trust gate. It checks the drafted artifact against the memory it
claims to be built from.

```bash
sibyl synthesis verify "How our auth system handles token refresh"
```

Verification covers four things:

- **Citation:** every claim traces to a real source memory.
- **Freshness:** cited memory is recent enough to still be reliable.
- **Redaction:** hidden-context and private material did not leak into the artifact.
- **Gap coverage:** the artifact does not silently skip parts of the goal.

A failing verification means the draft is not safe to ship as-is. Capture missing
memory or narrow the goal, then re-run.

## Remember

When a verified artifact is worth keeping, `synthesis remember` drafts, verifies, and
persists it as a memory in one command.

```bash
sibyl synthesis remember "Auth token refresh design" \
  --tags auth,design \
  --scope team --scope-key proj_auth
```

The artifact becomes a first-class memory. Later recall and further synthesis can
cite it, so synthesis output compounds like any other captured knowledge.

## Synthesis in the Web UI

The [memory workspace](./memory-workspace.md) includes a synthesize surface at
`/memory/synthesize` for running the same plan, draft, and verify stages
interactively, with the verification report rendered for review.

## Next Steps

- [The Memory Loop](./memory-loop.md) - Where synthesis fits in the cycle
- [Capturing Knowledge](./capturing-knowledge.md) - Better memory means better synthesis
- [Memory Workspace](./memory-workspace.md) - The web synthesize surface
