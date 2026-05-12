# Sibyl v0.7 Native Memory Core Spec

- Status: ready for implementation
- Target release: v0.7
- Tracking epic: `epic_564b41ff89d6`
- Primary outcome: make `remember -> recall/context -> reflect` run on native SurrealDB primitives
  with measurable quality and policy safety.

This spec turns the post-v0.6.0 roadmap into an executable plan. It assumes SurrealDB is the default
runtime, legacy PostgreSQL and FalkorDB active surfaces are gone, and Graphiti-on-Surreal is
transition scaffolding rather than the northstar.

---

## 1. Goal

Ship the first pure-Surreal memory core:

- raw memory remains the source of truth for capture and provenance
- context packs become the user-facing retrieval product
- reflection creates native decisions, procedures, tasks, artifacts, and relationships
- policy is enforced before memory is selected, rendered, reflected, or shared
- Graphiti is removed from the default `remember`, `recall`, `context`, and `reflect` loops after
  native behavior is measurably better

The release is not "Graphiti rewritten in SurrealQL." It is a Sibyl-native memory system optimized
for agents that need precise, scoped, source-grounded context.

## 2. Why Now

v0.6.0 proved the Surreal default runtime and opened Phase 3. The next commits landed:

- SurrealDB server image pinned to `surrealdb/surrealdb:v3.0.5`
- Python SDK locked to `surrealdb==2.0.0`
- raw `remember` source capture for CLI and MCP
- raw memory latency and keyed-scope write guards
- direct SurrealQL spike covering raw memory, entity, episode, relationship, lexical search, vector
  search, graph traversal, and context-pack rendering

The system is ready for the larger move, but only if we install a quality scoreboard before
replacing more behavior.

## 3. Non-Goals

v0.7 should not attempt:

- full admin UI for policy or memory-space management
- arbitrary cross-org sharing
- bulk mailbox or archive ingest as the main product milestone
- deleting Graphiti before native retrieval, reflection, temporal behavior, and source-grounded
  summary behavior are measured
- replacing every MCP/API/CLI surface in one pass
- building custom role systems beyond the minimum memory-policy contract

## 4. Success Criteria

v0.7 is done when all of these are true:

- A seeded eval harness blocks regressions in source grounding, permission safety, latency, token
  budget, and task usefulness.
- A minimal read-side memory policy primitive lands before native retrieval depends on scoped data.
- A native retrieval path can build context packs from raw lexical search, graph full-text search,
  vector search, and graph neighborhood expansion.
- At least one production write path creates native entity, episode, and relationship records
  without Graphiti performing the write.
- Reflection can promote a raw session capture into native decisions, procedures, artifacts, tasks,
  and relationships with source links.
- Memory policy is centralized and used by recall, context, remember, reflection, CLI commands, MCP
  tools, and API routes.
- Graphiti has a concrete removal inventory with default-loop call sites classified by behavior and
  either replaced, gated, or explicitly deferred.

## 5. Product Stories

### Coding Agent Handoff

An agent asks for project context before changing code. Sibyl returns active work, current
decisions, changed files, risks, and exact test evidence with source IDs. Private memories from
another principal do not appear.

### Personal Memory Recall

An agent asks about a home or preference routine. Sibyl returns only memories visible to that
principal and memory space. Project or team work memories do not leak into the personal pack.

### Session Reflection

After a development session, Sibyl preserves the raw transcript, extracts durable decisions and
procedures, links every derived record to the raw source, and marks candidates that need review.

### Agent Diary

A named agent can write private diary notes for recurring project gotchas. Those notes appear in
`wake` or `recall` only when principal, agent identity, project, and memory-space scope allow it.

## 6. Target Architecture

### 6.1 Native Memory Pipeline

1. Capture source material into `RawMemory`.
2. Authorize the requested memory scope and principal through the read-side policy primitive.
3. Search raw sources and native graph records with scoped filters.
4. Fuse lexical, vector, graph, recency, and task-state signals.
5. Render a context pack with source, reason, visibility, freshness, and token budget metadata.
6. Reflect selected raw captures into native records.
7. Expose recall, remember, reflection, and promotion decisions for audit and tests.

