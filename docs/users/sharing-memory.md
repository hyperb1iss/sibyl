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
public scopes stay reserved until their review and promotion workflows are fully explicit. The CLI
refuses `--scope org` (or `organization`) before sending anything; use `project` or `team` instead.

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

## Project Default Scope

A capture that names no scope lands in its project's default write scope. Every project starts at
`private`, so nothing is shared until someone decides it should be. A project owner, a maintainer,
or an organization admin can share a project once instead of every agent passing `--scope project`:

```bash
sibyl project update proj_oncall --default-scope project
sibyl project show proj_oncall    # prints the current default
```

The web app has the same switch under **Memory sharing** on the project page. With the project
shared, a contributor's unscoped capture lands in project scope. A viewer's capture still lands
private, because only contributors may write project memory. The receipt reports where each capture
landed and why, and an explicit `--scope` always wins.

The default governs memories and reflections. Task learnings recorded with
`sibyl task complete --learnings` belong to the task, so they follow the task's project like the
task itself does.

MCP tools take no scope argument, so an MCP agent shares only through this setting. Before this
setting existed, MCP captures in a project always landed in project scope; they now land private
until the project is shared.

Projects default to organization visibility, so every organization member can read a project and its
project-scoped memory. Shared project memory therefore reaches the whole organization unless the
project is private and granted to specific people or teams.

Reads follow the working project. In a linked repo, `sibyl context` and `sibyl search` return your
private memories, that project's shared memories, and your teams' memories. Another project's
memories appear only when you ask for them with `--project <id>` or `--all`.

## Choosing A Scope

Use private memory when the note is about your workflow, credentials, local machine, or unfinished
reasoning. Use project memory when the note would help a teammate or a future agent working in that
project.

Do not use project memory as a dumping ground for everything. The best shared memory is durable:
decisions, gotchas, runbooks, domain language, interfaces, and tested failure modes.

## Time-Boxed Team Notices

Some team memory is state, not knowledge: an active incident, a deploy freeze, a migration window.
The team needs it while it is true, and nobody needs it once it is over. Give that kind of capture
an expiry:

```bash
sibyl remember "Checkout outage" \
  "Checkout is returning 502s; hold all deploys until the on-call agent clears it" \
  --scope team \
  --scope-key 0f8b6c52-4d2e-4a7a-9c1d-6e5b2a9f3d10 \
  --expires 2h
```

`--expires` takes a duration such as `30m`, `2h`, `1d` or `1h30m`, between one minute and 30 days. A
time-boxed capture is stored verbatim, like `--raw`. Until it expires, it surfaces in team members'
context packs when it is relevant to their goal, whatever repo they work in, and it carries an
`expires=` marker so an agent can tell a live notice from history. A notice reaches a pack through
recall, so write its title and body in the words an agent would search by. A scheduled sweep retires
it within a minute of the deadline by applying the same `stale` correction an operator would, with
the reason `expired`, so it drops out of every pack, search and recall lane.

Resolve a notice early with a correction. The sweep sees it is already retired and leaves it alone:

```bash
sibyl correct <raw-memory-id> --action stale --reason resolved
```

Once the sweep has expired a notice, `sibyl correct <raw-memory-id> --action restore` brings it back
for good: the sweep has already handled it, so it does not expire again. Restoring a notice that was
resolved early, before its deadline, leaves its expiry in place. Never use an expiry for durable
knowledge. Write the lesson as a normal memory once the incident is over.

## Promotion

When a private note becomes useful to the team, rewrite it as a clean project memory instead of
blindly copying raw notes. Good promotion answers:

- What was learned?
- Why does it matter later?
- What command, file, or setting proves it?
- What should a future agent do differently?

To nominate a private memory for a team without sharing it yet, propose the scope and name the team.
The capture stays where you wrote it until the audited promotion pipeline accepts it, and promotion
checks team membership again at that point:

```bash
sibyl remember "Review ritual" "Verifier passes are mandatory." --kind rule \
  --propose-scope team \
  --propose-scope-key 0f8b6c52-4d2e-4a7a-9c1d-6e5b2a9f3d10
```

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
