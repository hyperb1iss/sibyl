# Sibyl v0.8 Pure Surreal Closure Execution Plan

- Status: closed; superseded by the parent A6/B6 release receipts
- Parent plan: `docs/architecture/SIBYL_V08_PURE_SURREAL_CLOSURE_AND_MEMORY_TRUST_PLAN.md`
- Tracking epic: Pure Surreal Closure, `epic_416f955f7f39`
- Source inventory: `docs/architecture/SURREALDB_GRAPHITI_EXIT_INVENTORY.md`
- Source burndown: `docs/architecture/SURREALDB_PHASE3_BURNDOWN.md`
- Current release evidence baseline: `4855ba8a`

This document was the working plan for the v0.8 pure Surreal closure work. The parent v0.8 plan now
owns the release evidence and the Memory Trust Foundation receipts. This child plan remains as the
historical Track A execution record for native graph managers, native embeddings, Graphiti
compatibility disposition, archive policy, stale docs, and the final Surreal-only release audit.

The release goal is simple: the default Sibyl runtime should be SurrealDB-only, with Graphiti,
FalkorDB, PostgreSQL, and Redis/Valkey absent from the default data plane. Any retained legacy
behavior must be opt-in, named honestly, separately tested, and documented as compatibility,
migration, admin, benchmark, or historical archive support.

## 1. Current State

Closed as of the 2026-05-14 release evidence refresh:

- Pushed `main` release baseline: `4855ba8a`.
- Main CI run `25870913035` completed successfully on `4855ba8a`.
- Docs deploy run `25877971558` completed successfully on `4855ba8a`.
- Nightly regression run `25877971585` completed successfully on `4855ba8a`.
- The parent v0.8 plan is authoritative for the final release recommendation and residual risks.

Completed as of `543963f3`:

- Default tests do not collect the named Graphiti compatibility files.
- Mixed Graphiti-dependent tests are marked with `graphiti_compatibility`.
- `moon run graphiti-compatibility-test` runs the opt-in compatibility island.
- `moon run core:no-graphiti-smoke` proves default importability without Graphiti.
- The generated inventory still owns every retained Graphiti import path.
- Default dependency metadata keeps `graphiti-core` outside normal runtime dependencies.

Recent receipts:

- `moon run core:test`: 873 passed, 14 skipped, 20 deselected.
- `moon run api:test`: 1396 passed, 1 skipped, 16 deselected.
- `moon run inventory-lint inventory-test`: 18 passed.
- `moon run graphiti-compatibility-test`: passed.
- `moon run core:no-graphiti-smoke`: 2 passed.
- `moon run :check`: 34 completed, 20 cached.
- `uv lock --check`: resolved successfully.
- Claude review for the compatibility test island: PASS.

Known constraints:

- Track B project RBAC hardening remains a release dependency for leak-safe default reads. Track A
  can build native hydration now, but release claims should wait for the B2 project-filtering gate.
- Compatibility code can remain during v0.8 only when it is opt-in and covered by a named test task.
- Benchmark claims must point to gated artifacts. Planned benchmark coverage is not release
  evidence.
- Historical archive support can stay, but default commands must not discover or depend on ambient
  PostgreSQL, FalkorDB, or Redis data services.

## 2. Release Outcomes

v0.8 pure Surreal closure is complete when:

- Default API boot, CLI boot, MCP import, prompt hooks, context packs, recall, wake, reflect, task
  workflows, jobs, and native retrieval do not import Graphiti.
- Default entity hydration uses Surreal rows and native normalization helpers, not `EntityNode`,
  `EpisodicNode`, or other Graphiti model classes.
- Default relationship traversal uses native `relates_to`, `mentions`, and temporal relationship
  records.
- Native graph writes preserve source IDs, project policy fields, confidence, validity intervals,
  provenance, and timestamps.
- Native embedding ownership covers provider selection, cache keys, model metadata, dimensions,
  tokenizer estimate metadata, vector writes, and vector search.
