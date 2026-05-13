# Sibyl v0.7 Graphiti Exit and Pure Surreal Cleanup Plan

- Status: Claude-reviewed execution plan
- Target release: v0.7 hardening wave
- Tracking epic: `epic_564b41ff89d6`
- Source docs:
  - `docs/architecture/SURREALDB_NATIVE_MEMORY_CORE_SPEC.md`
  - `docs/architecture/SURREALDB_GRAPHITI_EXIT_INVENTORY.md`
  - `docs/architecture/SURREALDB_GRAPHITI_EXIT_BENCHMARK_EVIDENCE.md`
  - `docs/architecture/SURREALDB_PHASE3_BURNDOWN.md`

This plan decomposes the next two roadmap lanes into executable work:

1. Exit Graphiti from the default memory loop.
2. Finish pure Surreal cleanup by removing or isolating legacy PostgreSQL, FalkorDB, Redis, and
   archive-only surfaces from default runtime paths.

The work is intentionally split. Graphiti exit proves Sibyl-native memory behavior. Pure Surreal
cleanup removes leftover operational coupling. Either lane can land in slices, but the v0.7 release
gate requires both to be boring, tested, and documented.

## 1. Release Definition

v0.7 is ready when a default install, CI run, local dev run, and production chart can operate on
SurrealDB without constructing Graphiti or requiring legacy services.

Required outcomes:

- Default `remember`, `recall`, `context`, `wake`, `reflect`, API jobs, CLI commands, MCP tools, and
  prompt hooks use native Surreal services.
- Graphiti imports are either deleted or classified as named compatibility, migration, admin, or
  test-only surfaces with owners and removal conditions.
- Native Surreal owns entity lookup, deduplication, semantic search, relationship hydration, entity
  hydration, episode mentions, and embedding cache behavior needed by default memory flows.
- PostgreSQL, FalkorDB, and Redis are absent from default compose, charts, CI services, docs,
  package extras, and runtime configuration. Any retained Redis coordination backend must be
  explicit opt-in behavior, not a default data service.
- Archive import, migration, and rollback behavior is explicit. Nothing relies on ambient legacy
  services.
- Generated inventory and hand-authored inventory agree.

Required gates:

- `moon run inventory-check inventory-typecheck inventory-test`
- `moon run core:test`
- `moon run api:test`
- `moon run cli:test`
- `moon run :check`
- `moon run baseline-seed`
- `moon run baseline-replay-runtime`
- `moon run core:bench-context -- --cases benchmarks/context_pack_cases.json --auth-manifest .moon/cache/baseline-runtime-manifest.json --label retrieval-compare --repeat 20 --metadata retrieval_mode=compare`
- `moon run core:no-graphiti-smoke`
- CI green
- nightly regression green in `.github/workflows/nightly-regression.yml`

Benchmark release rule:

- v0.7 can ship on no-Graphiti default-loop proof plus the context-pack gate above.
- Do not make public performance or quality comparisons against Graphiti until a paired `bench-live`
  legacy Graphiti artifact and native Surreal artifact exist, are gated, and are compared with
  `benchmarks/compare_eval_reports.py`.
- Do not make public AI memory benchmark or competitor claims until the cited suite has a raw
  artifact, overall metrics, per-slice metrics, per-case result records, corpus or dataset version,
  command, commit, runtime mode, and caveats recorded in
  `docs/architecture/SURREALDB_GRAPHITI_EXIT_BENCHMARK_EVIDENCE.md`. Missing external suites stay
  planned coverage only. LOCOMO, RULER, Mem0, Zep, LangMem, and any future competitor suite need
  full result rows before they become release-note evidence. Every citable AI-memory artifact must
  pass `moon run bench-gate -- <artifact>.json --profile ai-memory`.

## 2. Non-Goals

- Do not rewrite every compatibility command before v0.7. Explicit migration and archive tools can
  remain if they are named, documented, and absent from default runtime paths.
- Do not delete user data or force destructive migrations. Archive policy must be decided before
  legacy backup and restore code is removed.
- Do not preserve Graphiti-shaped abstractions just because native code currently wraps them. Keep a
  compatibility boundary only while an active caller still needs it.
