# MCP Tool: context

Compile a precise context pack for an agent goal. Context packs are structured for action, not for
generic search browsing.

## Overview

The `context` tool retrieves the memory that matters for a specific goal and groups it into facets
such as active work, decisions, plans, ideas, constraints, artifacts, procedures, gotchas, and
recent sessions. Use it before dispatching or resuming an agent so the agent starts with the
working set it needs.

The `context` tool is the retrieval companion to [`remember`](./mcp-remember.md): `remember` stores
what future agents should not have to relearn, and `context` pulls that material back into a goal.

## Input Schema

```typescript
interface ContextInput {
  // Required
  goal: string; // What the agent is trying to accomplish

  // Retrieval Shaping
  intent?: ContextIntent; // Goal mode (default "build")
  layer?: "wake" | "recall" | "deep_search"; // Retrieval depth (default "recall")
  domain?: string; // Domain/category to scope context
  project?: string; // Project ID to scope active work
  agent_id?: string; // Agent diary identity to include

  // Limits
  limit?: number; // Max total context items, clamped 1-50 (default 24)
  include_related?: boolean; // Include one-hop related graph context (default true)
  related_limit?: number; // Related items per context item (default 3)
}
```

### Intent Values

| Intent     | Use For                                            |
| ---------- | -------------------------------------------------- |
| `build`    | Implementation work (default)                      |
| `plan`     | Sequencing, milestones, roadmap work               |
| `ideate`   | Brainstorming and option generation                |
| `research` | Investigation and landscape analysis               |
| `debug`    | Diagnosing failures                                |
| `decide`   | Resolving a choice                                 |
| `learn`    | Building understanding of a topic                  |
| `general`  | Unscoped retrieval                                 |

Intent shapes which facets are emphasized in the resulting pack.

### Retrieval Layers

| Layer         | Behavior                                                  |
| ------------- | --------------------------------------------------------- |
| `wake`        | Compact retrieval for session start                       |
| `recall`      | Working context for an active goal (default)              |
| `deep_search` | Broad retrieval for research-grade context                |

## Response Schema

```typescript
interface ContextPackResponse {
  goal: string;
  intent: string;
  query: string; // Derived retrieval query
  domain: string | null;
  project: string | null;
  layer: string;
  sections: ContextSection[];
  total_items: number;
  usage_hint: string;
  markdown: string; // Rendered Markdown view of the pack
}

interface ContextSection {
  facet: string; // active_work, decisions, planning, ideation, ...
  title: string;
  items: ContextItem[];
}

interface ContextItem {
  id: string;
  type: string;
  name: string;
  content: string;
  score: number;
  facet: string;
  reason: string; // Why this item was selected
  source: string | null;
  quality: {
    origin: string | null;
    source: string | null;
    url: string | null;
    created_at: string | null;
    updated_at: string | null;
    valid_at: string | null;
    project_id: string | null;
  };
  metadata: Record<string, any>;
  related: ContextRelatedItem[];
}

interface ContextRelatedItem {
  id: string;
  type: string;
  name: string;
  relationship: string;
  direction: "outgoing" | "incoming";
  distance: number;
}
```

### Context Facets

Sections are keyed by facet:

```
active_work, artifacts, constraints, decisions, domain, gotchas,
ideation, planning, procedures, recent_memory, verification
```

## Usage Examples

### Compile Context for a Build Goal

```json
{
  "name": "context",
  "arguments": {
    "goal": "Add refresh-token rotation to the auth service",
    "intent": "build",
    "project": "proj_abc123"
  }
}
```

### Compact Session Start

```json
{
  "name": "context",
  "arguments": {
    "goal": "Resume work on the crawler import adapter",
    "layer": "wake",
    "project": "proj_abc123",
    "limit": 12
  }
}
```

### Deep Research Context

```json
{
  "name": "context",
  "arguments": {
    "goal": "Evaluate vector index options for document search",
    "intent": "research",
    "layer": "deep_search",
    "domain": "retrieval",
    "limit": 40
  }
}
```

### Include an Agent Diary

```json
{
  "name": "context",
  "arguments": {
    "goal": "Continue the migration rehearsal",
    "intent": "plan",
    "project": "proj_abc123",
    "agent_id": "migration-runner"
  }
}
```

When `agent_id` is set, the pack includes that agent's diary memory alongside normal private and
project raw memory.

## Workflow Patterns

### Dispatch an Agent with Context

```
1. context(goal="<task goal>", project="<project_id>")  --> Compile the pack
2. Hand the pack (or its markdown field) to the agent
3. As the agent works, capture new memory with remember(...)
```

### Context Then Synthesis

```
1. context(goal="...", layer="deep_search")  --> Gather working context
2. synthesis_plan(goal="...")                --> Plan a source-grounded artifact
```

## Notes

- `limit` is clamped to the range 1-50 regardless of the value supplied.
- Project scope is enforced against the caller's accessible projects. Requesting a project the
  credential cannot access raises a project-access error.
- The `markdown` field is a rendered view of the same pack, convenient for direct prompt injection.
- Context pack compilation is audited per call.

## Error Handling

| Error                              | Cause                                  | Resolution                          |
| ----------------------------------- | -------------------------------------- | ----------------------------------- |
| `Organization context required`     | No org-scoped token                    | Authenticate with an org-scoped token |
| `Project access denied: <id>`        | Caller cannot access the project       | Use an accessible project ID        |

## Related

- [mcp-remember.md](./mcp-remember.md) - Capture durable memory for later context
- [mcp-reflect.md](./mcp-reflect.md) - Reflect raw notes into memory candidates
- [mcp-search.md](./mcp-search.md) - Unstructured semantic search
- [rest-memory.md](./rest-memory.md) - REST context pack endpoint (`/api/context/pack`)
