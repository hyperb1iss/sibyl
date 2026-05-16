# REST API: Synthesis

Source-grounded synthesis from authorized memory. The synthesis endpoints turn the knowledge graph
into structured, citation-backed artifacts.

## Overview

Synthesis produces an artifact (documentation, report, briefing, roadmap, release notes, audit
packet) where every section is grounded in cited sources from the knowledge graph. The REST surface
mirrors the MCP synthesis tools.

**Base URL:** `/api/synthesis`

## Authentication

All endpoints require authentication via:

- JWT access token (cookie or Authorization header)
- API key with `api:read` scope for `/plan`, `api:write` for `/draft` with `remember`

## Role Requirements

| Operation                  | Required Roles               |
| --------------------------- | ---------------------------- |
| Plan and draft              | Owner, Admin, Member, Viewer |
| Draft with `remember: true` | Owner, Admin, Member         |

## Endpoints

### Plan Synthesis

```http
POST /api/synthesis/plan
```

Creates a deterministic, source-aware synthesis outline and materializes the source pack for each
section. No artifact text is generated.

**Request Body:**

```json
{
  "goal": "Document the Sibyl memory loop for new contributors",
  "output_type": "documentation",
  "audience": "engineers new to the codebase",
  "depth": "standard",
  "seed_query": null,
  "project": "proj_abc123",
  "domain": null,
  "entity_ids": [],
  "decision_ids": [],
  "task_ids": [],
  "artifact_ids": [],
  "required_sections": [],
  "constraints": [],
  "max_sections": 6,
  "include_neighborhoods": true
}
```

**Request Schema:**

| Field                   | Type     | Required | Default         | Description                                  |
| ----------------------- | -------- | -------- | --------------- | -------------------------------------------- |
| `goal`                  | string   | Yes      | -               | Synthesis goal (1-1000 chars)                |
| `output_type`           | string   | No       | `documentation` | Output type                                  |
| `audience`              | string   | No       | -               | Intended reader (max 500 chars)              |
| `depth`                 | string   | No       | `standard`      | `brief`, `standard`, or `deep`               |
| `seed_query`            | string   | No       | -               | Explicit retrieval query                     |
| `project`               | string   | No       | -               | Project ID to scope sources                  |
| `domain`                | string   | No       | -               | Domain to scope sources                      |
| `entity_ids`            | string[] | No       | `[]`            | Pinned entity sources (max 100)              |
| `decision_ids`          | string[] | No       | `[]`            | Pinned decision sources (max 100)            |
| `task_ids`              | string[] | No       | `[]`            | Pinned task sources (max 100)                |
| `artifact_ids`          | string[] | No       | `[]`            | Pinned artifact sources (max 100)            |
| `required_sections`     | object[] | No       | `[]`            | Forced outline sections (max 12)             |
| `constraints`           | string[] | No       | `[]`            | Free-text output constraints (max 50)        |
| `max_sections`          | integer  | No       | 6               | Outline section cap (1-12)                   |
| `include_neighborhoods` | boolean  | No       | true            | Include one-hop related sources              |

A `required_sections` entry has the shape:

```json
{
  "title": "Background",
  "prompt": "Summarize the prior approach",
  "required_source_ids": ["pattern_abc123"]
}
```

### Output Types

```
documentation, report, briefing, roadmap, release_notes, audit_packet, custom
```

**Response:**

```json
{
  "run_id": "synth_abc123",
  "status": "planned",
  "request": { "goal": "Document the Sibyl memory loop for new contributors" },
  "outline": {
    "title": "The Sibyl Memory Loop",
    "output_type": "documentation",
    "audience": "engineers new to the codebase",
    "sections": [
      {
        "section_id": "sec_1",
        "title": "Capture",
        "prompt": "How memory is captured",
        "source_query": "memory capture remember reflect",
        "source_ids": ["pattern_remember", "decision_raw_memory"],
        "gaps": []
      }
    ]
  },
  "source_packs": [
    {
      "section_id": "sec_1",
      "title": "Capture",
      "query": "memory capture remember reflect",
      "source_ids": ["pattern_remember", "decision_raw_memory"],
      "sources": [
        {
          "id": "pattern_remember",
          "type": "pattern",
          "name": "Raw memory capture",
          "content_preview": "Raw memory is stored verbatim before extraction...",
          "score": 0.91,
          "source": null,
          "origin": "graph",
          "relation": null,
          "metadata": {}
        }
      ],
      "hidden_count": 0,
      "redaction_count": 0,
      "freshness": {},
      "unresolved_claims": []
    }
  ],
  "verification": {
    "status": "pending",
    "source_count": 12,
    "gap_count": 0,
    "gaps": []
  }
}
```

