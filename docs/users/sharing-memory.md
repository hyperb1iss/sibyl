---
title: Sharing Memory
description: Choosing personal, project, and shared memory scopes
---

# Sharing Memory

MemorySpaces are Sibyl's policy boundary for recall. They decide who can read a memory before that
memory reaches search, context packs, MCP, or an agent.

## Scope Types

| Scope          | Use it for                                                     | Visibility                                             |
| -------------- | -------------------------------------------------------------- | ------------------------------------------------------ |
| `private`      | Personal notes, unfinished thinking, agent diary entries       | Only the owning user or explicitly authorized actor    |
| `delegated`    | A named agent acting for a user                                | The delegated agent under the configured authority     |
| `project`      | Work tied to one project                                       | Members with access to that project                    |
| `team`         | Knowledge a team shares across its projects                    | Members of that team; the scope key is the team's UUID |
| `organization` | Future org-wide memory                                         | Disabled until explicit policy ships                   |
| `shared`       | Retired; see [Legacy shared captures](#legacy-shared-captures) | Nobody; new writes are refused                         |
| `public`       | Future public memory                                           | Disabled until explicit policy ships                   |

Private, delegated, project, and team scopes are the day-to-day production scopes. Organization and
public scopes stay reserved until their review and promotion workflows are fully explicit.

## Capture Into A Scope

Use private scope for personal recall:

```bash
sibyl remember "Restore org check" \
  "sibyld migrate import refuses a backup whose organization differs from --org-id" \
  --scope private
```

Use project scope for knowledge the project team should recall:

```bash
sibyl remember "Surreal restore drill" \
  "The weekly drill imports the latest export and checks fixture counts" \
  --scope project \
  --scope-key proj_abc123
```

Use team scope for knowledge that belongs to a team rather than one project. Team scope keys are
team UUIDs; `sibyl team list` shows each team's key in its Scope column:

```bash
sibyl team list
sibyl remember "On-call escalation" \
  "Page the platform rotation first and escalate after fifteen minutes" \
  --scope team \
  --scope-key 0f8b6c52-4d2e-4a7a-9c1d-6e5b2a9f3d10
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

## Legacy Shared Captures

Since 1.4.3, Sibyl refuses new writes in `shared` scope, and recall does not return shared captures.
Captures that earlier versions stored in `shared` scope stay in the database, but no one can read
them until an operator retires them. The `retire-shared` migration moves each one into the team its
scope key names, provided the key is a team UUID in the same organization and the capture's author
is still a member of that team or an organization admin. Captures without that proof stay as
tombstones, with their IDs, raw bytes and provenance intact.

The command previews by default and prints a JSON receipt with the decision for every capture:

```bash
sibyld migrate retire-shared --org-id <org-uuid>
sibyld migrate retire-shared --org-id <org-uuid> --apply
```

Run it once per organization that has legacy shared captures. It exits non-zero when the receipt
records an error or a revision conflict with a concurrent write. Conflicted captures stay untouched,
and a rerun picks up only the shared captures that remain.
