# MCP Tools: synthesis

Source-grounded synthesis from authorized memory. Three tools cover the synthesis lifecycle:
`synthesis_plan`, `synthesis_draft`, and `synthesis_verify`.

## Overview

Synthesis turns the knowledge graph into a structured artifact (documentation, report, briefing,
roadmap, release notes, audit packet) where every section is grounded in cited sources. The three
tools share the same request shape and differ only in what they produce:

| Tool               | Produces                                                       |
| ------------------ | -------------------------------------------------------------- |
| `synthesis_plan`   | A deterministic outline with materialized source packs         |
| `synthesis_draft`  | A drafted artifact plus verification, optionally remembered    |
| `synthesis_verify` | The same run as `plan`, with verification applied, no artifact |

A typical flow is `synthesis_plan` to inspect coverage, then `synthesis_draft` to produce the
artifact. `synthesis_verify` is useful as a standalone coverage check.

## Shared Request Schema

All three tools accept the same core parameters:

```typescript
interface SynthesisRequest {
  // Required
  goal: string; // What the synthesis should accomplish

  // Output Shaping
  output_type?: SynthesisOutputType; // default "documentation"
  audience?: string; // Intended reader
  depth?: "brief" | "standard" | "deep"; // default "standard"

  // Source Selection
  seed_query?: string; // Explicit retrieval query (defaults from goal)
  project?: string; // Project ID to scope sources
  domain?: string; // Domain/category to scope sources
  entity_ids?: string[]; // Pin specific entities as sources
  decision_ids?: string[]; // Pin specific decisions
  task_ids?: string[]; // Pin specific tasks
  artifact_ids?: string[]; // Pin specific artifacts

  // Outline Control
  required_sections?: (string | SectionSpec)[]; // Force specific sections
  constraints?: string[]; // Free-text constraints on the output
  max_sections?: number; // Outline section cap (default 6)
  include_neighborhoods?: boolean; // Include one-hop related sources (default true)
}

interface SectionSpec {
  title: string;
  prompt?: string;
  required_source_ids?: string[];
}
```

A `required_sections` entry can be a `SectionSpec` object or a string. A string is split on `::`
into a title and an optional prompt, for example `"Background::Summarize the prior approach"`.

### Output Types

```
documentation, report, briefing, roadmap, release_notes, audit_packet, custom
```

### Depth Values

| Depth      | Behavior                                  |
| ---------- | ----------------------------------------- |
| `brief`    | Tight outline, fewer sources per section  |
| `standard` | Balanced coverage (default)               |
| `deep`     | Broad retrieval, more sources per section |

## Tool: synthesis_plan

Plans a source-grounded outline and materializes the source pack for each section. No artifact text
is generated. Use it to inspect which sources back each section and where coverage is thin.

```json
{
  "name": "synthesis_plan",
  "arguments": {
    "goal": "Document the Sibyl memory loop for new contributors",
    "output_type": "documentation",
    "audience": "engineers new to the codebase",
    "project": "proj_abc123",
    "max_sections": 5
  }
}
```

## Tool: synthesis_draft

Plans, materializes, drafts the artifact text, and applies verification. Optionally remembers the
artifact as a memory record.

In addition to the shared request schema, `synthesis_draft` accepts:

```typescript
interface SynthesisDraftExtra {
  output_format?: "markdown" | "json"; // Artifact format (default "markdown")
  remember?: boolean; // Persist the artifact as memory (default false)
  memory_scope?: string; // "private" or "project" (default "private")
  scope_key?: string; // Project ID when memory_scope is "project"
  tags?: string[]; // Tags for the remembered artifact
}
```

```json
{
  "name": "synthesis_draft",
  "arguments": {
    "goal": "Draft release notes for v0.9.0",
    "output_type": "release_notes",
    "project": "proj_abc123",
    "output_format": "markdown",
    "remember": true,
    "memory_scope": "project",
    "scope_key": "proj_abc123",
    "tags": ["release", "v0.9.0"]
  }
}
```

When `remember` is `true` and `memory_scope` is `project`, `scope_key` defaults to `project` and the
write is authorized against the memory policy. The remembered artifact's IDs appear on the returned
artifact (`remembered_memory_id`, `remembered_source_id`).