- Do not expand this lane into the post-v0.7 synthesis engine. Graph-guided synthesis stays after
  the native memory core is stable.

## 3. Epic 1: Graphiti Exit From Default Memory Loops

Goal: default memory behavior works without importing or constructing Graphiti.

### Wave 1: Inventory Lock

Purpose: make the removal map enforceable before deleting runtime code.

Tasks:

- Regenerate runtime inventory and compare it against
  `docs/architecture/SURREALDB_GRAPHITI_EXIT_INVENTORY.md`.
- Classify each `graphiti_core` import as `delete`, `compatibility`, `migration`, `admin`, or
  `test-only`.
- Add owners and removal conditions for any newly discovered import.
- Expand the inventory check when a path can otherwise drift outside the hand-authored coverage
  rule.

Files:

- `docs/architecture/SURREALDB_GRAPHITI_EXIT_INVENTORY.md`
- `docs/research/rust-port/INVENTORY.md`
- `tools/inventory/runtime_surface.py`
- `tools/tests/test_runtime_surface.py`

Verify:

- `moon run inventory-check inventory-typecheck inventory-test`

Exit criteria:

- Every generated Graphiti import is classified.
- The generated and hand-authored inventories fail closed on drift.

### Wave 2: Native Graph Read Parity

Purpose: remove Graphiti from entity and relationship reads used by recall, context, wake, and API
graph surfaces.

Prerequisite:

- Spec Milestone A is green: W2.5 scoreboard plus the full Wave 1 memory policy helper set from
  `SURREALDB_NATIVE_MEMORY_CORE_SPEC.md`. That includes read decisions plus default-deny
  `authorize_memory_write`, `authorize_memory_share`, and `authorize_memory_reflect` decisions with
  stable reason codes.

Tasks:

- Add native exact entity lookup with deterministic normalization and tenant scoping.
- Authorize candidate hydration through the read-side memory policy helper before results can enter
  recall, context, wake, or API graph responses.
- Add native entity hydration for the typed models currently coerced from Graphiti nodes.
- Add native relationship hydration for `relates_to`, `mentions`, and explicit task-learning links.
- Move episode mention reads behind a native relationship manager.
- Preserve source IDs, confidence, temporal metadata, and policy metadata in hydrated records.

Files:

- `packages/python/sibyl-core/src/sibyl_core/graph/entities.py`
- `packages/python/sibyl-core/src/sibyl_core/graph/relationships.py`
- `packages/python/sibyl-core/src/sibyl_core/tasks/workflow.py`
- `packages/python/sibyl-core/tests/test_graph_entities.py`
- `packages/python/sibyl-core/tests/test_graph_relationships.py`
- `packages/python/sibyl-core/tests/test_no_graphiti_default_loop.py`

Verify:

- `moon run core:test -- tests/test_graph_entities.py`
- `moon run core:test -- tests/test_graph_relationships.py`
- `moon run core:no-graphiti-smoke`

Exit criteria:

- Native mode can hydrate entities and relationships without `graphiti_core`.
- The stale `Procedure.category = None` coercion class of failure is covered by a fixture or model
  normalization test.

### Wave 3: Native Graph Write Parity

Purpose: remove Graphiti from default writes for `sibyl add`, task completion learnings, reflection
promotion, and API entity creation.

Tasks:

- Route summarized remember/add writes through native entity, episode, and relationship services.
- Route task completion artifacts and learning links through native relationships.
- Route persisted reflection promotion through native graph writes when
  `SIBYL_NATIVE_WRITE=enabled`.
- Call `authorize_memory_write` before any native write originating in `tools/add.py` or
  `jobs/entities.py`, and call `authorize_memory_reflect` before task-completion learning writes in
  `tasks/workflow.py`.
- Add at least one deny-case test for each native write and reflect surface touched by this wave.
- Remove fallback writes for structured entity types after native dedupe and exact lookup cover the
  same behavior.
- Keep compatibility writes available only through explicit compatibility or migration surfaces.

Files:

