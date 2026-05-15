# Sibyl Post-v0.8 Synthesis and Memory Cockpit Plan

- Status: full execution plan draft, anchored to the v0.8 evidence baseline at `4855ba8a`
- Target release: v0.9 candidate
- Depends on:
  - `docs/architecture/SIBYL_V08_PURE_SURREAL_CLOSURE_AND_MEMORY_TRUST_PLAN.md`
  - `docs/architecture/SIBYL_V08_PURE_SURREAL_CLOSURE_EXECUTION_PLAN.md`
  - `docs/architecture/SIBYL_NORTHSTAR.md`
- Primary outcome: turn the v0.8 trust substrate into a human-usable and agent-usable memory product
  with source-grounded synthesis, source inspection, correction, source adapters, and a memory
  cockpit.

v0.8 should leave Sibyl with a Surreal-only default runtime, policy-backed memory access, audit
receipts, promotion and share previews, and release gates that prove Graphiti is no longer on the
default path. The next large product move is to make that foundation useful: humans can understand
and shape memory, and agents can produce large source-grounded artifacts without crossing privacy
boundaries.

This plan intentionally keeps the same architecture center as the northstar: raw memory is source
truth, graph records are derived explanation and retrieval surface, and authorization is part of
retrieval rather than a final output filter.

## 1. Release Thesis

The post-v0.8 release should make Sibyl feel like a real second-brain product. The winning shape is
not more storage plumbing. It is a compact loop:

1. Ingest source material into policy-scoped raw memory.
2. Inspect what was remembered and why it is visible.
3. Correct, hide, redact, delete, or promote memory with preview and audit.
4. Synthesize grounded artifacts from authorized graph slices.
5. Remember the generated artifact with provenance back to the source memory.

The release should be shippable even if broad sharing remains preview-only. The important behavior
is that every source, derived fact, generated artifact, and human correction is inspectable,
policy-filtered, and citable.

## 2. Definition Of Done

Post-v0.8 is ready when all of these are true:

- `synthesize` can generate Markdown and JSON artifacts from authorized memory spaces with
  section-level source IDs, hidden-context signals, redaction metadata, freshness metadata, and
  unresolved-claim lists.
- Source inspection works across API, CLI, and web for raw source metadata, derived records,
  visibility, recent audit receipts, freshness, correction history, promotion state, and deletion or
  redaction status.
- Humans can correct, hide, redact, restore, export, or delete personal memory through policy-aware
  APIs and at least one friendly web flow.
- A source adapter contract exists and one mailbox-style import path proves resumable, deduplicated,
  source-preserving ingest into private memory.
- Import progress, skipped records, dedupe decisions, and extraction status are visible without
  requiring graph vocabulary.
- Promotion and share preview results are visible in CLI and web without enabling uncontrolled broad
  sharing.
- The memory cockpit gives users one place to see recent captures, imports, reflections, recalls,
  agent access, source inspections, and suggested follow-up.
- MCP, CLI, API, jobs, and prompt hooks reuse the same policy context and audit receipts for read,
  write, inspect, correction, promotion, sharing preview, and synthesis.
- New release gates catch source-grounding regressions, permission leaks, synthesis citation gaps,
  adapter ingest drift, and UI trust-flow regressions.

The first shippable product cut does not need every adapter, every cockpit panel, or broad sharing.
It does need one vertical path that proves the loop end to end:

1. A user can inspect an authorized source and see derived memory, visibility, policy, and audit
   context.
2. A user can correct or hide memory and watch that correction change recall and synthesis.
3. A user or agent can synthesize a source-backed architecture artifact from authorized project
   memory.
4. The artifact can be remembered with section citations, unresolved claims, and provenance.
5. A release gate can fail if citations, policy filtering, correction handling, or provenance drift.

## 3. Non-Goals

- Do not build arbitrary cross-organization sharing in this release. Keep broad sharing preview-only
  until real usage hardens the policy contract.
- Do not build a custom policy language. Extend the v0.8 policy primitives only where a concrete
  flow requires it.
- Do not make source adapters one-off importer branches. The first adapter should prove the contract
  rather than bypass it.
- Do not let synthesis invent unsupported claims. Missing sources are output gaps, not model
  creativity opportunities.
- Do not make the cockpit a marketing dashboard. It is a working surface for inspection, correction,
  import progress, agent access, and memory confidence.
- Do not require live queries for the first cockpit. Polling or normal query invalidation is
  acceptable until Surreal live-query behavior has explicit permission tests.

## 4. Product Stories

### Source-Grounded Architecture Synthesis

Bliss asks Sibyl to synthesize an architecture overview for the current project. Sibyl proposes an
outline from authorized project memories, materializes source packs per section, drafts the
artifact, verifies citations and hidden-context signals, lists unresolved claims, and optionally
remembers the final document as an artifact linked to the source memory.

### Personal Memory Inspection

Bliss finds a remembered preference and opens its source. Sibyl shows where it came from, what
derived facts were created, which agents read it, whether it was promoted, and how to correct, hide,
redact, export, or delete it. Private text stays hidden from actors who can only see redacted
metadata.

### Large Mailbox Import

Bliss imports a mailbox archive into private memory. Sibyl preserves messages, thread IDs,
timestamps, participants, attachments, source paths, dedupe keys, and import checkpoints. Lexical
metadata search works before embeddings or reflection finish. Nothing enters project memory without
an explicit promotion preview and apply action.

### Agent Access Preview

Before granting a delegated coding agent access to a project or memory space, Bliss previews what
the agent could recall. Sibyl reports visible source IDs, hidden-but-relevant counts, denied source
IDs, and stable reason codes without leaking private content.

### Correction And Supersession

Bliss marks an old decision stale and links the replacement decision. Future recall and synthesis
prefer the newer decision, preserve the old source, expose the correction trail, and list
supersession metadata in generated artifacts.

## 5. Architecture Principles

### 5.1 Source Truth First

Raw source records remain the durable truth. Ingest, reflection, synthesis, correction, and export
must link back to source IDs. Derived graph records can be rebuilt, corrected, or superseded. Source
records should preserve enough metadata to explain where they came from and how they were
transformed.

### 5.2 Policy At Every Boundary

Read, write, inspect, correction, promotion, share preview, export, delete, and synthesis all accept
the same policy context shape. UI affordances can make common cases pleasant, but the server and
core services own decisions and reason codes.

### 5.3 Preview Before Sensitive Writes

