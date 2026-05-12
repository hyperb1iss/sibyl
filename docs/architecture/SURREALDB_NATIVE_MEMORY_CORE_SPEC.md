# Sibyl v0.7 Native Memory Core Spec

- Status: ready for implementation after cross-model spec review
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
- shipping graph-guided `synthesize`; that remains post-v0.7

## 4. Success Criteria

v0.7 is done when all of these are true:

- A seeded eval harness blocks regressions in source grounding, permission safety, latency, token
  budget, and task usefulness.
- A minimal memory policy primitive lands before native retrieval or native writes depend on scoped
  data. Wave 1 makes read checks concrete and adds default-deny write, share, and reflect decisions
  for scope crossings until Wave 5 expands them.
- A native retrieval path can build context packs from raw lexical search, graph full-text search,
  vector search, and graph neighborhood expansion.
- At least one production write path creates native entity, episode, and relationship records
  without Graphiti performing the write.
- Reflection can promote a raw session capture into native decisions, procedures, artifacts, tasks,
  and relationships with source links.
- Memory policy is centralized and used by recall, context, wake, remember, reflection, CLI
  commands, MCP tools, and API routes.
- Graphiti has a concrete removal inventory with default-loop call sites classified by behavior and
  either replaced, gated, or explicitly deferred.
- Pre-v0.7 Graphiti-written `Episodic` and `Entity` records that can be projected into explicit
  scope and source metadata remain queryable through native retrieval after the default-loop flip.
  Unprojectable records are inventoried and excluded rather than default-allowed.

## 5. Product Stories

### Coding Agent Handoff

An agent asks for project context before changing code. Sibyl returns active work, current
decisions, changed files, risks, and exact test evidence with source IDs. Private memories from
another principal do not appear. A delegated agent acting for Bliss may see her project-scoped
memory, but not her private memory unless the delegation explicitly grants the private scope.

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
2. Authorize the requested memory action, scope, and principal through the memory policy primitive.
3. Search raw sources and native graph records with scoped filters.
4. Fuse lexical, vector, graph, recency, and task-state signals.
5. Render a context pack with source, reason, visibility, freshness, and token budget metadata.
6. Reflect selected raw captures into native records.
7. Expose recall, remember, reflection, and promotion decisions for audit and tests.

### 6.2 Core Primitives

- `Principal`: discriminated union for user or agent identity. Agent principals carry both
  `principal_id` and `acting_for_user_id` / `agent_identity` so audit fields do not collapse the
  actor and the human being represented.
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
enough metadata for tests, structured logs, responses, and future audit storage.

### 6.4 Evaluation and Runtime Contracts

The v0.7 scoreboard uses concrete, testable metrics:

- Leak count is any rendered context-pack item whose source ID matches a fixture's
  `forbidden_source_ids`, or whose visible text contains a fixture's `forbidden_terms`.
- Source metadata coverage is
  `(items with non-empty source_id, visibility, reason, and freshness) / items`. Fixtures tagged
  `source-grounding` require coverage 1.0.
- Token estimates use a small estimator interface. Prefer an exact tokenizer when one is already
  available in the runtime. OpenAI-compatible budgets use `tiktoken` with the `cl100k_base` encoder
  when it is available; fall back to the evaluator's `characters / 4` estimate only when the report
  labels the method as approximate and keeps a 20% safety margin against the hard budget.
- Deterministic pass/fail results require a fixed embedder, fixed index build settings, and seeded
  fixtures. The seeded suite pins `SIBYL_EMBEDDER_MODEL`, embedding dimensions, index settings, and
  tokenizer revision in eval metadata and fails if the recorded runtime drifts. If a fixture sits
  inside a documented threshold tolerance band, revise the fixture instead of gating on it.
- Policy decisions in v0.7 surface in two places: structured server logs with `surface`,
  `principal_id`, `memory_scope`, `scope_key`, `reason`, and `action`; and per-item `policy_reason`
  metadata on rendered context packs and remember/reflect responses.