- `apps/api/src/sibyl/jobs/entities.py`
- `packages/python/sibyl-core/src/sibyl_core/tools/add.py`
- `packages/python/sibyl-core/src/sibyl_core/tools/reflect.py`
- `packages/python/sibyl-core/src/sibyl_core/tasks/workflow.py`
- `packages/python/sibyl-core/tests/test_no_graphiti_default_loop.py`
- `apps/api/tests/test_jobs_entities.py`
- `apps/api/tests/test_tasks_workflow.py`

Verify:

- `moon run core:no-graphiti-smoke`
- `moon run api:test -- tests/test_jobs_entities.py`
- `moon run api:test -- tests/test_tasks_workflow.py`

Exit criteria:

- Default writes create native Surreal records with source provenance and centralized policy
  decisions from `sibyl_core.auth.memory_policy`.
- Compatibility writes are no longer reachable by default CLI, MCP, API, or job flows.

### Wave 4: Native Embedding Ownership

Purpose: stop carrying Graphiti-shaped embedder contracts in native retrieval.

Tasks:

- Introduce a native embedding service with cache ownership independent of Graphiti embedder types.
- Move Gemini embedding support behind the native service.
- Route vector writes and vector search through the native service.
- Keep deterministic embedder configuration surfaced in eval metadata.
- Leave `cached_embedder.py` and `gemini_embedder.py` as named compatibility code until Wave 5
  removes Graphiti client construction. Native paths must stop importing them in this wave.

Files:

- `packages/python/sibyl-core/src/sibyl_core/graph/cached_embedder.py`
- `packages/python/sibyl-core/src/sibyl_core/graph/gemini_embedder.py`
- `packages/python/sibyl-core/src/sibyl_core/graph/client.py`
- `packages/python/sibyl-core/src/sibyl_core/retrieval/native.py`
- `packages/python/sibyl-core/tests/test_graph_client.py`
- `packages/python/sibyl-core/tests/test_native_retrieval.py`

Verify:

- `moon run core:test -- tests/test_graph_client.py`
- `moon run core:test -- tests/test_native_retrieval.py`
- `moon run baseline-seed`
- `moon run baseline-replay-runtime`
- `moon run core:bench-context -- --cases benchmarks/context_pack_cases.json --auth-manifest .moon/cache/baseline-runtime-manifest.json --label retrieval-compare --repeat 20 --metadata retrieval_mode=compare`

Exit criteria:

- Native retrieval and writes do not depend on Graphiti embedder interfaces.
- Eval reports include deterministic embedding model, dimensions, index settings, and tokenizer
  metadata.

### Wave 5: Runtime Construction Cutover

Purpose: make Graphiti construction impossible on default paths.

Tasks:

- Replace default graph runtime construction with native Surreal managers.
- Move Graphiti client construction behind an explicit compatibility factory.
- Keep all `graphiti_core` imports inside compatibility factory functions so native app boot, CLI
  entrypoints, MCP tools, and prompt hooks do not import Graphiti at module import time.
- Remove Graphiti search-interface calls from compare mode once native retrieval is default and
  covered by seeded evals.
- Flip `SIBYL_NATIVE_WRITE` default to enabled after Wave 3 contract tests and the no-Graphiti smoke
  are green. Document rollback as a flag flip plus a raw-capture rebuild or replay path.
- Keep admin and migration surfaces named so they cannot be mistaken for default runtime behavior.
- Expand no-Graphiti smoke coverage for CLI, MCP, API job, and prompt-hook entrypoints.

Files:

- `apps/api/src/sibyl/persistence/graph_runtime.py`
- `packages/python/sibyl-core/src/sibyl_core/graph/client.py`
- `packages/python/sibyl-core/src/sibyl_core/graph/search_interface.py`
- `packages/python/sibyl-core/src/sibyl_core/graph/surreal/ops/*`
- `packages/python/sibyl-core/tests/test_no_graphiti_default_loop.py`

Verify:

- `moon run inventory-check inventory-typecheck inventory-test`
- `moon run core:no-graphiti-smoke`
- `moon run api:test`
- `moon run cli:test`

Exit criteria:

- No default path constructs Graphiti.
- Native writes are default-on with an explicit rollback envelope.
- Remaining Graphiti code is named compatibility, migration, admin, or test-only.

### Wave 6: Dependency Boundary