Promotion, sharing, correction with broad effects, deletion, redaction, and synthesis artifact
persistence all need previewable consequences. The preview should name source IDs, derived IDs,
target scope, expected visibility, hidden counts, and stable allow or deny reasons.

### 5.4 Derived Artifacts Are Memory Too

Generated architecture docs, release notes, briefs, and reports are artifacts. When saved, they
should carry source packs, section citations, unresolved gaps, policy metadata, author actor, target
audience, and freshness timestamp.

### 5.5 Human Language Before Graph Vocabulary

The web flow should say "source", "visible to", "used by", "hidden", "redacted", "stale",
"replacement", and "promoted" before it says edge, node, relation, or namespace. Power surfaces can
expose graph details after the human has context.

### 5.6 Deterministic Before Generative

Synthesis planning, source selection, policy filtering, citation coverage, freshness checks, and
verification should be deterministic. LLM drafting can turn verified section packs into prose, but
it should not decide which hidden records are safe, which citations are mandatory, or which claims
are supported.

### 5.7 Audit Metadata Is Not Source Content

Audit receipts should be rich enough to answer who, what, where, why, and under which policy
decision. They should not copy private message bodies, document text, generated paragraphs, or raw
attachment content. Receipts carry IDs, counts, reason codes, hashes, timestamps, and bounded
metadata.

### 5.8 Jobs Carry Authority Explicitly

Background jobs must not infer actor authority from environment, default organization, queue name,
or current server process. They carry a serialized policy context, reauthorize before writes, and
fail closed when the payload is missing or stale.

### 5.9 Product Surfaces Share One Contract

The API is the contract owner. CLI, MCP, web, prompt hooks, and jobs may shape presentation, but
they should not fork authorization, redaction, citation, correction, or synthesis semantics.

### 5.10 Full-Text Search Is An Accelerator, Not Truth

Surreal full-text indexes and vector search can accelerate recall and source discovery. They do not
own lifecycle state, visibility, source truth, correction state, or citation guarantees. Those live
in explicit records and policy-aware services.

## 6. Workstreams

### Track A: Memory Space Productization

Purpose: turn v0.8 memory spaces and policy into a product surface rather than internal plumbing.

Build:

- Memory-space CRUD and membership APIs for private, delegated, project, team, organization, shared,
  and public scopes.
- Stable disabled-scope behavior for team, organization, shared, and public writes until explicit
  enablement.
- Graph projections for explanation and traversal, not authorization truth.
- CLI and web listing for spaces, membership, visibility, and disabled reason.
- Agent access preview using the same share-preview primitives from v0.8.

Exit criteria:

- A user can list memory spaces, see who or what can read them, and preview an agent's effective
  recall surface.
- Disabled scopes fail closed with stable reason codes.
- Project memory resolves through canonical control-plane records rather than graph-only inference.

### Track B: Source Inspect, Correction, And Lifecycle

Purpose: make memory accountable and fixable.

Build:

- Source-centered inspect endpoint and CLI command.
- Derived-record, audit-receipt, correction, promotion, and visibility summaries.
- Correction actions: mark wrong, stale, sensitive, duplicate, superseded, hidden, redacted,
  restored, or deleted.
- Supersession links that retrieval and synthesis can understand.
- Export and deletion previews with source and derived-record counts.
- Audit receipts for every lifecycle action.

Exit criteria:

- Owners and admins can explain why memory exists and where it was used.
- Normal readers never receive hidden content through inspect.
- Corrections influence recall and synthesis without destroying source truth.

### Track C: Source Adapter And Raw Ingest Pipeline

Purpose: make large-corpus import boring and resumable.

Build:

- Source adapter protocol with source identity, version token, metadata schema, privacy class,
  transformation behavior, dedupe key, checkpoint, and attachment behavior.
- Import records with status, counters, current checkpoint, skipped records, errors, dedupe stats,
  and extraction status.
- First mailbox-style adapter for MBOX or Maildir archives.
- Private-memory ingest default with explicit promotion as the only path into project or shared
  spaces.
- Background extraction hooks that can run after raw metadata search is ready.

Exit criteria:

- A mailbox-like archive can be imported, stopped, resumed, searched, inspected, and audited.
- Attachments and skipped records are represented without blocking the whole import.
- Imported private records cannot appear in project or organization recall without explicit
  policy-backed promotion.

### Track D: Graph-Guided Synthesis

Purpose: build large-read output on top of recall, provenance, and policy.

Build:

- Synthesis request model with principal, delegated principal, organization, project, memory spaces,
  output type, audience, depth, seed query, source IDs, entity IDs, freshness policy, and optional
  outline.
- Outline planner that proposes sections from graph neighborhoods, active plans, decisions,
  artifacts, raw sources, and unresolved gaps.
- Section-pack materializer that reuses authorized context-pack retrieval.
- Draft artifact renderer for Markdown and JSON.
- Verification pass for source coverage, redactions, freshness, unsupported claims,
  hidden-but-relevant signals, and missing-source gaps.
- Optional artifact persistence through `remember` with source links.

Exit criteria:

- Seeded synthesis produces an architecture overview with source IDs per section.
- Forbidden-scope memories are absent or represented only as redacted hidden signals.
- Unsupported claims are listed as gaps rather than written as facts.
- The generated artifact can be remembered with provenance.

### Track E: Human Memory Cockpit

Purpose: make memory visible, understandable, and controllable in the web UI.

Build:

- Memory home showing recent captures, imports, reflections, recalls, agent access, source
  inspections, and suggested review actions.
- Scope controls for private, delegated, project, team, organization, shared, and public memory.
- Source inspect panel with raw metadata, derived facts, visibility, audit, and correction actions.
- Import progress panel with counters, skipped records, dedupe, attachments, and extraction status.
- Promotion and share preview UI with visible, hidden, denied, and redacted counts.
- Synthesis runner UI with outline review, section source packs, draft preview, verification
  results, and remember action.

Exit criteria:

- A human can create or import private memory, find it, inspect it, correct it, preview promotion,
  synthesize from it, and understand agent access without graph vocabulary.
- UI tests cover the trust flows instead of only happy-path rendering.

### Track F: Evaluation And Release Gates

Purpose: make trust and quality regressions block the release.

Build:

- `synthesis-gate` for citation coverage, hidden-scope absence, unresolved-gap reporting, output
  shape, and artifact provenance.
- `adapter-ingest-gate` for resumability, dedupe, attachment accounting, private scope enforcement,
  and searchable metadata before embedding completion.
