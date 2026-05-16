# MCP Tool: remember

Capture durable context from planning, ideation, building, or any domain. `remember` stores
verbatim raw memory as provenance and creates a graph entity in one call.

## Overview

Use `remember` aggressively during agent work to capture decisions, plans, ideas, claims,
procedures, artifacts, sessions, and domain facts. It is the capture companion to
[`context`](./mcp-context.md): `context` retrieves what matters, `remember` stores what future
agents should not have to relearn.

Each call does two things:

1. Stores the content verbatim as a raw memory record (provenance, scoped, audited).
2. Creates a corresponding graph entity of the requested `kind` so the memory is searchable.

Compared to [`add`](./mcp-add.md), `remember` additionally writes the raw memory record, applies a
memory scope, and can auto-link to the active task.

## Input Schema

```typescript
interface RememberInput {
  // Required
  title: string; // Short title
  content: string; // Full content to remember verbatim

  // Memory Configuration
  kind?: MemoryKind; // Memory kind (default "episode")
  domain?: string; // Domain/category
  project?: string; // Project ID (sets project memory scope)
  tags?: string[]; // Searchable tags

  // Linking
  related_to?: string[]; // Entity IDs to link
  task_ids?: string[]; // Task IDs for exact task context
  active_task?: boolean; // Link to the single active doing task (default true)

  // Metadata
  metadata?: Record<string, any>; // Additional structured metadata
}
```

### Memory Kinds

```
episode, decision, plan, idea, claim, artifact, procedure, domain, session, pattern, rule
```

| Kind        | Capture For                                       |
| ----------- | ------------------------------------------------- |
| `episode`   | Temporal knowledge, learnings (default)           |
| `decision`  | A chosen direction with rationale                 |
| `plan`      | Strategy, sequencing, milestones                  |
| `idea`      | A brainstormed concept or unresolved option       |
| `claim`     | An atomic fact or assertion                       |
| `artifact`  | A file, object, document, asset, or work product  |
| `procedure` | A repeatable workflow or runbook                  |
| `domain`    | A modeled problem space                           |
| `session`   | A conversation or work-session checkpoint         |
| `pattern`   | A reusable pattern or best practice               |
| `rule`      | A rule or guideline                               |

## Memory Scope

The scope is derived from `project`:

- With a `project`, the memory is scoped to that project (`project` scope).
- Without a `project`, the memory is private to the calling user (`private` scope).

Project-scoped writes require access to the project. Project-scoped credentials (an API key bound
to specific projects) must supply a `project`.

## Active Task Linking

When `active_task` is `true` (default) and a `project` is supplied, `remember` looks up the single
task in `doing` status for that project. If exactly one exists, the new memory is linked to it.
This keeps captures attached to the work in progress without the caller tracking task IDs.

Supply `task_ids` for explicit task linkage regardless of the active-task lookup.

## Response Schema

```typescript
interface RememberResponse {
  success: boolean;
  id: string | null; // Created graph entity ID
  message: string;
  timestamp: string;
  conflicts: ConflictWarning[];
  raw_memory_id: string; // Raw memory record ID
  raw_source_id: string; // Raw memory source ID (provenance key)
  policy_reason: string; // Memory policy decision reason
}
```

## Usage Examples

### Remember a Decision

```json
{
  "name": "remember",
  "arguments": {
    "title": "Use PKCE for the SPA OAuth flow",
    "content": "Decided to use the Authorization Code flow with PKCE for the single-page app. Implicit flow is deprecated and exposes tokens in the URL fragment. PKCE works without a client secret.",
    "kind": "decision",
    "project": "proj_abc123",
    "domain": "authentication",
    "tags": ["oauth", "security"]
  }
}
```

### Remember a Plan

```json
{
  "name": "remember",
  "arguments": {
    "title": "Crawler import adapter rollout",
    "content": "Step 1: ship the mbox adapter behind a flag. Step 2: add resumable import jobs. Step 3: expose the adapter contract for third-party adapters.",
    "kind": "plan",
    "project": "proj_abc123"
  }
}
```

### Remember a Claim with Explicit Task Context

```json
{
  "name": "remember",
  "arguments": {
    "title": "SurrealDB driver serializes websocket queries",
    "content": "The SurrealDB driver serializes all websocket queries through a per-client asyncio lock. A single driver instance cannot be safely shared across orgs; use driver.clone(group_id).",
    "kind": "claim",
    "project": "proj_abc123",
    "task_ids": ["task_xyz789"]
  }
}
```

### Private Memory (No Project)

```json
{
  "name": "remember",
  "arguments": {
    "title": "Personal note on the rebase workflow",
    "content": "Rebasing the docs branch onto main was clean after stashing the contexts directory.",
    "kind": "episode"
  }
}
```

Without a `project`, the memory is private to the calling user.

## Notes

- The raw memory record is stored verbatim before the graph entity is created. The
  `raw_source_id` is the stable provenance key for that record.
- `remember` requires a user context. API keys without a resolvable user cannot capture raw
  source material.
- Every call is authorized against the memory policy and audited.

## Error Handling

| Error                                                | Cause                                       | Resolution                            |
| ----------------------------------------------------- | ------------------------------------------- | ------------------------------------- |
| `Organization context required`                        | No org-scoped token                         | Authenticate with an org-scoped token |
| `User context required to remember raw source material` | Credential has no resolvable user           | Use a user-bound token or API key     |
| `Project is required when MCP credentials are project-scoped` | Project-scoped key with no `project`        | Supply a `project`                    |
| `api_key_memory_space_denied`                           | API key lacks the memory scope being written | Grant the key the required memory scope |

## Related

- [mcp-reflect.md](./mcp-reflect.md) - Reflect raw notes into reviewable candidates
- [mcp-context.md](./mcp-context.md) - Retrieve durable memory for a goal
- [mcp-add.md](./mcp-add.md) - Create a graph entity without raw provenance
- [rest-memory.md](./rest-memory.md) - REST memory endpoints (`/api/memory/raw`)