Purpose: decide whether Graphiti remains as an optional compatibility dependency or is deleted from
runtime packaging.

Tasks:

- Audit `pyproject.toml`, extras, lockfile, imports, and docs for Graphiti dependency ownership.
- Move Graphiti dependencies to an explicit compatibility extra if migration/admin surfaces still
  need them.
- Remove Graphiti from default runtime dependencies when no default import remains.
- Update release notes with exact compatibility status.

Files:

- `packages/python/sibyl-core/pyproject.toml`
- `uv.lock`
- `docs/guide/surrealdb-migration-release-notes.md`
- `docs/architecture/SURREALDB_GRAPHITI_EXIT_INVENTORY.md`

Verify:

- `moon run inventory-check inventory-typecheck inventory-test`
- `moon run core:test`
- `moon run :check`

Exit criteria:

- A default install does not need Graphiti for normal memory behavior.
- Any retained Graphiti dependency is opt-in and documented.

## 4. Epic 2: Pure Surreal Cleanup

Goal: SurrealDB is the only default data service. Legacy services exist only as explicit archive or
migration inputs.

### Wave 1: Legacy Surface Inventory

Purpose: identify every remaining default-path reference to PostgreSQL, FalkorDB, Redis, and legacy
auth/RBAC.

Tasks:

- Regenerate runtime inventory after Graphiti-exit inventory updates.
- Search default runtime code, compose files, charts, CI, docs, and package extras for legacy
  services.
- Classify each reference as `delete`, `archive`, `migration`, `compatibility`, `test-only`, or
  `historical docs`.
- Add pure-Surreal checks for any default-path drift that inventory cannot currently detect.

Files:

- `docs/architecture/SURREALDB_PHASE3_BURNDOWN.md`
- `docs/research/rust-port/INVENTORY.md`
- `tools/inventory/runtime_surface.py`
- `tools/tests/test_runtime_surface.py`
- `docker-compose.yml`
- `docker-compose.prod.yml`
- `docker-compose.quickstart.yml`
- `docker-compose.quickstart.test.yml`
- `compose.e2e.yml`
- `.devcontainer/docker-compose.yml`
- `.github/workflows/*`
- `charts/**`

Verify:

- `moon run inventory-check inventory-typecheck inventory-test`
- targeted `rg` audit for `postgres`, `falkor`, `redis`, and legacy auth symbols

Exit criteria:

- Every remaining legacy reference has an owner, purpose, and deletion condition.

### Wave 2: Archive And Rollback Policy

Purpose: settle what legacy data recovery means before deleting backup and restore branches.

Tasks:

- Define supported archive import formats.
- Define unsupported rollback paths and document why they are unsupported.
- Decide whether PostgreSQL archive support is migration-only or retained as a named import tool.
- Close the deferred graph export/import policy from Phase 3 Lane 5: drop it, keep it as a named
  import path, or replace it with a Surreal-native export.
- Make archive import commands require explicit input files and mode flags.
- Add tests for archive import refusing ambient database connections.

Files:

- `docs/guide/surrealdb-migration-release-notes.md`
- `docs/architecture/SURREALDB_PHASE3_BURNDOWN.md`
- archive or migration command modules under `apps/api/src/sibyl/` and `packages/python/sibyl-core/`

Verify:

- targeted archive import tests
- `moon run api:test`
- `moon run core:test`

Exit criteria:

- Recovery is file-based or explicitly configured.
- Graph export/import policy is no longer deferred.
- No default command reaches for PostgreSQL, FalkorDB, or Redis as a live dependency.

### Wave 3: Close Legacy Auth And RBAC Residue

Purpose: verify Phase 3 Lane 1 remains complete and remove only the residue that still appears in
inventory, docs, or explicit migration commands.

Tasks:

- Confirm generated inventory still has no active legacy auth/RBAC runtime modules.
- Delete empty legacy auth/RBAC directories or shims when no import needs them.
- Decide whether the `auth-readonly` freeze/unfreeze command remains as a historical migration tool
  or is removed.
- Remove stale settings, environment variables, tests, and docs that imply a default legacy auth
  store.