- UI trust-flow tests for inspect, correction, promotion preview, share preview, import progress,
  and synthesis verification.
- Benchmark fixtures for agent behavior with and without synthesized project docs.
- Release evidence ledger that distinguishes local receipts from CI receipts.

Exit criteria:

- One command can prove the post-v0.8 product claims.
- Release notes cite gated artifacts, not manual anecdotes.

## 7. Data Model Additions

Prefer additive records with migration-safe defaults.

### Control Plane

- `memory_spaces`
  - ID, organization ID, scope, scope key, name, description, state, created by, created at, updated
    at.
- `memory_space_members`
  - Space ID, principal type, principal ID, role, permissions, expires at, created by, created at.
- `memory_access_previews`
  - Actor, target principal, spaces, requested action, visible counts, denied counts, reason codes,
    created at.

### Source Ingest

- `source_imports`
  - Source adapter, source identity, source version, privacy class, target memory scope, checkpoint,
    status, counters, dedupe stats, error summaries.
- `source_records`
  - Adapter record ID, source import ID, raw memory ID, dedupe key, metadata, attachment refs,
    privacy class, transform version.
- `source_attachments`
  - Source record ID, filename, media type, size, checksum, storage pointer, extraction state.

### Lifecycle

- `memory_corrections`
  - Source ID, derived ID, action, reason, replacement ID, actor, policy context, created at,
    reversible flag.
- `memory_redactions`
  - Source ID, field path, redaction reason, replacement summary, actor, created at.
- `memory_deletions`
  - Source ID, derived IDs, deletion mode, preview counts, actor, created at, retention metadata.

### Synthesis

- `synthesis_runs`
  - Request metadata, actor, memory spaces, output type, audience, status, verification summary,
    artifact ID, created at.
- `synthesis_sections`
  - Run ID, title, outline path, source IDs, hidden counts, unresolved claims, freshness metadata,
    section text, verification status.

## 8. API Surface

Initial REST shape:

- `GET /api/memory/spaces`
- `POST /api/memory/spaces`
- `GET /api/memory/spaces/{space_id}`
- `POST /api/memory/spaces/{space_id}/members/preview`
- `POST /api/memory/spaces/{space_id}/members`
- `GET /api/memory/inspect/{source_id}`
- `POST /api/memory/inspect/{source_id}/corrections/preview`
- `POST /api/memory/inspect/{source_id}/corrections`
- `POST /api/memory/export/preview`
- `POST /api/memory/delete/preview`
- `POST /api/memory/delete`
- `POST /api/sources/imports`
- `GET /api/sources/imports/{import_id}`
- `POST /api/sources/imports/{import_id}/resume`
- `POST /api/sources/imports/{import_id}/cancel`
- `POST /api/synthesize/plan`
- `POST /api/synthesize/sections`
- `POST /api/synthesize/draft`
- `POST /api/synthesize/verify`
- `POST /api/synthesize/remember`

Every endpoint must return stable reason codes for policy denial and must avoid embedding hidden
source text in error detail.

## 9. CLI And MCP Surface

CLI:

- `sibyl memory-space list`
- `sibyl memory-space inspect <space-id>`
- `sibyl memory-space preview-agent <agent-id>`
- `sibyl memory-inspect <source-id>`
- `sibyl memory-correct <source-id> --preview`
- `sibyl memory-correct <source-id> --apply`
- `sibyl memory-delete <source-id> --preview`
- `sibyl source-import start <path> --adapter <name>`
- `sibyl source-import status <import-id>`
- `sibyl source-import resume <import-id>`
- `sibyl synthesize plan`
- `sibyl synthesize draft`
- `sibyl synthesize verify`
- `sibyl synthesize remember`

MCP tools:

- `memory_space_list`
- `memory_inspect`
- `memory_correct_preview`
- `source_import_status`
- `synthesize_plan`
- `synthesize_draft`
- `synthesize_verify`

MCP synthesis should prefer structured JSON output with concise Markdown rendering only when the
caller explicitly requests it.

## 10. Web Surface

Routes:

- `/memory`
- `/memory/sources/[sourceId]`
- `/memory/imports`
- `/memory/imports/[importId]`
- `/memory/spaces`
- `/memory/spaces/[spaceId]`
- `/memory/synthesize`
- `/memory/synthesize/[runId]`

Primary components:

- `MemoryHome`
- `MemoryActivityFeed`
- `MemoryScopeSwitcher`
- `SourceInspectPanel`
- `SourceCorrectionDialog`
- `MemoryPromotionPreview`
- `MemorySharePreview`
- `SourceImportProgress`
- `SynthesisRunner`
- `SynthesisOutlineEditor`
- `SynthesisSectionSources`
- `SynthesisVerificationPanel`

The first screen should be the working memory cockpit, not a landing page. It should show recent
memory activity, pending review, import progress, and quick actions for inspect, import, and
synthesize.

## 11. System Contracts

This section turns the plan into implementation contracts. Packets can adjust names while coding,
but they should preserve these semantics unless the doc is intentionally revised first.

### 11.1 Memory Policy Contract

Every trust-sensitive operation receives or derives a `MemoryPolicyContext` before touching memory:

- actor user ID
- organization ID and organization role
- accessible project IDs
- accessible delegated authority IDs
- agent ID when an agent acts on behalf of a user
- requested memory scope and scope key
- requested project ID when project memory is involved
- source surface such as `api`, `cli`, `mcp`, `job`, `prompt_hook`, or `web`

Operations covered by this contract:

- raw memory read and write
- context pack render
- wake, recall, search, and reflection render
- reflection promotion
- task-learning episode and procedure creation
- source inspect
- correction, hide, redact, restore, supersede, export, and delete preview or apply
- promotion and share preview or apply
- source import start, resume, extraction, and promotion
- synthesis plan, section materialization, draft, verify, and remember

Stable deny reasons:

- `missing_actor`
- `missing_organization`
- `missing_memory_space`
- `missing_scope_key`
- `missing_project_id`
- `scope_disabled`
- `unverified_membership`
- `delegation_required`
- `delegation_expired`
- `unsupported_scope`
- `hidden_source`
- `redacted_source`
- `source_deleted`
- `job_policy_context_missing`
- `job_policy_context_stale`

Policy decisions return the same high-level shape everywhere:

```json
{
  "allowed": false,
  "reason": "unverified_membership",
  "memory_scope": "project",
  "scope_key": "project_123",
  "source_surface": "mcp",
  "audit_id": "audit_..."
}
```

