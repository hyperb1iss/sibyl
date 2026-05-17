# Sibyl Reflection OS Plan

- Status: design provenance; first implementation slice shipped in v0.10.0
- Created: 2026-05-15
- Last implementation checkpoint: 2026-05-17
- Tracking task: `9c8b0033-fc26-4342-bce5-9f104fc69e06`
- Parent roadmap: [`SIBYL_1_0_ROADMAP.md`](SIBYL_1_0_ROADMAP.md)
- Active remap spec:
  [`SIBYL_POST_V010_RELEASE_REMAP_SPEC.md`](SIBYL_POST_V010_RELEASE_REMAP_SPEC.md)

This document is no longer the active release label. v0.10.0 absorbed the first Reflection OS slice:
reflection lifecycle records, dream-cycle maintenance, automatic promotion receipts, and the
`reflection-quality-gate`. Keep this plan as design provenance for reflection-quality work and for
the v0.11-v0.13 gates that depend on reflection behavior.

## 1. Original Release Promise

The original v0.12 packet promised to make memory consolidate itself.

A normal coding session should leave behind raw captures, source-grounded claims, decisions,
procedures, tasks, artifacts, duplicate/stale/contradiction decisions, policy receipts, and better
recall without a human approving routine cases.

Humans stay in the loop only for sensitive material, ambiguous contradictions, destructive actions,
policy failures, and high-impact sharing.

## 2. Why This Was A Large Block

The original v0.12 packet made sense before v0.10 expanded. v0.10 made memory review less manual,
installed trust-control and auth-session gates, and shipped the first Reflection OS slice. The
remaining reflection work now feeds the post-v0.10 schedule rather than owning a separate immediate
release label.

This packet pulls three roadmap threads together:

- W4 Reflection Intelligence: extraction, dedupe, contradiction, staleness, supersession.
- W6 Native LLM Substrate: only the thin provider-neutral extraction interface needed for reflection
  fixtures.
- W7 Agent Workflow Product: only the exception and receipt surfaces needed to inspect automatic
  decisions.

Graphiti deletion is not in this packet. Graphiti deletion is the prize; reflection quality is the
bridge.

## 3. Current Foundation

Already available:

- `ReflectionCandidate` and `ReflectionPack` model extracted memory candidates.
- Raw memory stores review state, source IDs, capture surface, tags, metadata, and policy context.
- Reflection candidate promotion previews enforce memory policy before durable graph writes.
- The autonomy engine routes safe candidates to promotion and exceptions to review.
- Correction lifecycle actions exist for wrong, stale, sensitive, duplicate, hidden, redacted,
  deleted, restored, and superseded memories.
- Existing gates cover memory trust, trust control, auth sessions, autonomy, overview performance,
  source ingest, and synthesis.

Landed in the first implementation slice:

- first-class claim, finding, and lifecycle records with serialization coverage
- structured reflection extraction with deterministic fixtures and source-grounding checks
- duplicate, stale, contradiction, and supersession decisions routed through findings
- org-scoped reflection dream-cycle maintenance job with budgets, dry-run support, receipts, and
  nightly all-org scheduling
- queue, owner API, CLI, and web visibility for automatic promotions and exception routing
- `reflection-quality-gate` wired to core, API, CLI, and web reflection-quality tests
- dogfood recall-improvement fixture proving reflected context beats the pre-reflection queue
- final release-matrix pass across autonomy, memory trust, auth/session, synthesis, source ingest,
  overview performance, and trust-control gates on May 15, 2026

## 4. Scope

### Packet A: Claim And Lifecycle Model

Introduce first-class reflection records instead of hiding every decision in free-form metadata.

Expected outputs:

- `ClaimRecord`: source-grounded assertion with confidence, source IDs, scope, freshness, validity,
  and contradiction support.
- `ReflectionFinding`: duplicate, stale, contradiction, supersession, promotion, or exception
  decision with reason, confidence, and policy receipt.
- `MemoryLifecycle`: shared state transitions for pending, promoted, duplicate, stale, wrong,
  sensitive, superseded, hidden, redacted, deleted, and restored.
- Native storage helpers that link raw captures, candidates, promoted graph entities, findings, and
  corrections.