- Confirm project-scoped recall and refresh-token behavior still use Surreal auth stores.
- Add regression coverage for revoked sessions and expired bearer tokens.

Files:

- `apps/api/src/sibyl/cli/migrate.py`
- `apps/api/src/sibyl/persistence/legacy/**`
- `apps/api/src/sibyl/auth/**`
- `apps/api/tests/test_auth*.py`
- `packages/python/sibyl-core/src/sibyl_core/auth/**`
- docs that mention legacy auth migration

Verify:

- `moon run api:test -- tests/test_auth*.py`
- `moon run core:test -- tests/test_memory_policy.py`

Exit criteria:

- Phase 3 Lane 1 remains true: legacy auth/RBAC is deleted or historical migration-only.
- Login, refresh, project-scoped recall, and owner-only debug routes remain green.

### Wave 4: Close Legacy Content, Settings, And Backup Residue

Purpose: verify Phase 3 Lanes 2 and 3 remain complete, then remove or isolate the remaining archive,
settings, backup, and coordination residue after policy is settled.

Tasks:

- Treat content and Surreal archive export/restore as already shipped. Audit only the residue.
- Remove settings branches that imply PostgreSQL, FalkorDB, or Redis are default data services.
- Decide whether Redis coordination remains explicit opt-in or moves out of the v0.7 default surface
  entirely.
- Remove backup and restore code that assumes live legacy services.
- Keep only explicit archive import or historical migration code.
- Update tests to assert default config forbids memory-mode production and legacy service fallbacks.

Files:

- `apps/api/src/sibyl/persistence/**`
- `apps/api/src/sibyl/persistence/settings_runtime.py`
- `apps/api/src/sibyl/persistence/settings_types.py`
- `apps/api/src/sibyl/services/settings.py`
- `apps/api/src/sibyl/jobs/backup.py`
- `apps/api/src/sibyl/cli/migrate.py`
- `apps/api/src/sibyl/persistence/backups_common.py`
- `packages/python/sibyl-core/src/sibyl_core/migrate/archive.py`
- `packages/python/sibyl-core/src/sibyl_core/config*.py`
- backup, restore, and migration modules
- related API/core tests

Verify:

- `moon run api:test`
- `moon run core:test`
- `moon run inventory-check inventory-typecheck inventory-test`

Exit criteria:

- Default config has no live PostgreSQL, FalkorDB, or Redis fallback.
- Backup and restore docs describe only supported Surreal archive flows.

### Wave 5: Runtime, CI, And Chart Cleanup

Purpose: make the operational surface match the code.

Tasks:

- Remove legacy service containers from default compose and CI workflows.
- Keep SurrealDB as the only required database service in default CI.
- Remove legacy chart values, secrets, jobs, env vars, and docs.
- Ensure dev docs use Surreal container service in CI and Surreal local runtime by default.
- Keep legacy migration docs clearly marked as historical or archive-only.

Files:

- `.github/workflows/*`
- `docker-compose.yml`
- `docker-compose.prod.yml`
- `docker-compose.quickstart.yml`
- `docker-compose.quickstart.test.yml`
- `compose.e2e.yml`
- `.devcontainer/docker-compose.yml`
- `charts/**`
- `README.md`
- `apps/api/README.md`
- `apps/cli/README.md`
- `docs/guide/why-surreal.md`

Verify:

- CI green
- `moon run :check`
- `moon run docs:lint`
- chart render tests when available

Exit criteria:

- Default CI does not start PostgreSQL, FalkorDB, or Redis.
- Default docs do not instruct users to run legacy services.
- Charts render without legacy secrets or jobs.

### Wave 6: Final Pure Surreal Release Audit

Purpose: prove the release surface is coherent.

Tasks:

- Run the full default install and local dev path against SurrealDB only.
- Run inventory and dependency checks from a clean checkout.
- Run the no-Graphiti smoke with legacy services absent.
- Audit docs for stale migration-era instructions.
- Audit benchmark evidence for full artifact-backed AI memory results. Missing external suites must
  stay explicit, not implied by context-pack or LongMemEval-style evidence. Gate every citable
  artifact with `moon run bench-gate -- <artifact>.json --profile ai-memory`.