- Native vs. Graphiti retrieval is selected by `SIBYL_RETRIEVAL_MODE`: `graphiti` for the
  transitional fallback, `native` once the Wave 2 quality gate is green, and `compare` to run both,
  return native, and log diffs.
- Compare-mode diff logs are policy-safe: Graphiti results are filtered through the Wave 1 read
  policy before comparison, and diff records log source IDs, counts, and reason codes instead of raw
  memory text.

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
  `project-recall`, `delegated-recall`, `agent-diary`, `private-leak-negative`,
  `stale-decision-replacement`, and `source-grounding`.
- Require any compressed or summary text in `source-grounding` fixtures to cite raw or native source
  IDs before it can count as useful context.
- Add a pure read-side memory policy decision helper for private, project, and delegated scopes.
  API, CLI, and MCP surfaces can supply principal, agent, project, and membership context without
  owning the decision rules.
- Add `authorize_memory_write`, `authorize_memory_share`, and `authorize_memory_reflect` decisions
  with closed reason codes. Write and reflect decisions allow same-scope operations only when the
  caller supplies principal, scope, and verified membership context. Share remains deny-only until
  `memory_spaces` enables an explicit allowed case. The helpers deny scope crossings, missing scope
  keys, unverified membership, principal mismatches, missing agent identity, and disabled scopes
  with stable codes: `scope_not_enabled`, `missing_scope_key`, `unverified_membership`,
  `scope_crossing_requires_promotion`, `principal_mismatch`, and `agent_identity_required`. Wave 5
  expands allowed cases; Waves 3 and 4 must call the helpers instead of inventing local policy.
- Add hard gates for max estimated tokens, max latency, source metadata coverage, forbidden terms,
  and required scoped metadata.
- Add fixture data that includes private memories from another principal and confirms they are
  omitted.
- Make the token estimator explicit in reports, including the estimator method and any approximate
  safety margin.
- Measure p95 over 20 runs on the standard CI runner image and document the CI-specific threshold in
  `docs/testing/benchmark-methodology.md`. Until that measurement lands, CI uses a 3x local latency
  threshold.
- Keep `stale-decision-replacement` green with scripted superseded metadata on raw captures in
  Wave 1. Upgrade the fixture to exercise native `supersedes` relations when Wave 4 lands.
- Persist eval reports under `.moon/cache/evals` for local runs and expose a concise summary.

Acceptance:

- seeded context-pack eval pass rate is 1.0
- frozen suite membership is explicit and any new fixture is additive
- source metadata coverage is 1.0 for required cases
- forbidden private-memory fixtures produce 0 leaks
- `wake` stays under 1,200 estimated tokens
- `recall` stays under 2,000 estimated tokens unless the case opts into a higher limit
- local recall context-pack p95 stays under 1s for seeded fixtures, while CI uses the documented
  runner-specific gate
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
- Use the embedder already wired into the current Graphiti path and configured through
  `SIBYL_EMBEDDER_*`. Async embedding is post-v0.7 unless raw memory write p95 exceeds the Wave 1
  latency target.
- Fuse candidates with reciprocal rank fusion and lightweight boosts for source freshness, active
  task state, project match, and direct raw-source match.
- Start with explicit scoring defaults: reciprocal-rank-fusion `k = 60`; active task-state boost
  `1.3`; project-match boost `1.2`; direct raw-source match boost `1.4`; freshness boost capped at
  `1.5`. Changes after Milestone B require a fresh seeded-suite green run.
- Keep weak signals as boosts, not hard filters.
- Render context-pack item metadata with source ID, visibility, reason, freshness, and retrieval
  signals.
- Select the path through `SIBYL_RETRIEVAL_MODE`. The mode flips from `graphiti` to `native` only
  after `compare` mode runs in CI for three consecutive merged-to-main builds with zero
  policy-affecting diffs, recorded by `tools/inventory/retrieval_mode_history.py`. `compare` mode
  logs policy-safe native vs. Graphiti diffs while returning native results.
- Apply the Wave 1 read-side policy helper to Graphiti fallback results before comparison so
  compare-mode logs cannot expose text that native retrieval would have filtered.