- Inspect output that shows source IDs, derived IDs, lifecycle state, findings, and reversibility.

Likely files:

- `packages/python/sibyl-core/src/sibyl_core/models/reflection.py`
- `packages/python/sibyl-core/src/sibyl_core/services/native_memory.py`
- `packages/python/sibyl-core/src/sibyl_core/services/surreal_content.py`
- `apps/api/src/sibyl/api/routes/memory.py`
- `apps/api/src/sibyl/api/schemas.py`

Acceptance:

- corrections update lifecycle records and recall exclusion consistently
- promoted graph entities link back to raw sources and findings
- inspect output explains why a memory is active, hidden, stale, duplicate, or superseded

### Packet B: Native Reflection Extractor

Make reflection produce structured outputs for more than generic candidates.

Expected outputs:

- extractor contract for claims, decisions, plans, procedures, tasks, artifacts, and relationships
- deterministic fake provider for tests
- schema validation before any candidate or finding is persisted
- source-grounding requirement for every extracted item
- confidence and sensitivity signals that feed autonomy decisions

Likely files:

- `packages/python/sibyl-core/src/sibyl_core/tools/reflect.py`
- `packages/python/sibyl-core/src/sibyl_core/services/reflection.py`
- `packages/python/sibyl-core/src/sibyl_core/models/reflection.py`
- `packages/python/sibyl-core/tests/test_reflection_extractor.py`

Acceptance:

- extraction never persists unsupported items without source IDs
- diary and session inputs can produce claims plus candidate memories
- fixtures cover decisions, procedures, artifacts, task learnings, and noisy session logs

### Packet C: Duplicate, Stale, Contradiction, Supersession Engine

Turn review from "does this look safe" into "what should happen to this memory."

Expected outputs:

- exact duplicate detection by source hash and stable normalized text hash
- near-duplicate candidate lookup against same scope/project
- stale detection using explicit date/version/supersession signals
- contradiction detection against active claims in the same policy-visible scope
- deterministic finding reasons and confidence scores
- exception routing for ambiguous or destructive outcomes

Acceptance:

- duplicate candidates archive without durable graph churn
- stale claims become recall-excluded when a newer supported claim supersedes them
- contradictions require review unless the newer claim has an explicit supersession signal
- all actions preserve rollback metadata and audit receipts

### Packet D: Policy-Backed Auto-Promotion And Exceptions

Keep automatic reflection aggressive but bounded.

Expected outputs:

- autonomy rules consume `ReflectionFinding`, not only ad hoc candidate metadata
- safe private, delegated, and project-scope findings can promote automatically
- organization, shared, public, sensitive, destructive, or cross-scope findings route to review
- dry-run parity for every automatic decision
- REST, CLI, MCP, and job surfaces share the same policy decisions

Acceptance:

- safe session learning promotes without manual review
- private memory does not leak through promotion, recall, synthesis, or inspect
- policy denials include stable reasons and source IDs
- exception queues contain only cases humans actually need to see

### Packet E: Dream-Cycle Consolidation Jobs

Run reflection as maintenance, not as a manual chore.

Expected outputs:

- scheduled/background job that scans recent raw captures and pending candidates by org/project
- idempotent run receipts: scanned, promoted, archived, exceptioned, skipped, latency, model usage
- bounded budgets per run to avoid hot-looping a bad corpus
- retry-safe resume markers
- owner/debug visibility through status and logs

Acceptance:

- repeated runs do not duplicate claims or graph entities
- failed runs preserve enough receipt data to diagnose the failed source or finding
- job payloads include memory policy context and audit metadata

### Packet F: Reflection Quality Gate

Make the reflection packet shippable through proof, not vibes.

Expected outputs:

- `tools/trust/reflection_quality_gate.py`
- `tools/tests/test_reflection_quality_gate.py`
- `moon run reflection-quality-gate`
- fixture suite for duplicate suppression, stale replacement, contradiction exception, diary
  promotion, private leak prevention, and rollback/correction propagation
- dogfood recall eval showing better context after reflection than before reflection

Required surfaces:

- extraction
- source grounding
- duplicate detection
- contradiction detection
- stale/supersession lifecycle
- correction and rollback
- permission safety
- recall quality
- CLI/API visibility