- Check release notes against the benchmark evidence table so only suites with full result records
  are cited.
- Append release notes with supported upgrade and archive import paths.

Verify:

- `moon run inventory-check inventory-typecheck inventory-test`
- `moon run core:test`
- `moon run api:test`
- `moon run cli:test`
- `moon run :check`
- CI green
- `moon run baseline-seed`
- `moon run baseline-replay-runtime`
- `moon run core:bench-context -- --cases benchmarks/context_pack_cases.json --auth-manifest .moon/cache/baseline-runtime-manifest.json --label retrieval-compare --repeat 20 --metadata retrieval_mode=compare`
- nightly regression green in `.github/workflows/nightly-regression.yml`

Exit criteria:

- Default development, CI, and release docs are all pure Surreal.
- Legacy references are only historical, archive, migration, compatibility, or test-only.

## 5. Suggested Execution Order

1. Graphiti Wave 1: lock inventory before deleting code.
2. Pure Surreal Wave 1: inventory legacy runtime references in parallel.
3. Pure Surreal Wave 2: settle archive and rollback policy.
4. Spec Milestone A: W2.5 scoreboard plus the full Wave 1 memory policy helper set are green.
5. Pure Surreal Wave 3: close legacy auth/RBAC residue.
6. Graphiti Wave 2: native graph read parity.
7. Graphiti Wave 3: native graph write parity.
8. Graphiti Wave 4: native embedding ownership.
9. Graphiti Wave 5: runtime construction cutover and native-write default flip.
10. Pure Surreal Wave 4: close legacy content, settings, backup, and coordination residue.
11. Pure Surreal Wave 5: clean runtime, CI, charts, and docs.
12. Graphiti Wave 6: dependency boundary and optional compatibility extra.
13. Pure Surreal Wave 6: final release audit.

This order keeps data recovery decisions ahead of destructive cleanup while moving Graphiti removal
through measurable parity gates.

## 6. Risk Register

| Risk                                               | Why It Matters                                                                 | Mitigation                                                                                                                                                                                                                             |
| -------------------------------------------------- | ------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Native retrieval quality regresses                 | Graphiti removal should not reduce context usefulness                          | Gate on seeded context evals and compare-mode evidence before default cutover                                                                                                                                                          |
| Legacy records lose visibility                     | Pre-v0.7 Graphiti-written memories may not have explicit scope/source metadata | Apply the Legacy Projection Rule from `SURREALDB_GRAPHITI_EXIT_INVENTORY.md`: project records with recoverable scope/source, organization-scope owner-less records with source IDs, and exclude records without recoverable source IDs |
| Archive cleanup deletes rollback options too early | Users need a clear recovery path after pure-Surreal cleanup                    | Decide archive policy before deleting backup/restore branches                                                                                                                                                                          |
| Compatibility surfaces hide default dependencies   | A named fallback can accidentally remain on the hot path                       | Use no-Graphiti smoke and inventory checks as release blockers                                                                                                                                                                         |
| Embedding drift changes eval results               | Native retrieval scores become noisy or unreproducible                         | Pin embedder metadata and tokenizer/index settings in eval reports                                                                                                                                                                     |
| Auth cleanup breaks session refresh                | Recent login failures showed auth/session behavior is easy to regress          | Keep focused refresh, revocation, owner-debug, and project-recall tests in the release gate                                                                                                                                            |

## 7. Task Tracking Shape

Recommended Sibyl tracking:

- Epic: `v0.7 Graphiti Exit`
  - Task: lock Graphiti exit inventory
  - Task: finish native graph read parity
  - Task: finish native graph write parity
  - Task: move embeddings to native ownership
  - Task: cut over runtime construction
  - Task: decide Graphiti dependency boundary
- Epic: `v0.7 Pure Surreal Cleanup`
  - Task: inventory legacy runtime surfaces
  - Task: settle archive and rollback policy
  - Task: close legacy auth and RBAC residue
  - Task: close legacy content, settings, backup, and coordination residue
  - Task: clean runtime, CI, charts, and docs
  - Task: run final pure-Surreal release audit

Each task should carry the relevant verification command from this plan and complete with concrete
evidence, not a summary-only note.