- Define the filter-selectivity threshold for demoting vector-only candidates in
  `docs/testing/benchmark-methodology.md` before Wave 2 exit. The initial threshold is 0.1: when a
  filter retains less than 10% of the corpus, vector-only candidates are demoted unless the seeded
  recall fixture proves they are still useful.

Acceptance:

- native quality is proven when the frozen Wave 1 suite passes with source metadata coverage 1.0,
  leak count 0, local p95 latency under 1s, and no Graphiti node-hybrid search in the context-pack
  path
- the frozen Wave 1 suite has no new failures; new fixtures may be added only after the baseline
  remains green
- private/project/team scope filters apply before candidate rendering
- context packs expose source IDs for every required item
- graph-expanded results never bypass raw memory policy
- filtered vector search over a seeded multi-org fixture shows recall@20 within 10% of unfiltered
  top-K plus post-filtering, or the retrieval plan demotes vector-only candidates when filter
  selectivity exceeds a documented threshold
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
- Call the Wave 1 write and reflect policy helpers before any native derived record is written.
- Attach raw source IDs and provenance to every derived record.
- Keep Graphiti write behavior behind an explicit compatibility path for unported flows.
- Promote the W6 spike into a stable native memory contract test, then keep the spike test only as
  historical coverage if it still catches a different behavior.
- Gate native reflection writes behind `SIBYL_NATIVE_WRITE=enabled|disabled`, default disabled until
  Milestone C exit. Disabled mode falls back to the Graphiti compatibility write path. Rollback
  requires only the environment flag plus running the rebuild script against retained
  `raw_captures`.

Acceptance:

- reflection promotion writes native Surreal graph records without calling Graphiti `add_episode`
- records are visible through native retrieval and context packs
- frozen JSON snapshots cover `/api/context/pack`, `/api/memory/remember`, and `sibyl context` /
  `sibyl remember` response shapes; any field-shape change needs a documented migration
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
- Promote accepted candidates into native graph records only after the promotion request supplies
  the target scope and the memory policy helper allows the reflect action.
- Deny promotion when input raw captures span multiple memory scopes unless the caller passes
  `promote_to_scope` matching the broadest input scope and policy allows it.
- Mark superseded decisions and facts with the v0.7 `supersedes` relation when a newer source
  explicitly replaces them. Validity windows and bitemporal modeling are deferred.

Acceptance:

- reflection can turn a seeded session into native context-pack-ready records
- every derived record links back to at least one raw source
- a `post-reflection-recall` fixture passes using promoted native records with no raw-only shortcut
- candidates that would cross memory scopes require an explicit promotion policy decision
- scope-crossing promotion without an explicit `promote_to_scope` argument returns a stable deny
  reason
- mixed-scope reflection inputs return a stable deny reason unless the promotion target is explicit
  and allowed
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

- Expand the Wave 1 memory policy decision helper from read plus default-deny write/share/reflect
  checks into the full v0.7 policy contract.
- Use the helper in raw memory API routes, context routes, `wake`, MCP remember/recall flows, and
  reflection promotion. CLI commands must consume the same surfaced decisions from the API instead
  of duplicating policy logic.
- Keep diary entries private unless principal, agent identity, project, and memory-space policy all
  match.
- Return policy decision reasons in testable response and log metadata. Durable audit-event storage
  remains a follow-on unless Wave 5 explicitly adds the table and migrations.
- Keep shared, organization, and public scopes as stable enum values that return
  `denied: scope not enabled for v0.7` until `memory_spaces` enables them.

Acceptance:

- private memory cannot leak into project, team, organization, or public context packs
- keyed scopes require explicit scope keys and authorized membership
- diary recall requires agent identity and matching project filter when project-bound
- API, CLI, MCP, and context-pack tests prove the same policy outcomes
- policy decision reasons are asserted for at least one allow and one deny case per surface
- structured log fields for allow and deny decisions are asserted in API/context route tests

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
- Map pre-v0.7 Graphiti-written `Episodic` and `Entity` records into the native retrieval plan so
  existing memories remain queryable after the default-loop flip.
