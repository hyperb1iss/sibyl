---
title: Memory Workspace
description: The web surface for captures, imports, and synthesis
---

# Memory Workspace 🎭

The memory workspace is the web UI's home for the [memory loop](./memory-loop.md). It is where a
human reviews what agents captured, watches source imports land, and runs synthesis interactively.
The CLI drives the loop from the terminal; the workspace gives it oversight.

It lives at the protected `/memory` route. Earlier releases called this surface the cockpit; it is
now the workspace.

## What's in the Workspace

| Surface        | Route                  | Purpose                                         |
| -------------- | ---------------------- | ----------------------------------------------- |
| **Overview**   | `/memory`              | Activity feed of recent memory writes           |
| **Captures**   | `/memory/captures`     | Raw quick captures awaiting review or promotion |
| **Imports**    | `/memory/imports`      | Source import jobs and their progress           |
| **Sources**    | `/memory/sources/[id]` | A single import source and its records          |
| **Synthesize** | `/memory/synthesize`   | Interactive plan, draft, and verify             |

## Activity Feed

The overview shows a unified feed of memory activity across the org: what was remembered, what was
reflected, what was imported, and what synthesis ran. It is the fastest way to answer "what has the
team's memory been doing".

## Captures

Captures are raw, low-friction memories created with `sibyl capture` or the `remember` flow. The
captures surface lists them so a human can:

- See what agents captured without leaving the browser
- Promote a capture into durable typed memory
- Discard noise before it clutters the graph

This is the human half of the reflection workflow. The
[dream-cycle](./memory-loop.md#the-reflection-dream-cycle) handles automatic review; the captures
surface handles the judgment calls.

## Imports

The imports surface tracks [source import](./sources.md#source-import) jobs. Source import ingests
structured external records, such as a mailbox archive, into raw memory. Because imports are
resumable, the surface shows checkpoint progress and lets you see exactly which records landed.

## Synthesize

The synthesize surface runs [source-grounded synthesis](./synthesis.md) interactively. Set a goal,
pick scope, and run plan, draft, and verify with the verification report rendered inline. It is the
same engine as `sibyl synthesis`, surfaced for review instead of scripting.

## Memory Spaces

Memory is scoped. A memory has a scope (`private`, `team`, `shared`, and similar) and the workspace
respects it, so you see the memory you are authorized to see. Preview what an agent could recall
from a given set of spaces with:

```bash
sibyl memory-space preview-agent
```

This makes sharing boundaries explicit before you widen a scope.

## Access

The `/memory` route is protected. Reaching it requires an authenticated session with access to the
organization. Memory is org-isolated like every other Sibyl surface; see
[Multi-Tenancy](./multi-tenancy.md).

## Next Steps

- [The Memory Loop](./memory-loop.md) - The cycle the workspace supports
- [External Sources](./sources.md) - Crawling and source import
- [Synthesis](./synthesis.md) - The synthesize surface in depth