The response may include redacted counts and denied IDs when the actor is allowed to know that
hidden context exists. It must not include hidden source text.

### 11.2 Source And Derived Memory Contract

Raw source records are the root of trust. Derived graph entities, episodes, procedures, artifacts,
facts, and relationships point back to source IDs. Retrieval may rank derived records, but inspect,
correction, deletion, export, synthesis, and audit should all be able to walk from a derived record
to its source.

Minimum source record fields:

- `id`
- `organization_id`
- `memory_scope`
- `scope_key`
- `source_type`
- `source_uri` or source-local identity
- `source_version`
- `content_hash`
- `metadata`
- `privacy_class`
- `created_by`
- `created_at`
- `updated_at`
- `lifecycle_state`

Lifecycle states:

- `active`: available to authorized recall and synthesis
- `hidden`: excluded from normal recall, inspectable by owners/admins
- `redacted`: content replaced or partially masked, metadata preserved
- `superseded`: retained but ranked below replacement records
- `duplicate`: retained for provenance, grouped under canonical source
- `deleted`: content unavailable except retention metadata when policy requires it

Derived records carry:

- source IDs and source hashes
- transform version
- policy metadata from the write decision
- freshness timestamp
- correction and supersession pointers
- confidence or extraction quality when applicable

### 11.3 Inspect Contract

Source inspect answers four questions:

1. What is this memory?
2. Why is it visible or hidden to this actor?
3. What did Sibyl derive from it?
4. What actions can this actor safely take next?

Inspect responses include:

- source metadata and redacted content preview when allowed
- derived entity, relationship, episode, procedure, artifact, and fact IDs
- visibility summary by scope and principal type
- lifecycle state and correction history
- promotion or share state
- recent audit receipts
- freshness and transform version metadata
- available actions with preview-required flags

Inspect never leaks hidden content through nested audit details, derived snippets, error messages,
or action labels.

### 11.4 Correction Contract

Correction actions are previewable before apply. The preview returns affected source IDs, derived
IDs, relationship IDs, recall impact, synthesis impact, reversible flag, and audit action name.

Correction actions:

- `mark_wrong`
- `mark_stale`
- `mark_sensitive`
- `mark_duplicate`
- `supersede`
- `hide`
- `redact`
- `restore`
- `delete`

Retrieval and synthesis must understand these actions:

- Hidden and deleted records are excluded unless the actor explicitly requests an inspect/admin
  surface.
- Superseded records can be used as historical context only when the current replacement is also
  visible.
- Redacted records can contribute metadata and hidden-context counts, not redacted text.
- Duplicate records collapse under the canonical source for synthesis citations.

### 11.5 Source Adapter Contract

Adapters expose source records without owning memory policy. The import service owns target scope,
policy context, persistence, audit, and job behavior.

Adapter responsibilities:

- identify a source corpus and version
- iterate source records deterministically
- emit source-local IDs, timestamps, participants, labels, metadata, body text, and attachments
- emit a stable dedupe key
- emit a checkpoint token after bounded batches
- classify privacy and transform requirements
- report skipped records with reasons

Import service responsibilities:

- verify target memory scope before start and resume
- persist import manifest, counters, checkpoint, errors, and dedupe stats
- write raw source records before extraction
- preserve attachments or attachment metadata according to storage policy
- enqueue extraction and embedding as follow-up work
- default all personal corpus imports to private memory
- require explicit promotion preview before project or shared visibility

Import states:

- `pending`
- `running`
- `paused`
- `completed`
- `failed`
- `cancelled`

Record extraction states:

- `metadata_ready`
- `content_ready`
- `extraction_pending`
- `extracted`
- `embedding_pending`
- `indexed`
- `skipped`
- `failed`

### 11.6 Synthesis Contract

Synthesis runs as a staged pipeline:

1. `plan`: build an outline and expected evidence requirements.
2. `sections`: materialize policy-filtered source packs per section.
3. `draft`: render Markdown or JSON from section packs.
4. `verify`: check citation coverage, hidden-context handling, freshness, and unsupported claims.
5. `remember`: persist the generated artifact through normal memory write policy.

The first implementation should support a deterministic non-LLM path through plan, sections, and
verify. Drafting can use templates or an LLM adapter, but it receives only authorized section packs.

Synthesis request fields:

- actor and organization context
- optional delegated principal or agent ID
- target project or memory-space set
- output type
- audience
- seed query
- source IDs and entity IDs
- freshness policy
- outline override
- output format

Section packs contain:

- section title and outline path
- source IDs
- source snippets or structured facts allowed for the actor
- derived IDs
- hidden-but-relevant counts
- denied source counts
- redaction metadata
- freshness metadata
- unresolved claim candidates
- policy reason codes

Verification statuses:

- `passed`
- `passed_with_gaps`
- `blocked_missing_sources`
- `blocked_policy_denial`
- `blocked_unsupported_claims`
- `blocked_stale_sources`

Generated artifacts are memory. Persisted artifacts must include:

- source pack IDs or source IDs per section
- generated text hash
- unresolved claims
- verification summary
- actor and policy context
- freshness timestamp
- output type and audience

### 11.7 Cockpit Contract

The cockpit is a working surface for memory trust. The default screen prioritizes action over
analytics:

- recent captures and imports
- pending corrections or review suggestions
- recent recalls and context renders
- agent access previews
- source inspections
- synthesis drafts and verification gaps
- import progress and skipped records

Every cockpit action that changes visibility, lifecycle, promotion, sharing, deletion, or generated
artifact persistence must use preview before apply. The UI can make happy paths fast, but the server
remains the source of truth for policy and consequences.

### 11.8 Release Cut Lines

The post-v0.8 work should ship in visible cuts:

#### Cut 1: Trust Inspection

- Memory-space listing and access preview.
- Source inspect API, CLI, and first web panel.
- Correction preview for hide, redact, stale, and supersede.
- `memory-trust-gate` remains green.

#### Cut 2: Source-Grounded Synthesis

- Synthesis plan, section materialization, verification, and artifact remember.
- CLI and MCP synthesis surfaces.
- `synthesis-gate` blocks missing citations, hidden source text, and unsupported claims.

#### Cut 3: Memory Cockpit

- Working `/memory` cockpit.
- Inspect, correction, import progress, and synthesis UI.
- Component and browser checks cover the trust flows.

#### Cut 4: Large Source Import