- Define a Graphiti-record projection that assigns explicit scope metadata before native retrieval:
  records with a project or source owner inherit that scoped owner; historical records without a
  recoverable owner become `memory_scope = organization`, `principal_id = null`, and `source_id`
  derived from the originating Graphiti episode UUID. Records that cannot be projected are excluded
  from native retrieval. Document the projection rule in the exit inventory.
- Keep historical migration and compatibility docs explicit.
- Expand `moon run inventory-check` and `tools/inventory/runtime_surface.py` so the generated
  runtime inventory still proves code reality while the hand-authored exit inventory carries removal
  intent.

Acceptance:

- no default `remember`, `recall`, `context`, or `reflect` path requires Graphiti
- Graphiti remains only in named compatibility or migration surfaces
- generated inventory and dependency files match the actual runtime
- every generated-inventory Graphiti import either has its own row in the hand-authored exit
  inventory, or is covered by a documented row that groups a named adapter package
- projectable pre-v0.7 Graphiti-written records are covered by the native projection inventory and
  remain queryable without reshaping them
- projected legacy records pass the `private-leak-negative` fixture in `compare` mode
- `inventory-check` fails if an unclassified default-loop Graphiti call site remains
- a no-Graphiti default-loop smoke test passes by blocking or monkeypatching Graphiti imports for
  `remember`, `recall`, `context`, `wake`, and `reflect`

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
- native writes and reflection promotion are gated by centralized policy decisions

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
- Context packs expose enough source and quality metadata for policy decision review.
- Reflection can propose a broader scope but cannot silently promote into it.
- Projectable pre-v0.7 Graphiti-written `Episodic` and `Entity` records remain queryable through
  native retrieval. v0.7 does not delete or reshape them; Wave 6 documents how they are projected
  into the native plan and how unprojectable records are inventoried and excluded.

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
- Disabled scopes: shared, organization, and public scope values stay in the API contract but return
  a stable `denied: scope not enabled for v0.7` reason until `memory_spaces` enables them.
- Policy decision metadata: v0.7 writes structured logs and response metadata; durable audit-event
  storage is post-v0.7 unless Wave 5 proves it is needed.
- Retrieval mode: `SIBYL_RETRIEVAL_MODE` owns the native/Graphiti switch with `graphiti`, `native`,
  and `compare` values.
- Temporal replacement: v0.7 ships a `supersedes` relation on native records with replacement
  reason, source ID, and `valid_from`. Validity windows and bitemporal modeling are post-v0.7.
- Native vector search: v0.7 uses the embedder already configured for the current Graphiti path. The
  filtered-vector recall benchmark decides whether vector-only candidates remain first-class.
- Promotion governance: scope-crossing reflection promotion requires an explicit `promote_to_scope`
  argument plus an allow decision from the policy helper. Human review UX can follow after v0.7.

## 13. Open Questions

- Should `memory_spaces` land before team/shared scope behavior, or can project/private scopes carry
  the first v0.7 release candidate?
- Should eval reports become CI artifacts immediately, or stay local until the harness stabilizes?

These questions should not block Milestone A. Wave 5 owns the `memory_spaces` answer before scope
expansion. Wave 1 owns the CI-artifact decision before the scoreboard becomes a release gate.

## 14. Recommendation

Start with Milestone A. Build the W2.5 scoreboard and minimal policy primitive first, then use them
to drive native retrieval and write-path replacement.

The first implementation task should be:

> Expand context-pack eval fixtures into a v0.7 scoreboard that measures source grounding,
> permission safety, latency, token budget, and usefulness for coding and personal-memory cases,
> with a minimal memory policy helper for scoped retrieval plus default-deny write, share, and
> reflect checks for later waves.

That gives every later Surreal-native change a measurable target and prevents the pure-Surreal push
from becoming a mechanical Graphiti deletion project. The first native production write path is
reflection promotion output, which lets raw capture stay stable while native derived records mature.