### 6.2 Core Primitives

- `Principal`: user or delegated agent identity.
- `MemoryScope`: private, delegated, project, team, organization, shared, public.
- `MemorySpace`: policy boundary for scoped recall and promotion.
- `Source`: source adapter identity, source version, privacy class, and original metadata.
- `RawMemory`: verbatim capture with source ID, principal ID, scope, provenance, and capture time.
- `NativeEntity`: decision, procedure, task, artifact, claim, note, project, person, domain, or
  other typed memory object.
- `NativeEpisode`: session or event memory with source links and temporal metadata.
- `NativeRelationship`: typed edge with source links, confidence, validity, and supersession
  metadata.
- `ContextPack`: agent-facing selected memory bundle.
- `ReflectionCandidate`: extracted record awaiting policy, review, or promotion.
- `MemoryPolicyDecision`: allow/deny plus reason and scope source.

### 6.3 Surreal Tables and Relations

Existing tables stay in place for the first slice:

- `raw_captures`
- `entity`
- `episode`
- `relates_to`
- `mentions`

v0.7 may add:

- `memory_spaces`
- `sources`
- `reflection_candidates`
- `visibility_edges` or a relation equivalent for scoped sharing
- `supersedes` relation for decision and fact replacement

Do not add new tables until a test needs the behavior. Prefer using existing Surreal graph tables
for the first production write adapter.

A dedicated memory audit table is deferred beyond v0.7 unless Wave 5 proves it is needed for the
central policy contract. The release still requires policy decisions to carry reason strings and
enough metadata for tests, logs, and future audit storage.

## 7. Work Plan

### Wave 0 - Spec, Tracking, and Baseline

Tracking: `epic_564b41ff89d6`

Purpose: lock the contract before implementation starts.

Tasks:

- Create this spec and link it from `SIBYL_NORTHSTAR.md`.
- Create a Sibyl epic for v0.7 and child tasks for each wave.
- Keep `SURREALDB_PHASE3_BURNDOWN.md` focused on dependency deletion, not product behavior.

Verify:

- `moon run docs:lint`
- Sibyl task list shows the v0.7 epic and first implementation task.

### Wave 1 - W2.5 Evaluation Scoreboard

Tracking: `eef209f1-59ea-4a1c-a3bc-8fd871804d9d`

Purpose: make "better context" measurable before changing retrieval.

Files:

- `packages/python/sibyl-core/src/sibyl_core/evals/context.py` [expand]
- `packages/python/sibyl-core/src/sibyl_core/evals/runtime.py` [expand]
- `packages/python/sibyl-core/src/sibyl_core/auth/memory_policy.py` [create]
- `packages/python/sibyl-core/tests/test_context_pack_evals.py` [expand]
- `packages/python/sibyl-core/tests/test_evals_runtime.py` [expand]
- `packages/python/sibyl-core/tests/test_memory_policy.py` [create]
- `benchmarks/context_pack_eval.py` [expand]
- `docs/testing/benchmark-methodology.md` [expand]

Implementation:

- Freeze context-pack fixtures into named suites: `coding-handoff`, `personal-memory`,
  `project-recall`, `agent-diary`, `private-leak-negative`, `stale-decision-replacement`, and
  `source-grounding`.
- Require any compressed or summary text in `source-grounding` fixtures to cite raw or native source
  IDs before it can count as useful context.
- Add a pure read-side memory policy decision helper for private, project, and delegated scopes.
  API, CLI, and MCP surfaces can supply principal, agent, project, and membership context without
  owning the decision rules.
- Add hard gates for max estimated tokens, max latency, source metadata coverage, forbidden terms,
  and required scoped metadata.
- Add fixture data that includes private memories from another principal and confirms they are
  omitted.
- Make the token estimate explicit in reports. Until a tokenizer is introduced, use the evaluator's
  approximate `characters / 4` estimator and label it as approximate.
