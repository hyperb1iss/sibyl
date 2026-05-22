---
title: Sharing Memory
description: Choosing personal, project, and shared memory scopes
---

# Sharing Memory

MemorySpaces are Sibyl's policy boundary for recall. They decide who can read a memory before that
memory reaches search, context packs, MCP, or an agent.

## Scope Types

| Scope          | Use it for                                               | Visibility                                          |
| -------------- | -------------------------------------------------------- | --------------------------------------------------- |
| `private`      | Personal notes, unfinished thinking, agent diary entries | Only the owning user or explicitly authorized actor |
| `delegated`    | A named agent acting for a user                          | The delegated agent under the configured authority  |
| `project`      | Work tied to one project                                 | Members with access to that project                 |
| `team`         | Future team spaces                                       | Disabled until explicit policy ships                |
| `organization` | Future org-wide memory                                   | Disabled until explicit policy ships                |
| `shared`       | Future deliberate sharing flows                          | Disabled until explicit policy ships                |
| `public`       | Future public memory                                     | Disabled until explicit policy ships                |

Private, delegated, and project scopes are the day-to-day production scopes. The broader scopes are
reserved until their review and promotion workflows are fully explicit.

## Capture Into A Scope

Use private scope for personal recall:

```bash
sibyl remember "Postgres archive gotcha" \
  "The restore rehearsal expects a retained postgres.sql payload" \
  --scope private
```

Use project scope for knowledge the project team should recall:

```bash
sibyl remember "Surreal restore drill" \
  "The weekly drill imports the latest export and checks fixture counts" \
  --scope project \
  --scope-key proj_abc123
```

API keys can also be limited to project IDs and memory-space IDs so MCP clients only recall the
memory they should see.

## Choosing A Scope

Use private memory when the note is about your workflow, credentials, local machine, or unfinished
reasoning. Use project memory when the note would help a teammate or a future agent working in that
project.

Do not use project memory as a dumping ground for everything. The best shared memory is durable:
decisions, gotchas, runbooks, domain language, interfaces, and tested failure modes.

## Promotion

When a private note becomes useful to the team, rewrite it as a clean project memory instead of
blindly copying raw notes. Good promotion answers:

- What was learned?
- Why does it matter later?
- What command, file, or setting proves it?
- What should a future agent do differently?

Sibyl records policy and audit metadata around memory operations so recall can stay useful without
leaking private project or user content.