- Source adapter contract.
- First mailbox adapter.
- Resumable import jobs and private-memory default.
- `adapter-ingest-gate` blocks dedupe, checkpoint, and policy regressions.

#### Cut 5: Release Audit

- Clean final gates.
- CI, docs deploy, nightly, memory trust, synthesis, adapter, and benchmark receipts.
- Binary ship or hold recommendation.

## 12. Implementation Packets

Each packet should land as one atomic commit unless the implementation exposes a smaller natural
boundary. Every packet needs focused tests, lint/typecheck for touched packages, `git diff --check`,
and a receipt in this document or the release evidence ledger.

### Packet 0.1: v0.8 Release Baseline

Depends on:

- v0.8 A6 pure Surreal release audit.
- v0.8 B6 memory trust gate.

Files:

- `docs/architecture/SIBYL_V08_PURE_SURREAL_CLOSURE_AND_MEMORY_TRUST_PLAN.md`
- `docs/architecture/SIBYL_POST_V08_SYNTHESIS_AND_MEMORY_COCKPIT_PLAN.md`

Implementation:

- Record the final v0.8 release commit, CI run IDs, nightly run IDs, benchmark artifacts, and
  trust-gate receipts.
- Lock the starting state for this plan.

Verify:

- `moon run docs:lint`
- v0.8 release gates from the v0.8 plan.

Exit criteria:

- This plan starts from current evidence, not assumptions.

Receipt, 2026-05-14:

- Starting evidence baseline: pushed `main` commit `4855ba8a`.
- Main CI run `25870913035`, docs deploy run `25877971558`, and nightly regression run `25877971585`
  completed successfully on `4855ba8a`.
- v0.8 claim boundary: Surreal-only default runtime, optional Graphiti compatibility, policy-backed
  inspectable memory, promotion preview, share preview, and the `memory-trust-gate`.
- Post-v0.8 owns persisted MemorySpace CRUD, source lifecycle and correction, source adapters,
  `synthesize`, mailbox import, and the memory cockpit.

### Packet A1: Memory Space Records

Files:

- `apps/api/src/sibyl/persistence/surreal/auth_runtime.py`
- `apps/api/src/sibyl/persistence/auth_runtime.py`
- `apps/api/src/sibyl/api/routes/memory.py`
- `apps/api/src/sibyl/api/schemas.py`
- `packages/python/sibyl-core/src/sibyl_core/auth/memory_policy.py`
- `apps/api/tests/test_surreal_auth_runtime.py`
- `apps/api/tests/test_routes_memory.py`
- `packages/python/sibyl-core/tests/test_memory_policy.py`

Implementation:

- Add control-plane memory-space and membership records.
- Add owner/admin CRUD for space metadata.
- Keep disabled scopes closed with stable reason codes.
- Project memory spaces must map to canonical project records.

Verify:

- `moon run api:test -- tests/test_surreal_auth_runtime.py tests/test_routes_memory.py`
- `moon run core:test -- tests/test_memory_policy.py`
- `moon run api:lint api:typecheck core:lint core:typecheck`

Exit criteria:

- Memory-space membership can be resolved without graph lookups.
- Disabled scopes fail closed.

### Packet A2: Agent Access Preview

Files:

- `packages/python/sibyl-core/src/sibyl_core/services/native_memory.py`
- `apps/api/src/sibyl/api/routes/memory.py`
- `apps/api/src/sibyl/api/schemas.py`
- `apps/cli/src/sibyl_cli/main.py`
- `apps/cli/src/sibyl_cli/client.py`
- `apps/api/tests/test_routes_memory.py`
- `apps/cli/tests/test_main_capture.py`

Implementation:

- Add preview for what a user, agent, or delegated principal can recall from a selected memory-space
  set.
- Return visible source IDs, hidden counts, denied IDs, reason codes, and audit receipt IDs when
  available.
- Reuse share-preview redaction and deny behavior.

Verify:

- `moon run api:test -- tests/test_routes_memory.py`
- `moon run cli:test -- tests/test_main_capture.py`
- `moon run api:lint api:typecheck cli:lint cli:typecheck`

Exit criteria:

- Access preview is non-mutating and policy-equivalent to recall.

### Packet B1: Source Inspect Surface

Files:

- `apps/api/src/sibyl/api/routes/memory.py`
- `apps/api/src/sibyl/api/schemas.py`
- `apps/api/src/sibyl/persistence/auth_runtime.py`
- `apps/api/src/sibyl/persistence/surreal/auth_runtime.py`
- `packages/python/sibyl-core/src/sibyl_core/services/native_memory.py`
- `apps/cli/src/sibyl_cli/main.py`
- `apps/cli/src/sibyl_cli/client.py`
- `apps/api/tests/test_routes_memory.py`
- `apps/api/tests/test_surreal_auth_runtime.py`
- `apps/cli/tests/test_main_capture.py`

Implementation:

- Add source inspect API and CLI.
- Return source metadata, derived IDs, correction history, promotion state, recent audit receipts,
  visibility metadata, and freshness metadata.
- Redact content unless the actor can read the source through normal memory policy.

Verify:

- `moon run api:test -- tests/test_routes_memory.py tests/test_surreal_auth_runtime.py`
- `moon run cli:test -- tests/test_main_capture.py`
- `moon run api:lint api:typecheck cli:lint cli:typecheck`

Exit criteria:

- Inspect explains memory without leaking hidden content.

### Packet B2: Correction And Lifecycle Actions

Files:

- `packages/python/sibyl-core/src/sibyl_core/services/native_memory.py`
- `packages/python/sibyl-core/src/sibyl_core/retrieval/native.py`
- `packages/python/sibyl-core/tests/test_native_memory.py`
- `packages/python/sibyl-core/tests/test_native_retrieval.py`
- `apps/api/src/sibyl/api/routes/memory.py`
- `apps/api/src/sibyl/api/schemas.py`
- `apps/api/tests/test_routes_memory.py`

Implementation:

- Add preview and apply paths for stale, wrong, sensitive, duplicate, superseded, hidden, redacted,
  restored, and deleted states.
- Make retrieval prefer non-superseded and non-hidden records while preserving source history.
- Emit audit receipts for every lifecycle action.

Verify:

- `moon run core:test -- tests/test_native_memory.py tests/test_native_retrieval.py`
- `moon run api:test -- tests/test_routes_memory.py`
- `moon run core:lint core:typecheck api:lint api:typecheck`

Exit criteria:

- Corrections affect recall and synthesis while preserving source truth.

### Packet C1: Source Adapter Contract

Files:

- `packages/python/sibyl-core/src/sibyl_core/models/sources.py`
- `packages/python/sibyl-core/src/sibyl_core/services/source_adapters.py`
- `packages/python/sibyl-core/tests/test_source_adapters.py`
- `apps/api/src/sibyl/api/routes/crawler.py`
- `apps/api/src/sibyl/api/routes/memory.py`
- `apps/api/src/sibyl/api/schemas.py`

Implementation:

- Define adapter protocol, import manifest, source record, attachment record, checkpoint, dedupe
  key, and privacy-class behavior.
- Keep adapters source-preserving and policy-aware.
- Add deterministic fake adapter tests.

Verify:

- `moon run core:test -- tests/test_source_adapters.py`
- `moon run api:test -- tests/test_routes_memory.py`
- `moon run core:lint core:typecheck api:lint api:typecheck`

Exit criteria:

- New adapters can be added without branching core import behavior.

### Packet C2: Mailbox Import Adapter

Files:

- `packages/python/sibyl-core/src/sibyl_core/services/source_adapters.py`
- `packages/python/sibyl-core/src/sibyl_core/services/mailbox_adapter.py`
- `packages/python/sibyl-core/tests/test_mailbox_adapter.py`
- `apps/api/src/sibyl/jobs/source_imports.py`
- `apps/api/tests/test_jobs_source_imports.py`

Implementation:

- Implement one mailbox-style adapter, preferably MBOX first unless Maildir is simpler against
  available fixtures.
- Preserve message IDs, thread IDs when available, timestamps, participants, subject, body,
  attachments, source path, and dedupe key.
- Default target scope to private memory.

Verify:

- `moon run core:test -- tests/test_mailbox_adapter.py`
- `moon run api:test -- tests/test_jobs_source_imports.py`
- `moon run core:lint core:typecheck api:lint api:typecheck`

Exit criteria:

- A mailbox archive can be imported into private source records without extraction or embedding
  completing first.

### Packet C3: Resumable Import Jobs And Progress

Files:

- `apps/api/src/sibyl/jobs/source_imports.py`
- `apps/api/src/sibyl/api/routes/crawler.py`
- `apps/api/src/sibyl/api/routes/memory.py`
- `apps/api/src/sibyl/api/schemas.py`
- `apps/api/tests/test_jobs_source_imports.py`
- `apps/api/tests/test_routes_memory.py`

Implementation:

- Add start, status, resume, cancel, and progress counters for source imports.
- Persist checkpoints after bounded batches.
- Record skipped records, dedupe counts, errors, attachments, and extraction pending counts.
- Make import jobs fail closed without actor and target-scope policy context.

Verify:

- `moon run api:test -- tests/test_jobs_source_imports.py tests/test_routes_memory.py`
- `moon run api:lint api:typecheck`

Exit criteria:

- Interrupted imports can resume without duplicate raw records.
- Import progress is readable and source-safe.

### Packet D1: Synthesis Request And Plan Contract

Files:

- `packages/python/sibyl-core/src/sibyl_core/models/synthesis.py`
- `packages/python/sibyl-core/src/sibyl_core/services/synthesis.py`
- `packages/python/sibyl-core/tests/test_synthesis.py`
- `apps/api/src/sibyl/api/routes/synthesis.py`
- `apps/api/src/sibyl/api/app.py`
- `apps/api/src/sibyl/api/schemas.py`
- `apps/api/tests/test_routes_synthesis.py`

Implementation:

- Add synthesis request, outline, section, source-pack, verification, and run models.
- Add a non-LLM outline planner from seed query, entity IDs, decisions, tasks, artifacts, and graph
  neighborhoods.
- Return gaps when no source supports a requested section.

Verify:

- `moon run core:test -- tests/test_synthesis.py`
- `moon run api:test -- tests/test_routes_synthesis.py`
- `moon run core:lint core:typecheck api:lint api:typecheck`

Exit criteria:

- Synthesis planning is deterministic and source-aware before drafting exists.

### Packet D2: Section Pack Materialization

Files:

- `packages/python/sibyl-core/src/sibyl_core/services/synthesis.py`
- `packages/python/sibyl-core/src/sibyl_core/tools/context.py`
- `packages/python/sibyl-core/tests/test_synthesis.py`
- `packages/python/sibyl-core/tests/test_context_pack.py`
- `apps/api/tests/test_routes_synthesis.py`

Implementation:

- Reuse context-pack retrieval for each outline section.
- Track source IDs, hidden counts, redactions, freshness, and unresolved claims per section.
- Apply policy before materialization and again before render.

Verify:

- `moon run core:test -- tests/test_synthesis.py tests/test_context_pack.py`
- `moon run api:test -- tests/test_routes_synthesis.py`
- `moon run core:lint core:typecheck api:lint api:typecheck`

Exit criteria:

- Section packs never contain unauthorized source text.

### Packet D3: Draft, Verify, And Remember Artifact

Files:

- `packages/python/sibyl-core/src/sibyl_core/services/synthesis.py`
- `packages/python/sibyl-core/src/sibyl_core/models/synthesis.py`
- `packages/python/sibyl-core/tests/test_synthesis.py`
- `apps/api/src/sibyl/api/routes/synthesis.py`
- `apps/api/tests/test_routes_synthesis.py`

Implementation:

- Render Markdown and JSON artifacts from section packs.
- Verify citation coverage, hidden-context handling, freshness, unsupported claims, and unresolved
  gaps.
- Add optional artifact persistence that writes generated artifacts through normal `remember`
  behavior with source links.

Verify:

- `moon run core:test -- tests/test_synthesis.py`
- `moon run api:test -- tests/test_routes_synthesis.py`
- `moon run core:lint core:typecheck api:lint api:typecheck`

Exit criteria:

- The first synthesis artifact is citable, policy-safe, and rememberable.

### Packet D4: CLI And MCP Synthesis

Files:

- `apps/cli/src/sibyl_cli/main.py`
- `apps/cli/src/sibyl_cli/client.py`
- `apps/cli/tests/test_main_capture.py`
- `apps/api/src/sibyl/server.py`
- `packages/python/sibyl-core/src/sibyl_core/tools/synthesis.py`
- `packages/python/sibyl-core/tests/test_tools.py`

Implementation:

- Add CLI commands for plan, draft, verify, and remember.
- Add MCP tools for synthesis plan, draft, and verify.
- Prefer JSON output for agent calls and readable Markdown for humans.