- Persist eval reports under `.moon/cache/evals` for local runs and expose a concise summary.

Acceptance:

- seeded context-pack eval pass rate is 1.0
- frozen suite membership is explicit and any new fixture is additive
- source metadata coverage is 1.0 for required cases
- forbidden private-memory fixtures produce 0 leaks
- `wake` stays under 1,200 estimated tokens
- `recall` stays under 2,000 estimated tokens unless the case opts into a higher limit
- local recall context-pack p95 stays under 1s for seeded fixtures
- the frozen suite can run twice locally with identical pass/fail outcomes

Verify:

- `moon run core:test`
- `moon run core:bench-context`
- `moon run core:lint core:typecheck`

### Wave 2 - W7 Native Retrieval Baseline

Tracking: `40eddb63-f40d-48be-a892-29920864a320`

Purpose: build a scoped retrieval plan that context packs can use without depending on Graphiti
hybrid search.

Files:

- `packages/python/sibyl-core/src/sibyl_core/tools/context.py` [expand]
- `packages/python/sibyl-core/src/sibyl_core/tools/search.py` [expand]
- `packages/python/sibyl-core/src/sibyl_core/retrieval/hybrid.py` [expand]
- `packages/python/sibyl-core/src/sibyl_core/retrieval/fusion.py` [expand]
- `packages/python/sibyl-core/src/sibyl_core/graph/search_interface.py` [expand]
- `packages/python/sibyl-core/tests/test_context_pack.py` [expand]
- `packages/python/sibyl-core/tests/test_context_pack_evals.py` [expand]
- `packages/python/sibyl-core/tests/graph/surreal/test_search_interface.py` [expand]

Implementation:

- Use the Wave 1 read-side policy helper before candidate search, ranking, rendering, or graph
  expansion.
- Define a native retrieval plan object that records requested facets, scopes, filters, candidate
  limits, and ranking weights.
- Pull candidates from raw lexical recall, entity/episode/edge full-text search, vector similarity,
  and graph neighborhood expansion.
- Fuse candidates with reciprocal rank fusion and lightweight boosts for source freshness, active
  task state, project match, and direct raw-source match.
- Keep weak signals as boosts, not hard filters.
- Render context-pack item metadata with source ID, visibility, reason, freshness, and retrieval
  signals.

Acceptance:

- native quality is proven when the frozen Wave 1 suite passes with source metadata coverage 1.0,
  leak count 0, local p95 latency under 1s, and no Graphiti node-hybrid search in the context-pack
  path
- the frozen Wave 1 suite has no new failures; new fixtures may be added only after the baseline
  remains green
- private/project/team scope filters apply before candidate rendering
- context packs expose source IDs for every required item
- graph-expanded results never bypass raw memory policy
- current Graphiti-backed path remains available as fallback until the native quality gate above is
  met

Verify:

- `moon run core:test`
- `moon run core:bench-context`
- `moon run core:lint core:typecheck`

### Wave 3 - Production Native Write Adapter

Tracking: `d4ee14c4-cea0-4c77-9d56-34f59ad966a1`

Purpose: turn the W6 spike into one real production write path.

Files:

- `packages/python/sibyl-core/src/sibyl_core/graph/entities.py` [expand]
- `packages/python/sibyl-core/src/sibyl_core/services/graph_runtime.py` [expand]
- `packages/python/sibyl-core/src/sibyl_core/tools/add.py` [expand]
- `apps/api/src/sibyl/jobs/entities.py` [expand]
- `packages/python/sibyl-core/tests/graph/surreal/test_native_memory_contract.py` [create]
- `packages/python/sibyl-core/tests/graph/surreal/test_native_memory_spike.py` [refactor]
- `packages/python/sibyl-core/tests/test_graph_entities.py` [expand]
- `apps/api/tests/test_jobs_entities.py` [expand]

Implementation:

- Add a native write service for the first selected flow: reflection promotion output.
- Promote accepted `ReflectionCandidate` records into native entity, episode, and relationship
  records directly through Surreal operations. The `remember` hot path continues to preserve raw
  source capture first.