- Retained Graphiti modules live behind one named compatibility boundary and are tested only by
  explicit compatibility tasks.
- Archive import and restore flows are explicit, file-based, dry-run friendly, and labeled as
  Surreal-native or historical migration behavior.
- Active docs, compose files, CI, and charts describe a Surreal-only default runtime.
- Release notes can cite current local gates, CI run IDs, docs deploy run IDs, nightly run IDs, and
  benchmark artifacts from the final tree.

## 3. Non-Goals

- Do not build the post-v0.8 synthesis engine in this track.
- Do not delete archive history before restore policy is explicit and tested.
- Do not remove compatibility tests that still prove a supported migration or benchmark surface.
  Move them into the compatibility island instead.
- Do not treat Redis/Valkey as a data-plane dependency. It may remain only as explicit coordination
  infrastructure.
- Do not make public Graphiti or AI-memory benchmark comparisons without gated, citable artifacts.

## 4. Execution Order

The dependency shape is:

1. A2 native graph manager replacement.
2. A3 native embedding ownership and benchmark metadata.
3. A4 Graphiti operations disposition.
4. A5 archive, coordination, docs, compose, and chart cleanup.
5. A6 final release audit.

A2 can start now. A3 can run partly in parallel after the native hydration contract is stable. A4
should wait until A2 and A3 have removed active default callers. A5 waits for A4 policy decisions.
A6 waits for all Track A work and the B6 memory trust gate if v0.8 ships both tracks together.

## 5. Wave A2: Native Graph Manager Replacement

Purpose: replace Graphiti-shaped graph manager APIs in default runtime paths with native Surreal row
hydration, relationship traversal, and temporal reads.

### A2.1 Native Entity Hydration

Implement:

- Add explicit native row hydration helpers in
  `packages/python/sibyl-core/src/sibyl_core/services/native_graph.py`.
- Preserve legacy row compatibility without requiring Graphiti model classes.
- Normalize IDs from `uuid`, legacy `entity_id`, and safe legacy `id` forms.
- Preserve Surreal record IDs as metadata when they are not entity IDs.
- Resolve entity type from top-level `entity_type`, legacy attributes, and labels.
- Preserve `project_id`, `epic_id`, `task_id`, `status`, `priority`, `complexity`, `feature`,
  `tags`, source IDs, confidence, validity intervals, provenance fields, created/modified actors,
  and timestamps.
- Update API graph runtime entity coercion so native mappings do not require compatibility manager
  methods.

Files:

- `packages/python/sibyl-core/src/sibyl_core/services/native_graph.py`
- `packages/python/sibyl-core/src/sibyl_core/retrieval/native.py`
- `apps/api/src/sibyl/persistence/graph_runtime.py`
- `packages/python/sibyl-core/tests/test_native_graph.py`
- `apps/api/tests/test_routes_entities.py`
- `apps/api/tests/test_routes_entities_read.py`

Verify:

- `moon run core:test -- tests/test_native_graph.py`
- `moon run api:test -- tests/test_routes_entities.py tests/test_routes_entities_read.py`
- `moon run core:no-graphiti-smoke`
- `moon run core:lint`
- `moon run api:lint`

Exit criteria:

- Default entity reads do not import Graphiti node classes.
- Native and legacy-shaped rows hydrate with preserved policy and provenance metadata.
- The historical `Procedure.category = None` coercion class is covered by a native normalization
  fixture.

### A2.2 Native Relationship And Temporal Reads

Implement:

- Move relationship hydration to native `relates_to`, `mentions`, and temporal relationship records.
- Preserve relationship source IDs, confidence, validity intervals, provenance, direction,
  relationship type, and project scope.
- Cover traverse, related summary, dependency, search, context, and timeline reads using native
  fixtures.
- Keep archive compatibility conversion helpers explicit and out of default reads.

Files:

- `packages/python/sibyl-core/src/sibyl_core/services/native_graph.py`
- `packages/python/sibyl-core/src/sibyl_core/retrieval/native.py`
- `packages/python/sibyl-core/src/sibyl_core/graph/relationships.py`
- `packages/python/sibyl-core/tests/test_native_graph.py`
- `packages/python/sibyl-core/tests/test_native_retrieval.py`
- `apps/api/tests/test_routes_search.py`
- `apps/api/tests/test_routes_context.py`

Verify:

- `moon run core:test -- tests/test_native_graph.py tests/test_native_retrieval.py`
- `moon run api:test -- tests/test_routes_search.py tests/test_routes_context.py`
- `moon run core:no-graphiti-smoke`

Exit criteria:

- Default relationship paths do not import Graphiti edge classes.
- Temporal relationship behavior remains covered by native Surreal fixtures.

### A2.3 Graph Runtime Facade Cleanup

Implement:

- Remove Graphiti edge error handling from default API graph runtime paths.
- Ensure native managers expose the methods the API graph store needs, instead of relying on
  compatibility-manager method names.
- Keep admin, migration, and compare surfaces explicit when they still need the compatibility
  manager.
- Add inventory coverage if default-path imports can drift.

Files:

- `apps/api/src/sibyl/persistence/graph_runtime.py`
- `apps/api/tests/test_graph_entities.py`
- `apps/api/tests/test_graph_relationships.py`
- `tools/inventory/runtime_surface.py`
- `tools/tests/test_runtime_surface.py`
- `docs/architecture/SURREALDB_GRAPHITI_EXIT_INVENTORY.md`

Verify:

- `moon run api:test -- tests/test_graph_entities.py tests/test_graph_relationships.py`
- `moon run inventory-check inventory-typecheck inventory-test`
- `moon run core:no-graphiti-smoke`

Exit criteria:

- API graph runtime resolves native graph managers by default.
- Compatibility manager use is admin, migration, compare, or test-only.

## 6. Wave A3: Native Embedding Ownership

Purpose: make embedding behavior a Sibyl-native service instead of a Graphiti adapter contract.

### A3.1 Native Embedding Contract

Implement:

- Define a native embedding provider interface with async batch support.
- Support deterministic test embeddings without Graphiti test utilities.
- Represent provider, model, dimensions, cache namespace, and tokenizer estimate method in a native
  metadata object.
- Route retrieval code through the native contract.

Files:

- `packages/python/sibyl-core/src/sibyl_core/retrieval/native.py`
- `packages/python/sibyl-core/src/sibyl_core/services/native_graph.py`
- `packages/python/sibyl-core/tests/test_native_retrieval.py`
- `packages/python/sibyl-core/tests/test_native_graph.py`

Verify:

- `moon run core:test -- tests/test_native_retrieval.py tests/test_native_graph.py`
- `moon run core:no-graphiti-smoke`

Exit criteria:

- Native retrieval and vector writes do not require Graphiti embedder interfaces.

### A3.2 Provider And Cache Migration

Implement:

- Move Gemini, OpenAI, and cached embedding behavior behind native providers.
- Keep Graphiti-compatible embedders in the compatibility island until A4 decides whether to delete
  or retain them.
- Ensure cache keys include provider, model, dimensions, text version, and any normalization setting
  that affects vector output.
- Preserve deterministic metadata in eval artifacts.

Files:

- `packages/python/sibyl-core/src/sibyl_core/graph/cached_embedder.py`
- `packages/python/sibyl-core/src/sibyl_core/graph/gemini_embedder.py`
- `packages/python/sibyl-core/src/sibyl_core/graph/client.py`
- `packages/python/sibyl-core/src/sibyl_core/retrieval/native.py`
- `packages/python/sibyl-core/tests/test_graph_client.py`
- `packages/python/sibyl-core/tests/test_native_retrieval.py`

Verify:

- `moon run core:test -- tests/test_graph_client.py tests/test_native_retrieval.py`
- `moon run core:no-graphiti-smoke`

Exit criteria:

- Default embedding setup is native.
- Compatibility embedders are isolated and removable.

### A3.3 Benchmark Metadata Gate

Implement:

- Require retrieval mode, provider, model, dimensions, tokenizer method, dataset name, corpus hash,
  repeat count, auth manifest ID, commit, and runtime mode in citable benchmark artifacts.
- Separate pre-Graphiti, post-Graphiti, native, and compare labels so reports do not mix
  incompatible runs.
- Document which artifact classes are release-citable and which are planned only.

Files:

- `benchmarks/context_pack_eval.py`
- `benchmarks/context_pack_cases.json`
- `benchmarks/ai_memory/**`
- `benchmarks/results/ai-memory/manifest.json`
- `docs/testing/benchmark-methodology.md`
- `moon.yml`

Verify:

- `moon run core:bench-context -- --cases benchmarks/context_pack_cases.json --auth-manifest .moon/cache/baseline-runtime-manifest.json --label retrieval-compare --repeat 20 --metadata retrieval_mode=compare`
- `moon run bench-gate`
- `moon run docs:lint`

Exit criteria:

- Underspecified benchmark outputs fail the gate.
- Every release benchmark claim can point to a gated artifact.

## 7. Wave A4: Graphiti Operations Disposition

Purpose: delete unneeded Graphiti operations modules or move retained code into a named
compatibility namespace.

### A4.1 Ops Classification Pass

Implement:

- Audit every file under `packages/python/sibyl-core/src/sibyl_core/graph/surreal/compat/ops/`.
- Classify each module as delete, migrate-to-native, compatibility-retain, admin-only,
  benchmark-only, or historical migration.
- Record the owner, removal condition, and verification task for every retained module.

Files:

- `packages/python/sibyl-core/src/sibyl_core/graph/surreal/compat/ops/**`
- `docs/architecture/SURREALDB_GRAPHITI_EXIT_INVENTORY.md`

Verify:

- `moon run inventory-check inventory-typecheck inventory-test`

Exit criteria:

- No Graphiti ops module remains unclassified.

### A4.2 Move Or Delete Ops Code

Implement:

- Delete modules with no retained purpose.
- Move retained modules under a named compatibility namespace.
- Update import paths for compatibility tests, migration commands, and compare tools.
- Remove stale comments that imply Graphiti is active default runtime.

Files:

- `packages/python/sibyl-core/src/sibyl_core/graph/surreal/compat/ops/**`
- `packages/python/sibyl-core/src/sibyl_core/graph/**`
- `packages/python/sibyl-core/tests/**`
- `apps/api/tests/**`

Verify:

- `moon run graphiti-compatibility-test`
- `moon run core:no-graphiti-smoke`
- `moon run core:test`
- `moon run api:test`

Exit criteria:

- Default paths do not import moved compatibility modules.
- Compatibility tests still cover retained behavior explicitly.

### A4.3 Dependency Boundary Check

Implement:

- Confirm `graphiti-core` remains absent from default runtime dependencies.
- Confirm compatibility extras install everything needed for retained Graphiti surfaces.
- Add or update no-Graphiti import smoke coverage for API, CLI, MCP, jobs, prompt hooks, and native
  retrieval.

Files:

- `packages/python/sibyl-core/pyproject.toml`
- `uv.lock`
- `moon.yml`
- `packages/python/sibyl-core/tests/test_no_graphiti_default_loop.py`
- `tools/inventory/runtime_surface.py`
- `tools/tests/test_runtime_surface.py`

Verify:

- `uv lock --check`
- `moon run core:no-graphiti-smoke`
- `moon run inventory-check inventory-typecheck inventory-test`
- `moon run :check`

Exit criteria:

- A default install works without Graphiti installed.
- Compatibility install remains deliberate and tested.

## 8. Wave A5: Archive, Coordination, Docs, And Deployment Cleanup

Purpose: make legacy service behavior explicit and remove stale instructions from the default user
path.

### A5.1 Archive And Restore Policy

Implement:

- Make archive import and restore commands require explicit source files, source type, target mode,
  and dry-run review before writes.
- Label PostgreSQL and FalkorDB payloads as historical migration inputs.
- Ensure Surreal-native backup and restore are the default documented path.
- Report unsupported or ignored payloads before any restore writes.

Files:

- `apps/api/src/sibyl/cli/migrate.py`
- `apps/api/src/sibyl/jobs/backup.py`
- `packages/python/sibyl-core/src/sibyl_core/migrate/archive.py`
- `apps/api/tests/test_migrate.py`
- `packages/python/sibyl-core/tests/test_archive_migration.py`
- `docs/guide/surrealdb-migration-release-notes.md`
- `docs/architecture/SURREALDB_PHASE3_BURNDOWN.md`

Verify:

- `moon run api:test -- tests/test_migrate.py`
- `moon run core:test -- tests/test_archive_migration.py`
- `moon run docs:lint`

Exit criteria:

- Default recovery docs are Surreal-native.
- Historical imports cannot run from ambient legacy services.

### A5.2 Stale Docs And Deployment Sweep

Implement:

- Audit active docs, compose files, CI, and charts for stale default-runtime references to
  PostgreSQL, FalkorDB, Redis/Valkey, and Graphiti.
- Keep retained legacy terms only in historical, migration, benchmark, compatibility, or
  coordination sections.
- Add an allowlist-backed check if active docs can drift back toward legacy service defaults.

Files:

- `README.md`
- `apps/api/README.md`
- `apps/cli/README.md`
- `apps/web/README.md`
- `docs/**`
- `docker-compose*.yml`
- `compose.e2e.yml`
- `.github/workflows/**`
- `charts/**`
- `tools/inventory/runtime_surface.py`
- `tools/tests/test_runtime_surface.py`

Verify:

- `rg -n "postgres|falkor|redis|valkey|Graphiti|graphiti" README.md apps docs docker-compose*.yml compose.e2e.yml .github charts`
- `moon run inventory-check inventory-typecheck inventory-test`
- `moon run docs:lint`
- `moon run :check`

Exit criteria:

- A new user following active docs starts a Surreal-only default stack.
- Retained legacy references have explicit owners and reasons.

## 9. Wave A6: Final Pure Surreal Release Audit

Purpose: produce the release recommendation from current evidence instead of memory.

Implement:

- Run the full local release gate from a clean worktree or clean checkout.
- Confirm dependency metadata excludes Graphiti, FalkorDB, PostgreSQL, and Redis/Valkey as default
  data-plane requirements.
- Confirm compatibility, migration, benchmark, admin, and historical archive surfaces are named and
  separately tested.
- Record final local receipts, CI run IDs, docs deploy run IDs, nightly run IDs, and benchmark
  artifact paths.
- Write the binary recommendation: ship or hold.

Files:

- `docs/architecture/SIBYL_V08_PURE_SURREAL_CLOSURE_AND_MEMORY_TRUST_PLAN.md`
- `docs/architecture/SIBYL_V08_PURE_SURREAL_CLOSURE_EXECUTION_PLAN.md`
- `docs/architecture/SURREALDB_GRAPHITI_EXIT_INVENTORY.md`
- `docs/architecture/SURREALDB_PHASE3_BURNDOWN.md`
- release notes draft

Verify:

- `moon run inventory-check inventory-typecheck inventory-test`
- `moon run core:no-graphiti-smoke`
- `moon run memory-trust-gate`
- `moon run core:test`
- `moon run api:test`
- `moon run cli:test`
- `moon run docs:lint`
- `moon run :check`
- `moon run baseline-seed`
- `moon run baseline-replay-runtime`
- `moon run bench-gate`
- `moon run core:bench-context -- --cases benchmarks/context_pack_cases.json --auth-manifest .moon/cache/baseline-runtime-manifest.json --label retrieval-compare --repeat 20 --metadata retrieval_mode=compare`
- CI green on `main`
- docs deploy green on `main`
- nightly regression green on `main`