`hidden_count` on a source pack reports relevant sources withheld because the caller's memory
policy does not grant access.

### Draft Synthesis

```http
POST /api/synthesis/draft
```

Plans, materializes, drafts the artifact text, and applies verification. Optionally remembers the
artifact as a memory record.

**Request Body:**

`SynthesisDraftRequest` extends the plan request with these additional fields:

```json
{
  "goal": "Draft release notes for v0.9.0",
  "output_type": "release_notes",
  "project": "proj_abc123",
  "output_format": "markdown",
  "remember": true,
  "memory_scope": "project",
  "scope_key": "proj_abc123",
  "tags": ["release", "v0.9.0"]
}
```

**Additional Request Schema:**

| Field           | Type     | Required | Default    | Description                                      |
| --------------- | -------- | -------- | ---------- | ------------------------------------------------ |
| `output_format` | string   | No       | `markdown` | `markdown` or `json`                             |
| `remember`      | boolean  | No       | false      | Persist the generated artifact as memory         |
| `memory_scope`  | string   | No       | `private`  | `private` or `project` for the remembered artifact |
| `scope_key`     | string   | No       | -          | Project ID when `memory_scope` is `project`      |
| `tags`          | string[] | No       | `[]`       | Tags for the remembered artifact (max 50)        |

When `remember` is `true` with `memory_scope: "project"`, `scope_key` is required (it defaults to
`project` when omitted) and the write requires Member-or-higher role plus project access.

**Response:**

The draft response is a plan response plus an `artifact` field:

```json
{
  "run_id": "synth_def456",
  "status": "verified",
  "request": { "goal": "Draft release notes for v0.9.0" },
  "outline": { "title": "Sibyl v0.9.0 Release Notes", "sections": [] },
  "source_packs": [],
  "verification": {
    "status": "pass",
    "source_count": 18,
    "gap_count": 0,
    "gaps": []
  },
  "artifact": {
    "artifact_id": "art_789",
    "format": "markdown",
    "title": "Sibyl v0.9.0 Release Notes",
    "markdown": "# Sibyl v0.9.0\n\n## Highlights\n...",
    "json_payload": {},
    "source_ids": ["pattern_remember", "decision_raw_memory"],
    "section_source_ids": {
      "sec_1": ["pattern_remember"]
    },
    "generated_text_hash": "sha256:...",
    "verification": {
      "status": "pass",
      "source_count": 18,
      "gap_count": 0,
      "gaps": []
    },
    "remembered_memory_id": "memory_abc",
    "remembered_source_id": "src_abc"
  }
}
```

When `remember` is `false`, `remembered_memory_id` and `remembered_source_id` are `null`.

## Verification

Both endpoints attach a `verification` summary. The `/draft` endpoint applies verification before
returning; `/plan` returns `verification.status: "pending"`.

| Status    | Meaning                                                       |
| --------- | ------------------------------------------------------------- |
| `pending` | Verification not yet applied (raw `/plan` response)           |
| `gaps`    | One or more sections lack sufficient grounding                |
| `pass`    | All sections are adequately source-grounded                   |

Each `gap` identifies the under-grounded section, the reason, the retrieval query that came up
short, and any missing source IDs.

## Error Responses

| Status | Cause                                                          |
| ------ | -------------------------------------------------------------- |
| 400    | Invalid request, or `missing_scope_key` on a project-scoped draft |
| 401    | Missing or invalid authentication                              |
| 403    | Insufficient role, `insufficient_org_role`, or project access denied |
| 422    | Request body validation failed                                |
| 500    | Synthesis planning or drafting failed                          |

## Related

- [mcp-synthesis.md](./mcp-synthesis.md) - MCP synthesis tools
- [rest-memory.md](./rest-memory.md) - Memory and context endpoints
- [rest-search.md](./rest-search.md) - Locate candidate sources