Verify:

- `moon run cli:test -- tests/test_main_capture.py`
- `moon run core:test -- tests/test_tools.py`
- `moon run api:test -- tests/test_mcp_auth.py tests/test_server_accessible_projects.py`
- `moon run cli:lint cli:typecheck core:lint core:typecheck api:lint api:typecheck`

Exit criteria:

- Agents can synthesize through MCP without bypassing policy context.

### Packet E1: Memory Cockpit Foundation

Files:

- `apps/web/src/lib/api.ts`
- `apps/web/src/lib/constants/navigation.ts`
- `apps/web/src/app/(main)/memory/page.tsx`
- `apps/web/src/app/(main)/memory/memory-content.tsx`
- `apps/web/src/components/memory/memory-home.tsx`
- `apps/web/src/components/memory/memory-activity-feed.tsx`
- `apps/web/src/components/memory/memory-scope-switcher.tsx`
- `apps/web/src/components/layout/sidebar.tsx`
- `apps/web/src/lib/constants/navigation.test.ts`
- `apps/web/src/app/(main)/memory/page.test.tsx`

Implementation:

- Add memory route and navigation.
- Render recent captures, imports, reflections, recalls, agent access, and pending review actions.
- Keep layout dense, calm, and task-oriented.

Verify:

- `moon run web:test -- src/app/\\(main\\)/memory/page.test.tsx src/lib/constants/navigation.test.ts`
- `moon run web:lint web:typecheck`

Exit criteria:

- Memory has a real working home in the app.

### Packet E2: Source Inspect And Correction UI

Files:

- `apps/web/src/lib/api.ts`
- `apps/web/src/app/(main)/memory/sources/[sourceId]/page.tsx`
- `apps/web/src/components/memory/source-inspect-panel.tsx`
- `apps/web/src/components/memory/source-correction-dialog.tsx`
- `apps/web/src/components/memory/source-visibility-summary.tsx`
- `apps/web/src/components/memory/source-inspect-panel.test.tsx`

Implementation:

- Render source metadata, derived records, audit summaries, visibility, correction history, and
  available lifecycle actions.
- Add correction preview and apply flow.
- Never render hidden text when API marks content redacted.

Verify:

- `moon run web:test -- src/components/memory/source-inspect-panel.test.tsx`
- `moon run web:lint web:typecheck`

Exit criteria:

- Human inspect and correction flows are usable without graph vocabulary.

### Packet E3: Import And Synthesis UI

Files:

- `apps/web/src/lib/api.ts`
- `apps/web/src/app/(main)/memory/imports/page.tsx`
- `apps/web/src/app/(main)/memory/synthesize/page.tsx`
- `apps/web/src/components/memory/source-import-progress.tsx`
- `apps/web/src/components/memory/synthesis-runner.tsx`
- `apps/web/src/components/memory/synthesis-outline-editor.tsx`
- `apps/web/src/components/memory/synthesis-verification-panel.tsx`
- `apps/web/src/components/memory/synthesis-runner.test.tsx`

Implementation:

- Show import status, counters, skipped records, dedupe, attachments, and extraction status.
- Add synthesis runner with outline review, section source packs, draft preview, verification gaps,
  and remember action.
- Keep share and promotion previews visible but do not enable broad share writes.

Verify:

- `moon run web:test -- src/components/memory/synthesis-runner.test.tsx`
- `moon run web:lint web:typecheck`

Exit criteria:

- A user can watch ingest progress and synthesize a source-backed artifact from the web UI.

### Packet F1: Post-v0.8 Product Gates

Files:

- `moon.yml`
- `tools/trust/synthesis_gate.py`
- `tools/trust/adapter_ingest_gate.py`
- `tools/tests/test_synthesis_gate.py`
- `tools/tests/test_adapter_ingest_gate.py`
- `benchmarks/context_pack_cases.json`
- `docs/testing/benchmark-methodology.md`

Implementation:

- Add `synthesis-gate` and `adapter-ingest-gate` moon tasks.
- Require source IDs per section, hidden-scope absence, unresolved-gap reporting, artifact
  provenance, import resumability, dedupe correctness, and private scope enforcement.
- Document benchmark and artifact locations.

Verify:

- `moon run synthesis-gate adapter-ingest-gate`
- `moon run bench-gate`
- `moon run docs:lint`

Exit criteria:

- Product claims have repeatable local gates.

### Packet F2: Release Audit

Files:

- `docs/architecture/SIBYL_POST_V08_SYNTHESIS_AND_MEMORY_COCKPIT_PLAN.md`
- release notes draft

Implementation:

- Run the final local gates from a clean worktree.
- Record CI, docs deploy, nightly, synthesis, adapter, memory trust, and bench receipts.
- Write a binary ship or hold recommendation.

Verify:

- `moon run memory-trust-gate`
- `moon run synthesis-gate`
- `moon run adapter-ingest-gate`
- `moon run bench-gate`
- `moon run core:test`
- `moon run api:test`
- `moon run cli:test`
- `moon run web:test`
- `moon run docs:lint`
- `moon run :check`
- CI green on `main`
- nightly regression green on `main`

Exit criteria:

- The release can claim source-grounded synthesis, inspectable and correctable memory,
  source-preserving ingest, and a usable memory cockpit.

Receipt, 2026-05-15:

- Local release-audit candidate: current local `main`, ahead of `origin/main` by 21 commits. The
  tracked worktree was clean after the gate repairs; local untracked scratch files were left
  untouched.
- Product surface delivered:
  - Source-grounded synthesis across service, CLI, MCP, API, and web with section source IDs,
    unresolved-gap reporting, hidden-source filtering, artifact provenance, and remember provenance.
  - Inspectable and correctable memory through source inspection, correction preview/apply, audit
    receipts, and unified Memory cockpit capture review.
  - Source-preserving ingest through adapter contracts, mailbox import, resumable jobs, dedupe keys,
    private-memory defaults, and import progress UI.
  - Unified cockpit UX: Memory owns captures, imports, synthesis, review, and source inspection; the
    legacy Archive route redirects into Memory Captures with query parameters preserved.