Exit criteria:

- v0.8 can claim a Surreal-only default runtime.
- Retained compatibility or historical surfaces are opt-in, named, documented, and separately
  tested.
- Release notes have evidence, not inherited assumptions.

## 10. Verification Matrix

| Gate                                                          | Proves                                                | Required Before              |
| ------------------------------------------------------------- | ----------------------------------------------------- | ---------------------------- |
| `moon run core:no-graphiti-smoke`                             | Default imports work without Graphiti                 | Every wave                   |
| `moon run graphiti-compatibility-test`                        | Retained Graphiti behavior still works explicitly     | A4, A6                       |
| `moon run inventory-check inventory-typecheck inventory-test` | Inventory and allowlists fail closed                  | A2.3, A4, A5, A6             |
| `moon run core:test`                                          | Core native and compatibility behavior remains stable | A4, A6                       |
| `moon run api:test`                                           | API runtime and graph routes remain stable            | A2, A4, A6                   |
| `moon run cli:test`                                           | CLI default runtime remains stable                    | A6                           |
| `moon run docs:lint`                                          | Docs render and links remain valid                    | A3, A5, A6                   |
| `moon run :check`                                             | Monorepo quality gate remains green                   | A4, A5, A6                   |
| `moon run bench-gate`                                         | Benchmark artifacts are release-citable               | A3, A6                       |
| `moon run memory-trust-gate`                                  | Trust track release gate remains green                | A6 if v0.8 ships both tracks |

## 11. Commit Boundaries

Use small, reversible commits:

- `fix(memory): hydrate native entity rows`
- `fix(memory): read native relationship history`
- `feat(memory): add native embedding providers`
- `test(memory): gate benchmark metadata`
- `refactor(memory): isolate graphiti ops`
- `docs(memory): label legacy archive policy`
- `docs(memory): sweep stale legacy service docs`
- `chore(release): record pure surreal audit receipts`

Each commit should include the tightest meaningful verification receipt in the body when the change
is more than documentation.

## 12. Risk Register

| Risk                                                          | Why It Matters                                              | Mitigation                                                             |
| ------------------------------------------------------------- | ----------------------------------------------------------- | ---------------------------------------------------------------------- |
| Native hydration drops legacy metadata                        | Old memories lose provenance or scope                       | Add native and legacy row fixtures before deleting compatibility paths |
| Relationship reads change recall ranking                      | Context packs can become less useful while tests stay green | Run context-pack benchmarks after native relationship changes          |
| Embedding metadata remains incomplete                         | Release claims become unreproducible                        | Gate citable artifacts with required provider and corpus metadata      |
| Compatibility code moves but tests still import default paths | Graphiti silently returns to the hot path                   | Keep no-Graphiti smoke and inventory gates in every wave               |
| Archive policy stays vague                                    | Users cannot trust rollback or historical imports           | Require explicit source type, dry-run counts, and docs before A6       |
| Stale docs imply legacy services                              | New installs become confusing or wrong                      | Add an allowlist-backed docs sweep for legacy terms                    |

## 13. Open Questions

- Should retained Graphiti compatibility live under `sibyl_core.compat.graphiti`,
  `sibyl_core.graph.compatibility`, or a package-extra-only module path?
- Does v0.8 ship Track A and Track B together, or can pure Surreal closure ship independently if
  memory trust remains preview-only?
- Which historical archive payloads are still contractually supported after v0.8?
- Does Redis/Valkey coordination stay documented in v0.8, or move to an advanced operations note?
- Which benchmark suite becomes the first public AI-memory comparison artifact?

## 14. Recommendation

Historical execution recommendation: proceed with A2 first, then A3, A4, A5, and A6. That sequence
has now closed. Current recommendation lives in the parent plan: v0.8 can ship from `4855ba8a`, and
the next roadmap starts with post-v0.8 memory productization plus the 0.8.1 inventory guard
hardening.