- Attach raw source IDs and provenance to every derived record.
- Keep Graphiti write behavior behind an explicit compatibility path for unported flows.
- Promote the W6 spike into a stable native memory contract test, then keep the spike test only as
  historical coverage if it still catches a different behavior.
- Add a feature flag only if needed for rollback. Rollback must preserve raw captures and allow
  derived native records to be deleted or rebuilt from source IDs.

Acceptance:

- reflection promotion writes native Surreal graph records without calling Graphiti `add_episode`
- records are visible through native retrieval and context packs
- current API and CLI response contracts do not regress
- `test_native_memory_contract.py` is the minimum integration fixture, not a one-off proof

Verify:

- `moon run core:test`
- `moon run api:test -- tests/test_jobs_entities.py tests/test_routes_entities.py`
- `moon run cli:test`
- `moon run :check`

### Wave 4 - W8 Reflection MVP

Tracking: `8ea4beab-04ab-4e5a-9cbb-5143fcf6b067`

Purpose: make raw captures become durable native memory.

Files:

- `packages/python/sibyl-core/src/sibyl_core/tools/reflect.py` [expand]
- `packages/python/sibyl-core/src/sibyl_core/models/reflection.py` [expand]
- `packages/python/sibyl-core/src/sibyl_core/services/surreal_content.py` [expand]
- `packages/python/sibyl-core/tests/test_reflect.py` [expand]
- `apps/api/src/sibyl/api/routes/memory.py` [expand]
- `apps/api/tests/test_routes_memory.py` [expand]

Implementation:

- Preserve raw source material before any extraction.
- Extract candidate decisions, procedures, plans, tasks, artifacts, claims, and relationships.
- Store candidates with raw source IDs, extraction prompt metadata, confidence, review state, and
  suggested memory scope.
- Promote accepted candidates into native graph records.
- Mark superseded decisions and facts when a newer source explicitly replaces them.

Acceptance:

- reflection can turn a seeded session into native context-pack-ready records
- every derived record links back to at least one raw source
- a `post-reflection-recall` fixture passes using promoted native records with no raw-only shortcut
- candidates that would cross memory scopes require an explicit promotion policy decision
- rejected or deferred candidates remain auditable

Verify:

- `moon run core:test`
- `moon run api:test -- tests/test_routes_memory.py`
- `moon run core:lint core:typecheck`

### Wave 5 - W3/W9 Memory Policy Backbone

Tracking: `9128418b-42c9-4d89-9db6-800271098f9e`

Purpose: centralize the authorization contract for memory operations.

Files:

- `packages/python/sibyl-core/src/sibyl_core/auth/context.py` [expand]
- `packages/python/sibyl-core/src/sibyl_core/auth/memory_policy.py` [expand after Wave 1]
- `packages/python/sibyl-core/src/sibyl_core/services/surreal_content.py` [expand]
- `apps/api/src/sibyl/auth/authorization.py` [expand]
- `apps/api/src/sibyl/api/routes/memory.py` [expand]
- `apps/api/src/sibyl/api/routes/context.py` [expand]
- `apps/api/src/sibyl/server.py` [expand]
- `apps/cli/src/sibyl_cli/main.py` [expand]
- `apps/cli/src/sibyl_cli/client.py` [expand]
- `apps/api/tests/test_routes_memory.py` [expand]
- `apps/api/tests/test_routes_context.py` [expand]
- `apps/api/tests/test_server_accessible_projects.py` [expand]
- `apps/cli/tests/test_context_pack.py` [expand]

Implementation:

- Expand the Wave 1 memory policy decision helper from read-only checks into read, write, share, and
  reflect decisions.
- Use the helper in raw memory API routes, context routes, MCP remember/recall flows, and reflection
  promotion. CLI commands must consume the same surfaced decisions from the API instead of
  duplicating policy logic.
- Keep diary entries private unless principal, agent identity, project, and memory-space policy all
  match.