## Tool: synthesis_verify

Runs the same plan-and-materialize step as `synthesis_plan`, then applies verification. It returns
the run with verification results but no artifact. Use it to check citation, freshness,
hidden-context, and gap coverage without drafting.

```json
{
  "name": "synthesis_verify",
  "arguments": {
    "goal": "Audit packet for the auth subsystem",
    "output_type": "audit_packet",
    "project": "proj_abc123",
    "depth": "deep"
  }
}
```

## Response Schema

`synthesis_plan` and `synthesis_verify` return a synthesis run. `synthesis_draft` returns the same
run with an additional `artifact` field.

```typescript
interface SynthesisRunResponse {
  run_id: string;
  status: "planned" | "drafting" | "verified" | "failed";
  request: SynthesisRequest;
  outline: SynthesisOutline;
  source_packs: SynthesisSourcePack[];
  verification: SynthesisVerification;
  artifact?: SynthesisArtifact; // synthesis_draft only
}

interface SynthesisOutline {
  title: string;
  output_type: string;
  audience: string | null;
  sections: SynthesisOutlineSection[];
}

interface SynthesisOutlineSection {
  section_id: string;
  title: string;
  prompt: string;
  source_query: string;
  source_ids: string[];
  gaps: SynthesisGap[];
}

interface SynthesisSourcePack {
  section_id: string;
  title: string;
  query: string;
  source_ids: string[];
  sources: SynthesisSourceReference[];
  hidden_count: number; // Relevant sources withheld by policy
  redaction_count: number;
  freshness: Record<string, string | null>;
  unresolved_claims: string[];
}

interface SynthesisSourceReference {
  id: string;
  type: string;
  name: string;
  content_preview: string;
  score: number;
  source: string | null;
  origin: string; // "graph" or "document"
  relation: string | null;
  metadata: Record<string, any>;
}

interface SynthesisVerification {
  status: "pending" | "gaps" | "pass";
  source_count: number;
  gap_count: number;
  gaps: SynthesisGap[];
}

interface SynthesisGap {
  section_id: string;
  title: string;
  reason: string;
  query: string;
  missing_source_ids: string[];
}

interface SynthesisArtifact {
  artifact_id: string;
  format: "markdown" | "json";
  title: string;
  markdown: string;
  json_payload: Record<string, any>;
  source_ids: string[];
  section_source_ids: Record<string, string[]>;
  generated_text_hash: string;
  verification: SynthesisVerification;
  remembered_memory_id: string | null;
  remembered_source_id: string | null;
}
```

### Verification Status

| Status    | Meaning                                                      |
| --------- | ------------------------------------------------------------ |
| `pending` | Verification has not been applied (raw `synthesis_plan` run) |
| `gaps`    | One or more sections lack sufficient grounding               |
| `pass`    | All sections are adequately source-grounded                  |

`hidden_count` on a source pack reports sources that are relevant but withheld because the caller's
memory policy does not grant access. A high `hidden_count` means the caller is synthesizing without
full visibility.

## Notes

- All three tools enforce project scope. A `project` is verified against the caller's accessible
  projects; without one, sources are drawn from all accessible projects.
- `synthesis_draft` with `remember=true` requires a write-capable role and a resolvable user.
- `synthesis_plan` runs return `status: "planned"` and `verification.status: "pending"`;
  `synthesis_verify` and `synthesis_draft` apply verification before returning.

## Error Handling

| Error                           | Cause                                   | Resolution                            |
| ------------------------------- | --------------------------------------- | ------------------------------------- |
| `Organization context required` | No org-scoped token                     | Authenticate with an org-scoped token |
| `Project access denied: <id>`   | Caller cannot access the project        | Use an accessible project ID          |
| `missing_scope_key`             | `remember` with project scope, no key   | Supply `scope_key` or `project`       |
| `principal_id is required`      | `remember=true` with no resolvable user | Use a user-bound credential           |

## Related

- [mcp-context.md](./mcp-context.md) - Compile a context pack before synthesis
- [mcp-search.md](./mcp-search.md) - Locate candidate sources
- [rest-synthesis.md](./rest-synthesis.md) - REST synthesis endpoints (`/api/synthesis/*`)
