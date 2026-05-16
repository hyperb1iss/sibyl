# MCP Tool: reflect

Reflect raw notes into reviewable durable memory candidates. Use `reflect` after planning,
ideation, debugging, or building sessions to extract structured memory from unstructured notes.

## Overview

Where [`remember`](./mcp-remember.md) captures a single piece of memory you already know you want
to keep, `reflect` takes a block of raw session notes and extracts multiple memory candidates from
it: decisions, plans, ideas, claims, artifacts, procedures, and session checkpoints. Each candidate
carries a confidence score and a reason.

By default `reflect` only extracts and returns candidates. Use the persistence flags to write them
back into Sibyl.

## Input Schema

```typescript
interface ReflectInput {
  // Required
  content: string; // Raw session notes to reflect on

  // Source
  source_title?: string; // Title for the reflection source (default "Session reflection")
  intent?: ContextIntent; // Goal mode (default "general")
  domain?: string; // Domain/category
  project?: string; // Project ID (sets project memory scope)

  // Linking
  related_to?: string[]; // Entity IDs to link
  task_ids?: string[]; // Task IDs for exact task context
  active_task?: boolean; // Link persisted output to the active doing task (default true)

  // Persistence
  persist?: boolean; // Write candidates back into Sibyl (default false)
  persist_source?: boolean; // Persist the raw reflection source (default true)
  persist_review?: boolean; // Store candidates in the raw review queue (default false)

  // Limits
  limit?: number; // Max candidates to extract (default 12)
}
```

### Intent Values

```
build, plan, ideate, research, debug, decide, learn, general
```

Intent guides which kinds of candidates the extractor emphasizes.

## Persistence Modes

The persistence flags control what happens to the extracted candidates:

| `persist` | `persist_review` | Outcome                                                          |
| --------- | ---------------- | ---------------------------------------------------------------- |
| `false`   | any              | Extract and return candidates only, no writes                    |
| `true`    | `false`          | Promote candidates into the knowledge graph                      |
| `true`    | `true`           | Store candidates in the raw review queue for later review        |

`persist_source` (default `true`) controls whether the raw reflection source itself is stored as a
provenance record. The review queue feeds the reflection dream-cycle, where candidates are reviewed
and promoted automatically or by an operator.

## Active Task Linking

With `persist=true` and a `project`, and `active_task=true` (default), `reflect` links persisted
output to the single task in `doing` status for that project when exactly one exists. Supply
`task_ids` for explicit linkage.

## Response Schema

```typescript
interface ReflectResponse {
  source_title: string;
  source_id: string | null; // Raw reflection source ID (when persisted)
  intent: string;
  domain: string | null;
  project: string | null;
  candidates: ReflectionCandidate[];
  total_candidates: number;
  persisted_count: number; // Candidates written back (0 unless persist=true)
  usage_hint: string;
  markdown: string; // Rendered Markdown view of the reflection
}

interface ReflectionCandidate {
  kind: string; // decision, plan, idea, claim, artifact, procedure, session
  title: string;
  content: string;
  reason: string; // Why this candidate was extracted
  confidence: number; // 0-1
  tags: string[];
  metadata: Record<string, any>;
  raw_source_ids: string[];
  suggested_memory_scope: string | null;
  suggested_scope_key: string | null;
  review_state: string; // pending, promoted, ...
  persisted_id: string | null; // Set when the candidate was written back
  claim_records: ClaimRecord[];
  reflection_findings: ReflectionFinding[];
  relationship_records: ReflectionRelationshipRecord[];
  sensitivity_flags: string[];
}
```

## Usage Examples

### Extract Candidates Without Persisting

```json
{
  "name": "reflect",
  "arguments": {
    "content": "Spent the session debugging the crawler. Found that imports stall when the source has more than 5000 documents. Decided to add resumable jobs. Also noticed the adapter contract should expose a progress callback.",
    "source_title": "Crawler debugging session",
    "intent": "debug",
    "project": "proj_abc123"
  }
}
```

The response returns candidates (a decision, a claim, an idea) with confidence scores. No writes
occur.

### Reflect and Promote into the Graph

```json
{
  "name": "reflect",
  "arguments": {
    "content": "Session notes: settled on Surreal-native storage, dropped the Postgres sidecar. Plan is to keep Postgres only for migration rehearsal.",
    "source_title": "Storage architecture session",
    "intent": "decide",
    "project": "proj_abc123",
    "persist": true
  }
}
```

`persisted_count` reflects how many candidates were written into the knowledge graph.

### Reflect into the Review Queue

```json
{
  "name": "reflect",
  "arguments": {
    "content": "Rough ideas from the planning call, not all confirmed yet.",
    "source_title": "Planning call notes",
    "intent": "plan",
    "project": "proj_abc123",
    "persist": true,
    "persist_review": true
  }
}
```

Candidates land in the raw review queue instead of being promoted directly. They are later reviewed
through the reflection dream-cycle.

## Notes

- `reflect` without `persist` is read-only and safe to call freely.
- When `persist=true` and the credential is project-scoped, a `project` is required.
- Every call is authorized against the memory policy and audited.
- The `markdown` field is a rendered view of the reflection, convenient for prompt injection.

## Error Handling

| Error                                                | Cause                                | Resolution                            |
| ----------------------------------------------------- | ------------------------------------ | ------------------------------------- |
| `Organization context required`                        | No org-scoped token                  | Authenticate with an org-scoped token |
| `Project is required when MCP credentials are project-scoped` | Project-scoped key, `persist=true`, no `project` | Supply a `project`        |
| `api_key_memory_space_denied`                           | API key lacks the target memory scope | Grant the key the required memory scope |

## Related

- [mcp-remember.md](./mcp-remember.md) - Capture a single durable memory directly
- [mcp-context.md](./mcp-context.md) - Retrieve durable memory for a goal
- [rest-memory.md](./rest-memory.md) - REST reflection endpoints (`/api/context/reflect`)