- Return policy decision reasons in testable response or log metadata. Durable audit-event storage
  remains a follow-on unless Wave 5 explicitly adds the table and migrations.

Acceptance:

- private memory cannot leak into project, team, organization, or public context packs
- keyed scopes require explicit scope keys and authorized membership
- diary recall requires agent identity and matching project filter when project-bound
- API, CLI, MCP, and context-pack tests prove the same policy outcomes
- policy decision reasons are asserted for at least one allow and one deny case per surface

Verify:

- `moon run api:test -- tests/test_routes_memory.py tests/test_routes_context.py`
- `moon run api:test -- tests/test_server_accessible_projects.py`
- `moon run core:test`
- `moon run cli:test`
- `moon run :check`

### Wave 6 - Graphiti Exit Inventory

Tracking: `649eb71b-0fd6-4c32-bc14-77d5fd12dc7d`

Purpose: make deletion boring.

Files:

- `docs/architecture/SURREALDB_GRAPHITI_EXIT_INVENTORY.md` [create]
- `docs/research/rust-port/INVENTORY.md` [regenerate]
- `docs/architecture/SURREALDB_PHASE3_BURNDOWN.md` [expand]
- `tools/inventory/runtime_surface.py` [expand]
- `tools/tests/test_runtime_surface.py` [expand]
- `packages/python/sibyl-core/src/sibyl_core/graph/client.py` [refactor]
- `packages/python/sibyl-core/src/sibyl_core/graph/entities.py` [refactor]
- `apps/api/src/sibyl/jobs/entities.py` [refactor]
- `apps/api/src/sibyl/persistence/legacy/graph.py` [refactor]
- `packages/python/sibyl-core/pyproject.toml` [refactor]

Implementation:

- Inventory every Graphiti dependency by behavior: extraction, duplicate detection, write path,
  search, temporal model, summaries, communities, and compatibility adapters.
- Add a hand-authored Graphiti exit inventory for behavior, call site, status, default-loop usage,
  removal condition, owner, and verification command.
- Mark each call site as replaced, fallback, or retained with a removal condition.
- Delete Graphiti from the default memory loop only after native evals pass.
- Keep historical migration and compatibility docs explicit.
- Expand `moon run inventory-check` and `tools/inventory/runtime_surface.py` so the generated
  runtime inventory still proves code reality while the hand-authored exit inventory carries removal
  intent.

Acceptance:

- no default `remember`, `recall`, `context`, or `reflect` path requires Graphiti
- Graphiti remains only in named compatibility or migration surfaces
- generated inventory and dependency files match the actual runtime
- hand-authored exit inventory has a row for every generated Graphiti import file or explicitly
  groups generated adapter files under one removal condition
- `inventory-check` fails if an unclassified default-loop Graphiti call site remains
- a no-Graphiti default-loop smoke test passes by blocking or monkeypatching Graphiti imports for
  `remember`, `recall`, `context`, and `reflect`

Verify:

- `moon run inventory-check`
- `moon run core:test`
- `moon run api:test`
- `moon run :check`

## 8. Milestones

### Milestone A - Quality Gate Installed

Includes Wave 0 and Wave 1. This is the first work to land.

Exit criteria:

- spec committed
- v0.7 epic/tasks created
- eval scoreboard blocks seeded leaks and token/latency regressions

### Milestone B - Native Context Path

Includes Wave 2.

Exit criteria:

- context packs can use native retrieval without Graphiti hybrid search
- seeded evals prove source grounding and policy safety

### Milestone C - Native Write and Reflect

Includes Wave 3 and Wave 4.

Exit criteria:

- one production flow writes native graph records
- reflection promotes raw captures into native records with source links

### Milestone D - Default Loop Cleanup

Includes Wave 5 and Wave 6.

Exit criteria:

- policy is centralized
- Graphiti is out of the default memory loop or every remaining default dependency has an explicit
  blocker and owner

## 9. Verification Matrix