### Packet G: Minimum Workspace Visibility

Do not polish the whole Memory Workspace yet. Show enough for trust.

Expected outputs:

- CLI status for reflection runs, findings, and exceptions
- web memory activity feed entries for automatic promotions and exception routing
- inspect links from candidate/finding to raw sources and promoted graph entities
- correction actions that operate on findings and lifecycle state

Acceptance:

- a user can answer "why does Sibyl believe this?" from the product
- a user can correct, hide, mark stale, mark duplicate, or restore without touching raw data
- automatic decisions have visible receipts

## 5. Non-Goals

- full Graphiti dependency deletion
- broad source adapter expansion
- full synthesis rewrite
- arbitrary cross-org or public memory sharing
- full LLM provider settings UI
- replacing every free-form metadata field in one migration
- making human review impossible; the goal is exception-only review

## 6. Implementation Waves

### Wave 0: Baseline And Gate Skeleton

- audit current reflection, correction, autonomy, and inspect surfaces
- add the `reflection-quality-gate` skeleton and required-surface tests
- add fixture names before implementation so the target cannot drift
- update roadmap docs

Commit shape: docs and gate skeleton.

### Wave 1: Claim And Finding Foundation

- add models, serialization, and storage helpers
- thread lifecycle records through inspect and correction preview/apply
- keep current candidate promotion behavior compatible

Commit shape: model/service/tests.

### Wave 2: Extraction And Finding Generation

- add extractor contract and deterministic fake provider
- generate claims and findings from raw/session/diary inputs
- enforce source-grounding before persistence

Commit shape: core reflection tests plus API route coverage.

### Wave 3: Lifecycle Decisions

- implement duplicate, stale, contradiction, and supersession decisions
- connect findings to correction and recall exclusion
- make rollback metadata visible

Commit shape: core lifecycle tests plus memory trust regression tests.

### Wave 4: Dream-Cycle Runtime

- add scheduled/background consolidation job
- make job receipts inspectable
- add idempotency and budget protections

Commit shape: API job tests plus owner/debug receipts.

### Wave 5: Quality Gate And Product Receipts

- wire `reflection-quality-gate` to core, API, CLI, and web checks
- add CLI and web visibility for findings and exceptions
- run old trust gates alongside the new gate

Commit shape: gate implementation, CLI/web tests, docs.

## 7. Required Dogfood Fixture

The release fixture should replay a realistic coding session:

1. raw session captures a decision, a procedure, a task learning, and a stale claim
2. a later session repeats the procedure with slightly different wording
3. a later session supersedes the stale claim with source evidence
4. a later private note contains sensitive context that must not promote to project memory
5. reflection runs automatically
6. recall returns one durable procedure, the new active claim, the decision, and the task learning
7. recall excludes duplicate, stale, sensitive, and superseded material
8. inspect shows the source chain and lifecycle reason for each automatic decision

## 8. Original Verification Matrix

Minimum commands from the original Reflection OS release-candidate plan:

```bash
moon run reflection-quality-gate
moon run autonomy-gate
moon run memory-trust-gate
moon run trust-control-gate
moon run auth-session-gate
moon run synthesis-gate
moon run adapter-ingest-gate
moon run overview-perf-gate
```

Packet-level checks should stay smaller:

```bash
moon run core:lint core:typecheck core:test
moon run api:lint api:typecheck api:test
moon run cli:lint cli:typecheck cli:test
moon run web:lint web:typecheck web:test
```

## 9. Original Release Exit Criteria

The original Reflection OS packet is complete when:

- `reflection-quality-gate` passes with all required surfaces covered
- automatic reflection has source IDs and audit receipts for every write
- correction and rollback fixtures prove stale, duplicate, wrong, sensitive, and superseded states
- dogfood recall quality improves after reflection
- existing trust, auth, overview, synthesis, and adapter gates still pass
- docs explain the automatic path and the exception-only review path

## 10. Original Recommendation

Make the original v0.12 packet the Reflection OS milestone.

This was the right ambitious block before v0.10 expanded because it reduced the largest remaining
human bottleneck while creating the evidence needed for source/synthesis scale and eventual Graphiti
deletion. It turns "memory review" from a queue into a maintenance loop with receipts.