- Local verification:
  - `moon run memory-trust-gate` -> PASS, 7 checks and 0 failed. Covered surfaces: audit, CLI,
    context pack, inspect, jobs, MCP, memory policy, promotion preview, prompt hook, raw memory,
    recall, reflect, share preview, task learning, and wake.
  - `moon run synthesis-gate` -> PASS, 2 checks and 0 failed. Covered surfaces: artifact provenance,
    hidden-scope absence, remember provenance, source IDs per section, and unresolved-gap reporting.
  - `moon run adapter-ingest-gate` -> PASS, 2 checks and 0 failed. Covered surfaces: dedupe
    correctness, import resumability, private scope enforcement, source adapter contract, and
    source-preserving ingest.
  - `moon run bench-gate` -> Gate passed for `benchmarks/results/ai-memory/manifest.json`.
  - `moon run core:test` -> 932 passed, 14 skipped, 20 deselected in 7.19s.
  - `moon run api:test` -> 1467 passed, 1 skipped, 16 deselected in 14.53s.
  - `moon run cli:test` -> 174 passed in 8.70s.
  - `moon run web:test` -> 26 files passed, 102 tests passed.
  - Post-unified-UX update: `moon run web:lint web:typecheck docs:lint` -> 3 tasks completed, 1
    cached.
  - `moon run docs:lint` -> all matched files use Prettier code style.
  - `moon run :check` -> 40 tasks completed, 26 cache hits before the receipt docs were written;
    post-doc rerun completed 36 tasks with 33 cache hits.
  - `git diff --check` -> clean.
- Gate repairs made during audit:
  - `docs/.prettierignore` now ignores generated `docs/research/*` dumps and local Gradial
    brainstorm scratch notes so docs lint evaluates tracked documentation rather than ignored local
    artifacts.
  - `docs/research/rust-port/INVENTORY.md` and its guard constants now include the synthesis REST
    router and the three MCP synthesis tools.
  - `tools/tests/test_dev_scripts.py` now uses non-login bash for the legacy detector test so the
    docker stub remains on `PATH` and `moon run :check` does not hang on real local Docker.
- GitHub receipts:
  - Latest pushed `origin/main` is `f8d23e6450ec86dfd8251f0c94e6804cdbcc4f76`.
  - CI run `25879991056` succeeded on `f8d23e6450ec86dfd8251f0c94e6804cdbcc4f76`.
  - Docs deploy run `25879991026` succeeded on `f8d23e6450ec86dfd8251f0c94e6804cdbcc4f76`.
  - Release run `25891107401` succeeded on `f8d23e6450ec86dfd8251f0c94e6804cdbcc4f76`.
  - Latest nightly regression receipt is run `25877971585`, successful on
    `4855ba8ad8be6be958ba720e81b3459e727a973b`.
  - No CI, docs deploy, or nightly regression receipt covers the current local release-audit
    candidate.
- Binary recommendation: HOLD for tag or public release until the local 0.9 candidate is pushed to a
  CI-covered branch and CI, docs deploy, and nightly regression are green on that exact head. The
  local product gate evidence supports the source-grounded synthesis, inspectable/correctable
  memory, source-preserving ingest, and usable cockpit claims once that external verification lands.

## 13. Verification Matrix

| Surface              | Gate                                                  | Required before |
| -------------------- | ----------------------------------------------------- | --------------- |
| Memory spaces        | `moon run api:test -- tests/test_routes_memory.py`    | A2              |
| Policy context       | `moon run memory-trust-gate`                          | every packet    |
| Source inspect       | `moon run api:test -- tests/test_routes_memory.py`    | B2, E2          |
| Correction lifecycle | `moon run core:test -- tests/test_native_memory.py`   | D1              |
| Adapter contract     | `moon run core:test -- tests/test_source_adapters.py` | C2              |
| Mailbox ingest       | `moon run adapter-ingest-gate`                        | F2              |
| Synthesis            | `moon run synthesis-gate`                             | F2              |
| CLI                  | `moon run cli:test`                                   | D4, F2          |
| MCP                  | `moon run api:test -- tests/test_mcp_auth.py`         | D4, F2          |
| Web cockpit          | `moon run web:test`                                   | E3, F2          |
| Full release         | `moon run :check`                                     | F2              |

## 14. Risk Register

| Risk                                | Why it matters                           | Mitigation                                            |
| ----------------------------------- | ---------------------------------------- | ----------------------------------------------------- |
| Synthesis writes unsupported claims | Users may trust generated docs too much  | Require source IDs and unresolved-gap output          |
| Inspect leaks hidden source text    | Trust UI becomes a privacy bug           | Server-side redaction and negative tests              |
| Source adapters become one-offs     | Ingest complexity fragments quickly      | Contract-first fake adapter before mailbox adapter    |
| Corrections delete source truth     | Future audits lose provenance            | Lifecycle metadata over destructive delete by default |
| Memory cockpit becomes ornamental   | Users still cannot fix memory            | Build inspect/correct/import/synthesize flows first   |
| Broad sharing ships too early       | Policy surface expands before trust      | Keep sharing preview-only until explicit enablement   |
| Live UI outruns permission tests    | Realtime can leak state changes          | Defer live queries until permission fixtures pass     |
| Import jobs bypass actor policy     | Background workers become a side channel | Serialize policy context into job payloads            |

## 15. Open Questions

- Should the first mailbox adapter be MBOX or Maildir?
- Should source attachments be stored inside SurrealDB records, content storage, or
  filesystem-backed blobs with metadata in SurrealDB?
- Which synthesis output ships first: architecture overview, release notes, onboarding guide, audit
  packet, or custom Markdown?
- Should generated artifacts be persisted automatically after verification, or only after explicit
  `remember`?
- What is the first web correction flow: mark stale, hide, redact, delete, or supersede?
- How much redacted hidden-context signal should synthesis expose without overemphasizing
  unavailable context?
- Should organization and team memory remain preview-only for the entire post-v0.8 release?
- What exact UI test runner should own cockpit end-to-end flows if component tests are not enough?
- Which source adapter should follow mailbox import: repo snapshot, chat export, docs crawl,
  calendar export, or Haven event history?

## 16. Recommendation

Start with Track B and Track D in a narrow vertical slice:

1. Build source inspect and correction primitives.
2. Build deterministic synthesis planning and section packs.
3. Add draft, verification, and artifact remember.
4. Add the minimum cockpit UI around those flows.

This sequence makes the product immediately more useful while preserving the trust model. Source
inspect gives humans confidence. Synthesis gives agents a powerful large-read primitive. The cockpit
turns both into a visible product. Mailbox import should follow once inspect and lifecycle controls
are solid, because large personal corpora are only safe after people can see and correct what Sibyl
remembered.