The core and CLI test tasks are package-level gates today because their Moon task commands already
include `tests/`. If a later wave needs a narrower blocking gate, add a named Moon task instead of
depending on passthrough test paths.

| Surface             | Per-wave gate                                                                   | Release gate                          |
| ------------------- | ------------------------------------------------------------------------------- | ------------------------------------- |
| Core evals          | `moon run core:test` plus `moon run core:bench-context`                         | frozen suite pass rate 1.0            |
| Native graph        | `moon run core:test`                                                            | no default Graphiti write/search path |
| Reflection          | `moon run core:test`                                                            | `post-reflection-recall` green        |
| API memory/context  | `moon run api:test -- tests/test_routes_memory.py tests/test_routes_context.py` | policy allow and deny reasons covered |
| CLI capture/context | `moon run cli:test`                                                             | CLI consumes API policy outcomes      |
| Inventory           | `moon run inventory-check`                                                      | unclassified default-loop matches 0   |
| Docs                | `moon run docs:lint`                                                            | spec, northstar, and burndown agree   |
| Release confidence  | wave gates plus `moon run :check`                                               | GitHub CI green                       |

## 10. Data and Policy Invariants

- Raw source material is written before extraction, embedding, reflection, or graph traversal.
- Every derived record has at least one source ID.
- Organization and memory-space filters are applied before ranking and rendering.
- Private memories are principal-bound.
- Project memories require project membership.
- Team, delegated, shared, organization, and public scopes need explicit policy before expansion.
- Agent diary recall requires a named agent and never defaults into shared memory.
- Context packs explain why an item was included.
- Context packs expose enough source and quality metadata for audit.
- Reflection can propose a broader scope but cannot silently promote into it.

## 11. Risks

- Eval fixtures may become too synthetic. Mitigation: include dogfood fixtures from real Sibyl work
  and keep adding regression cases from incidents.
- Native retrieval may look worse at first because Graphiti has implicit summarization behavior.
  Mitigation: compare by context-pack usefulness, not raw search overlap.
- Policy complexity can sprawl. Mitigation: centralize allow/deny decisions and make every denial
  explainable.
- Direct SurrealQL write adapters may duplicate Graphiti assumptions. Mitigation: model Sibyl
  primitives first, then map old behaviors only where tests require them.
- Full Graphiti removal may uncover hidden temporal and duplicate-detection dependencies.
  Mitigation: inventory by behavior before deleting dependencies.

## 12. Resolved Decisions

- First production write path: reflection promotion output. `remember` remains raw capture first
  until the native write adapter proves derived record quality.
- Policy ownership: core owns the decision model and reason strings. API, CLI, and MCP surfaces
  supply principal, project, agent, and membership context.
- Initial policy scope: private, project, and delegated scopes are required for Milestone A and
  Milestone B. Team, shared, organization, and public expansion can wait for `memory_spaces`.
- Audit storage: dedicated durable audit events are post-v0.7 unless Wave 5 proves they are needed.
  Policy decision metadata still has to be testable and loggable in v0.7.

## 13. Open Questions

- Should `memory_spaces` land before team/shared scope behavior, or can project/private scopes carry
  the first v0.7 release candidate?
- What is the minimum useful temporal replacement for Graphiti: validity fields, supersession edges,
  or both?
- Should eval reports become CI artifacts immediately, or stay local until the harness stabilizes?

These questions should not block Milestone A. They become blocking before Milestone C if they affect
promotion semantics or release confidence.

## 14. Recommendation

Start with Milestone A. Build the W2.5 scoreboard and read-side policy primitive first, then use
them to drive native retrieval and write-path replacement.

The first implementation task should be:

> Expand context-pack eval fixtures into a v0.7 scoreboard that measures source grounding,
> permission safety, latency, token budget, and usefulness for coding and personal-memory cases,
> with a minimal read-side memory policy helper for scoped retrieval.

That gives every later Surreal-native change a measurable target and prevents the pure-Surreal push
from becoming a mechanical Graphiti deletion project. The first native production write path is
reflection promotion output, which lets raw capture stay stable while native derived records mature.
