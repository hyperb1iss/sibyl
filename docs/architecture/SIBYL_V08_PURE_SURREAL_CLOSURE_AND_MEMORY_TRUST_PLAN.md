# Sibyl v0.8 Pure Surreal Closure and Memory Trust Plan

- Status: v0.8 release evidence complete for pushed `main` commit `4855ba8a`; tag and release
  publication remain manual
- Target release: v0.8
- Planning source: `plan_e464fd1e7b11`
- Plan-authoring task: `c64a358e-aef4-4b32-8735-28f03047a13e`
- Tracking epics:
  - Pure Surreal Closure: `epic_416f955f7f39`
  - Memory Trust Foundation: `epic_539eea7afeb3`
- Related docs:
  - `docs/architecture/SIBYL_NORTHSTAR.md`
  - `docs/architecture/SIBYL_V08_PURE_SURREAL_CLOSURE_EXECUTION_PLAN.md`
  - `docs/architecture/SURREALDB_NATIVE_MEMORY_CORE_SPEC.md`
  - `docs/architecture/SURREALDB_V07_GRAPHITI_EXIT_AND_PURE_SURREAL_PLAN.md`
  - `docs/architecture/SURREALDB_GRAPHITI_EXIT_INVENTORY.md`
  - `docs/architecture/SURREALDB_PHASE3_BURNDOWN.md`
  - `docs/architecture/PERMISSION_SYSTEM_AUDIT.md`

v0.7 made the SurrealDB-native memory loop real. The default `remember`, `recall`, `context`,
`wake`, `reflect`, task workflow, jobs, CLI, MCP, and prompt-hook surfaces can run without Graphiti
or legacy services on the hot path. v0.8 should make that state boring and durable.

The next large chunk has two tracks:

1. Pure Surreal closure: remove, quarantine, or explicitly name the remaining compatibility
   scaffolding so a normal install and normal runtime are Surreal-only.
2. Memory trust foundation: install the identity, policy, audit, and inspection substrate needed
   before Sibyl expands into memory spaces, sharing, team memory, and graph-guided synthesis.

These tracks are connected. Pure Surreal closure reduces operational ambiguity. Memory trust makes
the second brain safe enough to use for personal, delegated, project, team, and organization memory
without leaking the wrong context.

## 1. Current State

Release evidence verified on 2026-05-14:

- Pushed `main` release baseline: `4855ba8a`.
- Main CI run `25870913035` completed successfully on `4855ba8a`.
- Docs deploy run `25877971558` completed successfully on `4855ba8a`.
- Nightly regression run `25877971585` completed successfully on `4855ba8a`.
- The local 0.8.1 docs and inventory hardening after this baseline needs separate CI evidence if it
  becomes patch-release evidence.

Earlier A0 baseline lock, 2026-05-13:

- Local baseline commit: `1de0b408`.
- Last pushed `origin/main` receipt commit: `d2d3d926`.
- `moon run inventory-check inventory-typecheck inventory-test` passes; generated inventory is
  current and covers 21 Graphiti import files; inventory tests report 14 passed.
- `moon run core:no-graphiti-smoke` passes with 2 tests.
- `moon run :check` passes with 33 tasks completed, including 5 executed tasks and 28 cache hits.
  Receipts include core 1327 passed and 15 skipped, API 1639 passed and 1 skipped, CLI 156 passed,
  and web 88 passed.
- Main CI is green on `origin/main` run ID `25801942331`. Docs deploy is green on run ID
  `25801942466`. Scheduled nightly regression is green on run ID `25791871706`.
- Local `main` is ahead of `origin/main`; the CI receipts cover the latest pushed main commit, and
  the local receipts cover this A0 checkpoint.
- Default `sibyl-core` runtime dependencies do not include `graphiti-core`; Graphiti is isolated to
  the `compatibility` optional extra and `sibyl-core` dev dependency group.
- Generated inventory still lists 21 Graphiti import files. They are classified as compatibility,
  admin, migration, or test scaffolding, not default-loop requirements.
- Default compose, CI, and docs are already SurrealDB-first, with Redis/Valkey as explicit
  coordination opt-in.
- Phase 3 burndown still carries archive, rollback, stale docs, and compatibility-policy residue.
- The permission audit identifies project RBAC, MCP policy context, setup endpoint gating, and audit
  consistency as the next security-sensitive control-plane work.

## 2. Release Definition

v0.8 is ready when all of these are true:

- A default install, default local dev run, default CI run, and default chart render do not need
  Graphiti, FalkorDB, PostgreSQL, or Redis/Valkey as data services.
- Any retained Graphiti code lives in one named compatibility island and cannot be imported by
  default application boot, CLI, MCP tools, jobs, prompt hooks, context packs, task workflow, or
  native retrieval.
- Native graph managers own entity lookup, relationship hydration, temporal reads, exact lookup,
  graph traversal, and default graph writes.
- Native embedding service owns embedding model selection, cache behavior, vector writes, vector
  search, and eval metadata without Graphiti embedder interfaces.
- Archive import, rollback, and historical migration surfaces are file-based or explicitly
  configured. No default command reaches for ambient PostgreSQL or FalkorDB.
- Project-scoped memory cannot leak through REST, MCP, CLI, search, explore, context packs, wake,
  recall, or reflection promotion.
- Memory policy decisions are shared across API, CLI, MCP, raw memory, context packs, reflection,
  and task learning writes.
- Context packs, memory writes, and reflection promotion expose source IDs, visibility, freshness,
  and policy reason metadata that can be inspected and tested.
- Audit events record the actor, delegated authority, organization, project, memory scope, action,
  and policy decision for trust-sensitive memory operations.
- Persisted `MemorySpace` CRUD and membership tables are not a v0.8 release claim. v0.8 claims the
  `MemoryScope`/`MemoryPolicyContext` policy primitive for private, delegated, and project memory;
  team, organization, shared, and public scopes remain disabled with stable deny reasons until the
  post-v0.8 memory workspace work.

Required release gates:

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
- Nightly regression green on `main`

The baseline seed and replay gates predate v0.8 and remain required because release benchmark and
runtime claims must be regenerated against the final tree, not inherited from earlier receipts.

## 3. Non-Goals

- Do not build full `synthesize` in v0.8. This release prepares the trust and provenance substrate
  that `synthesize` will reuse.
- Do not build an arbitrary policy language. Keep policy as code plus simple data records until real
  usage requires more.
- Do not delete historical archive support before archive and rollback policy is explicit.
- Do not ship broad cross-organization sharing. v0.8 can support previews, stable deny reasons, and
  promotion foundations.
- Do not rebuild the entire web UI. Add only the minimal API and CLI inspection surfaces needed to
  prove trust behavior.
- Do not keep compatibility code just because tests still import it. Tests should move to named
  compatibility gates when the product no longer needs the path.

## 4. Track A: Pure Surreal Closure

Goal: make SurrealDB the only default data plane and make Graphiti a deliberate compatibility choice
rather than ambient scaffolding.

### Wave A0: Baseline Lock

Purpose: preserve the post-v0.7 green state before deleting or moving compatibility code.

Implementation:

- Record the current generated inventory, no-Graphiti smoke state, CI receipts, and dependency
  boundary in the v0.8 tracking epic.
- Add release-gate wording to the relevant docs if any current default-loop gate is missing.
- Confirm `graphiti-core` remains optional in runtime package metadata.
- Confirm scratch, generated, and benchmark artifacts are not accidentally pulled into commits.

Files:

- `docs/architecture/SURREALDB_GRAPHITI_EXIT_INVENTORY.md`
- `docs/architecture/SURREALDB_PHASE3_BURNDOWN.md`
- `docs/architecture/SIBYL_V08_PURE_SURREAL_CLOSURE_AND_MEMORY_TRUST_PLAN.md`

Verify:

- `moon run inventory-check inventory-typecheck inventory-test`
- `moon run core:no-graphiti-smoke`
- `moon run :check`

A0 receipt, 2026-05-13:

- Local commit: `1de0b408`.
- `moon run inventory-check inventory-typecheck inventory-test`: current generated inventory, 21
  covered Graphiti import files, 14 passed, inventory typecheck passed.
- `moon run core:no-graphiti-smoke`: 2 passed.
- `moon run :check`: 33 completed, including 5 executed tasks and 28 cache hits. Core reported 1327
  passed and 15 skipped; API reported 1639 passed and 1 skipped; CLI reported 156 passed; web
  reported 88 passed.
- Dependency boundary: `graphiti-core` appears in `sibyl-core[compatibility]` and the `sibyl-core`
  dev dependency group, not default `sibyl-core` runtime dependencies.
- CI boundary: `origin/main` at `d2d3d926` has green CI and docs deploy runs from
  2026-05-13T13:24:12Z plus a green scheduled nightly from 2026-05-13T09:56:01Z. Local `main`
  remains ahead of `origin/main`, so this receipt does not claim CI coverage for the local commits.

Exit criteria:

- Baseline gates are green and documented.
- Any later wave can prove whether it reduced, preserved, or intentionally moved compatibility
  surface area.

### Wave A1: Graphiti Compatibility Quarantine

Purpose: make Graphiti importability explicit.

Implementation:

- Move Graphiti-dependent tests behind named compatibility tasks or markers.
- Ensure default test, lint, typecheck, API boot, CLI boot, MCP import, job import, and prompt-hook
  import do not rely on Graphiti being installed.
- Add an import-boundary test that fails if default modules import from the compatibility island.
- Introduce a narrow compatibility package or module boundary for remaining Graphiti adapters.
- Keep compatibility docs explicit about installation with `sibyl-core[compatibility]`.

Files:

- `packages/python/sibyl-core/pyproject.toml`
- `moon.yml`
- `packages/python/sibyl-core/src/sibyl_core/graph/client.py`
- `packages/python/sibyl-core/src/sibyl_core/graph/*`
- `packages/python/sibyl-core/tests/*`
- `apps/api/tests/*`
- `docs/architecture/SURREALDB_GRAPHITI_EXIT_INVENTORY.md`

Verify:

- `uv lock --check`
- `moon run inventory-check inventory-typecheck inventory-test`
- `moon run core:no-graphiti-smoke`
- default `moon run core:test`
- compatibility test task when explicitly enabled

Exit criteria:

- Graphiti can be absent from a default development or production environment.
- Any test that needs Graphiti names that requirement in its task or marker.
- The inventory can distinguish default code from compatibility code.

### Wave A2: Native Graph Manager Replacement

Purpose: remove Graphiti-shaped entity and relationship read/write adapters from active graph
manager APIs.

Implementation:

- Replace remaining default uses of `EntityNode`, `EpisodicNode`, and Graphiti edge models with
  native Surreal record hydration.
- Move relationship CRUD to native `relates_to` and `mentions` managers.
- Move temporal reads to native relationship history helpers.
- Keep exact source IDs, confidence, validity, and provenance fields intact.
- Add model normalization fixtures for legacy row shapes and native row shapes.
- Remove Graphiti edge error handling from default API graph runtime.

Files:

- `apps/api/src/sibyl/persistence/graph_runtime.py`
- `packages/python/sibyl-core/src/sibyl_core/services/native_graph.py`
- `packages/python/sibyl-core/src/sibyl_core/graph/entities.py`
- `packages/python/sibyl-core/src/sibyl_core/graph/relationships.py`
- `packages/python/sibyl-core/src/sibyl_core/retrieval/native.py`
- `packages/python/sibyl-core/tests/test_graph_entities.py`
- `packages/python/sibyl-core/tests/test_graph_relationships.py`
- `apps/api/tests/test_graph_entities.py`
- `apps/api/tests/test_graph_relationships.py`

Verify:

- `moon run core:test -- tests/test_graph_entities.py tests/test_graph_relationships.py`
- `moon run api:test -- tests/test_graph_entities.py tests/test_graph_relationships.py`
- `moon run core:no-graphiti-smoke`

Exit criteria:

- Default graph manager APIs no longer require Graphiti node or edge classes.
- Native graph reads and writes cover the seeded behavior previously covered by Graphiti
  compatibility adapters.

### Wave A3: Native Embedding Ownership

Purpose: make embedding a Sibyl-native service, not a Graphiti-shaped adapter.

Implementation:

- Create a native embedding service with provider selection, dimensions, cache keys, and metadata.
- Move Gemini and OpenAI embedding support behind native provider implementations.
- Route native vector writes and vector search through the native service.
- Record embedding model, dimensions, provider, tokenizer estimate method, and index settings in
  eval reports.
- Keep old Graphiti-compatible embedders only inside the compatibility island until deletion.

Files:

- `packages/python/sibyl-core/src/sibyl_core/retrieval/native.py`
- `packages/python/sibyl-core/src/sibyl_core/services/native_graph.py`
- `packages/python/sibyl-core/src/sibyl_core/graph/cached_embedder.py`
- `packages/python/sibyl-core/src/sibyl_core/graph/gemini_embedder.py`
- `packages/python/sibyl-core/src/sibyl_core/graph/client.py`
- `packages/python/sibyl-core/tests/test_native_retrieval.py`
- `packages/python/sibyl-core/tests/test_graph_client.py`
- `benchmarks/context_pack_eval.py`
- `docs/testing/benchmark-methodology.md`

Verify:

- `moon run core:test -- tests/test_native_retrieval.py tests/test_graph_client.py`
- `moon run core:bench-context`
- `moon run baseline-seed`
- `moon run baseline-replay-runtime`

Exit criteria:

- Native paths do not import Graphiti embedder interfaces.
- Eval reports include deterministic embedding and tokenizer metadata.
- Compatibility embedders are isolated and removable.

### Wave A4: Graphiti Operations Island Or Deletion

Purpose: decide whether the Graphiti Surreal ops package remains as an optional compatibility
artifact or is removed.

Implementation:

- Audit `packages/python/sibyl-core/src/sibyl_core/graph/surreal/compat/ops/*` after A1-A3.
- Delete modules with no compatibility owner.
- Move retained modules under a clearly named compatibility namespace if they still support
  migration, admin, or explicit compare workflows.
- Remove stale comments that imply Graphiti is the active graph runtime.
- Update inventory coverage rules after the package move or deletion.

Files:

- `packages/python/sibyl-core/src/sibyl_core/graph/surreal/compat/ops/*`
- `packages/python/sibyl-core/src/sibyl_core/backends/surreal/driver.py`
- `packages/python/sibyl-core/src/sibyl_core/graph/search_interface.py`
- `packages/python/sibyl-core/src/sibyl_core/graph/mock_llm.py`
- `tools/inventory/runtime_surface.py`
- `tools/tests/test_runtime_surface.py`
- `docs/architecture/SURREALDB_GRAPHITI_EXIT_INVENTORY.md`

Verify:

- `moon run inventory-check inventory-typecheck inventory-test`
- compatibility test task when explicitly enabled
- `moon run core:no-graphiti-smoke`
- `moon run :check`

Exit criteria:

- Generated inventory has no unowned Graphiti imports.
- Remaining Graphiti code is either deleted or isolated as explicit compatibility.

### Wave A5: Legacy Archive, Coordination, And Docs Cleanup

Purpose: close the leftover operational ambiguity around legacy services.

Implementation:

- Settle archive policy for retained `postgres.sql` payloads and graph archive imports.
- Make archive import commands require explicit input files and mode flags.
- Ensure default backup/restore docs mention only supported Surreal archive flows.
- Confirm Redis/Valkey remains explicit coordination opt-in and is never implied as default data
  storage.
- Remove stale FalkorDB/PostgreSQL instructions from active docs, leaving only historical or
  migration-labeled guidance.
- Add inventory checks for any default-path drift not currently covered.

Files:

- `apps/api/src/sibyl/cli/migrate.py`
- `apps/api/src/sibyl/jobs/backup.py`
- `apps/api/src/sibyl/persistence/**`
- `packages/python/sibyl-core/src/sibyl_core/migrate/archive.py`
- `docker-compose*.yml`
- `compose.e2e.yml`
- `.github/workflows/*`
- `charts/**`
- `README.md`
- `apps/api/README.md`
- `apps/cli/README.md`
- `docs/guide/surrealdb-migration-release-notes.md`
- `docs/architecture/SURREALDB_PHASE3_BURNDOWN.md`

Verify:

- `moon run api:test`
- `moon run core:test`
- `moon run docs:lint`
- targeted `rg` audit for `postgres`, `falkor`, `redis`, `Graphiti`, and `graphiti`

Exit criteria:

- Default docs and default runtime no longer suggest legacy services.
- Migration and archive surfaces are explicit, file-based, and tested.
- Redis/Valkey is clearly coordination-only and opt-in.

### Wave A6: Pure Surreal Release Audit

Purpose: prove the release surface is coherent from a clean checkout.

Implementation:

- Run full local dev verification against SurrealDB only.
- Run inventory and dependency checks from a clean checkout.
- Run no-Graphiti smoke with Graphiti absent from the default environment.
- Audit docs, charts, compose, CI, package metadata, and release notes.
- Gate every citable AI-memory artifact with `bench-gate`.
- Record final CI and nightly receipts in Sibyl.

Verify:

- `moon run inventory-check inventory-typecheck inventory-test`
- `moon run core:no-graphiti-smoke`
- `moon run core:test`
- `moon run api:test`
- `moon run cli:test`
- `moon run docs:lint`
- `moon run :check`
- `moon run baseline-seed`
- `moon run baseline-replay-runtime`
- `moon run core:bench-context -- --cases benchmarks/context_pack_cases.json --auth-manifest .moon/cache/baseline-runtime-manifest.json --label retrieval-compare --repeat 20 --metadata retrieval_mode=compare`
- CI green
- nightly regression green

Exit criteria:

- v0.8 can be released as a Surreal-only default runtime.
- Every retained compatibility surface is opt-in, documented, and tested separately.

## 5. Track B: Memory Trust Foundation

Goal: make Sibyl safe and inspectable enough for memory spaces, project privacy, delegated agents,
promotion, sharing previews, and future synthesis.

### Wave B0: Trust Surface Inventory

Purpose: lock the current policy and authorization reality before changing control-plane behavior.

Implementation:

- Reconcile `PERMISSION_SYSTEM_AUDIT.md` with the current Surreal auth/runtime code.
- Inventory REST, MCP, CLI, prompt hook, and job surfaces that read or write memory.
- Mark which surfaces carry user ID, agent identity, organization, project, memory scope, and
  membership context.
- Add missing test fixtures for project-private data and private memory leaks.

Files:

- `docs/architecture/PERMISSION_SYSTEM_AUDIT.md`
- `docs/architecture/PERMISSION_SYSTEM_PLAN.md`
- `apps/api/src/sibyl/auth/authorization.py`
- `apps/api/src/sibyl/server.py`
- `apps/api/src/sibyl/api/routes/search.py`
- `apps/api/src/sibyl/api/routes/context.py`
- `apps/api/src/sibyl/api/routes/memory.py`
- `packages/python/sibyl-core/src/sibyl_core/auth/memory_policy.py`
- `packages/python/sibyl-core/tests/test_memory_policy.py`

Verify:

- `moon run core:test -- tests/test_memory_policy.py`
- `moon run api:test -- tests/test_routes_context.py tests/test_routes_memory.py`

B0 inventory receipt, 2026-05-13:

- `docs/architecture/PERMISSION_SYSTEM_AUDIT.md` now has a Surreal auth reconciliation section and
  trust-surface inventory covering REST, MCP, CLI, prompt hook, and job memory paths.
- `docs/architecture/PERMISSION_SYSTEM_PLAN.md` is explicitly marked as historical design context
  rather than current Postgres/RLS implementation guidance.
- Current green coverage already includes core memory policy tests, REST memory tests, REST context
  tests, and MCP accessible-project tests.
- Tracked implementation gaps:
  - B2 owns direct entity list/get project-private filtering, temporal search classification,
    raw-capture visibility classification, and project fallback retirement.
  - B3 owns canonical policy context across raw memory, context, MCP `add/manage`, CLI output, and
    async job payloads.
  - B4 owns inspect/audit output for allowed, denied, hidden, promoted, and source-derived memory.

Exit criteria:

- Every memory surface has an explicit policy-context status.
- Missing context is tracked as implementation work, not tribal knowledge.

### Wave B1: Memory Scope Policy Boundary

Purpose: install the memory-scope policy primitive needed for v0.8 and defer first-class persisted
memory-space administration to the post-v0.8 memory workspace work.

Implementation:

- Carry `memory_space` and `scope_key` through `MemoryPolicyContext`.
- Model private, delegated, and project membership through the current user, agent, project, and
  accessible-project/delegation context.
- Keep team, organization, shared, and public write/share behavior disabled until explicit policy
  cases are implemented.
- Resolve project graph memory through the canonical project ID and policy scope key.
- Defer persisted `memory_spaces` and `memory_space_members` tables, CRUD, and graph projections to
  `docs/architecture/SIBYL_POST_V08_SYNTHESIS_AND_MEMORY_WORKSPACE_PLAN.md`.

Files:

- `apps/api/src/sibyl/persistence/surreal/auth_runtime.py`
- `apps/api/src/sibyl/persistence/auth_runtime.py`
- `apps/api/src/sibyl/api/routes/memory.py`
- `apps/api/src/sibyl/api/schemas.py`
- `packages/python/sibyl-core/src/sibyl_core/auth/context.py`
- `packages/python/sibyl-core/src/sibyl_core/auth/memory_policy.py`
- `apps/api/tests/test_surreal_auth_persistence.py`
- `apps/api/tests/test_routes_memory.py`
- `packages/python/sibyl-core/tests/test_memory_policy.py`

Verify:

- `moon run api:test -- tests/test_surreal_auth_persistence.py tests/test_routes_memory.py`
- `moon run core:test -- tests/test_memory_policy.py`

Exit criteria:

- Policy helpers can resolve private, delegated, and project visibility without graph lookups.
- Disabled scopes return stable deny reasons.
- Persisted memory-space CRUD and membership basics are explicitly deferred, not claimed by v0.8.

B1 scope correction receipt, 2026-05-14:

- Repository audit found no implemented `memory_spaces` or `memory_space_members` control-plane
  tables. v0.8 therefore must not claim persisted MemorySpace CRUD or membership management.
- The implemented release surface is `MemoryScope` plus `MemoryPolicyContext.memory_space` and
  `scope_key`, used by REST, MCP, CLI-mediated API calls, context/session rendering, and
  task-learning jobs.
- Private, delegated, and project policy decisions are covered by `moon run core:test` and
  `moon run memory-trust-gate`; team, organization, shared, and public scopes fail closed with
  `scope_not_enabled`.
- Post-v0.8 `SIBYL_POST_V08_SYNTHESIS_AND_MEMORY_WORKSPACE_PLAN.md` owns the persisted memory-space
  control plane, membership UI/API/CLI, and agent access preview work.

### Wave B2: Project RBAC Hardening

Purpose: close the known project authorization gaps before expanding sharing.

Implementation:

- Ensure graph project creation, rename, and archive synchronize canonical project control-plane
  records.
- Fix graph project ID versus internal project ID mismatches in project-member routes.
- Remove write-path fallbacks that allow missing or unregistered project metadata to bypass required
  roles.
- Ensure org membership is a precondition for project membership.
- Gate setup endpoints after initialization.
- Add owner/admin override tests and project-private negative tests.

Files:

- `apps/api/src/sibyl/auth/authorization.py`
- `apps/api/src/sibyl/api/routes/project_members.py`
- `apps/api/src/sibyl/api/routes/entities.py`
- `apps/api/src/sibyl/api/routes/search.py`
- `apps/api/src/sibyl/api/routes/setup.py`
- `apps/api/src/sibyl/persistence/surreal/auth_runtime.py`
- `apps/web/src/lib/api.ts`
- `apps/api/tests/test_project_members.py`
- `apps/api/tests/test_routes_entities*.py`
- `apps/api/tests/test_routes_search.py`
- `apps/api/tests/test_setup_routes.py`

Verify:

- `moon run api:test -- tests/test_project_members.py tests/test_routes_search.py`
- `moon run api:test -- tests/test_routes_entities.py tests/test_routes_entities_write.py`
- `moon run web:typecheck`

B2 progress receipt, 2026-05-13:

- `8199ddf1` filters REST entity list, direct entity reads, and related-summary hydration through
  accessible project IDs. Explicit list scopes now use the auth runtime verifier instead of local
  set membership, and project entities authorize against their own graph project IDs.
- `b9552139` removes write-path project fallbacks for entity, task, and epic mutations by requiring
  registered project records before project-scoped writes proceed.
- The tighter write gate exposed one real dogfood gap: existing graph projects can still lack
  canonical auth-control-plane `projects` records. The next B2 slice must add an owner/admin repair
  path that backfills records from graph project entities, then use that path to repair local
  dogfood data before relying on stricter enforcement.

B2 remaining slices:

1. Add a project-record sync and backfill surface for existing graph project entities.
2. Fix project-member routes so graph project IDs and auth project records resolve consistently.
3. Gate setup endpoints once initialization has completed.
4. Extend search, explore, context, and entity read tests with project-private deny fixtures.
5. Run the B2 route gate, web typecheck, full API policy slice, and independent review.

B2 closure update, 2026-05-13: all five slices above are implemented and verified in the packet
receipts below. Remaining trust work moves to B3/B4/B6 policy context, inspect/audit, and release
gate coverage.

Exit criteria:

- Project-private data does not leak through list, search, explore, or direct entity reads.
- Mutations require the right project role.
- Project membership management works with graph project IDs.

### Wave B3: Unified Policy Context For API, CLI, MCP, And Jobs

Purpose: make every integration call the same policy primitive.

Implementation:

- Extend MCP auth context with user ID, agent identity, delegated authority, org role, and
  accessible project IDs.
- Ensure MCP `remember`, `recall`, `context`, `reflect`, `search`, `explore`, and `manage` pass
  policy context into core services.
- Make CLI commands consume API policy decisions and reason strings instead of duplicating policy.
- Add job payload policy context for task-learning and reflection promotion writes.
- Add deny-case tests for missing agent identity, missing scope key, unverified membership, and
  scope crossing.

Files:

- `apps/api/src/sibyl/server.py`
- `apps/api/src/sibyl/auth/mcp_auth.py`
- `apps/api/src/sibyl/auth/mcp_oauth.py`
- `apps/api/src/sibyl/api/routes/context.py`
- `apps/api/src/sibyl/api/routes/memory.py`
- `apps/api/src/sibyl/jobs/entities.py`
- `apps/cli/src/sibyl_cli/client.py`
- `apps/cli/src/sibyl_cli/main.py`
- `packages/python/sibyl-core/src/sibyl_core/tools/context.py`
- `packages/python/sibyl-core/src/sibyl_core/tools/add.py`
- `packages/python/sibyl-core/src/sibyl_core/tools/reflect.py`
- `apps/api/tests/test_server_accessible_projects.py`
- `apps/api/tests/test_mcp_auth.py`
- `apps/cli/tests/test_context_pack.py`
- `packages/python/sibyl-core/tests/test_memory_policy.py`

Verify:

- `moon run api:test -- tests/test_server_accessible_projects.py tests/test_mcp_auth.py`
- `moon run api:test -- tests/test_routes_context.py tests/test_routes_memory.py`
- `moon run cli:test`
- `moon run core:test -- tests/test_memory_policy.py`

Exit criteria:

- REST, CLI, MCP, and jobs produce matching allow and deny reasons.
- MCP no longer acts as an org-only bypass around project or memory-space policy.

### Wave B4: Audit And Inspect

Purpose: let humans and agents answer why a memory was shown, hidden, written, or promoted.

Implementation:

- Add memory audit events for remember, recall, wake, context pack render, reflect, promotion, share
  preview, and policy denies.
- Add an inspect API and CLI surface for source, derived records, visibility, freshness, policy
  reason, and actor metadata.
- Add redaction metadata for hidden-but-relevant context without leaking hidden text.
- Preserve raw source IDs and derived record IDs in audit and inspect responses.
- Keep audit storage bounded enough for local development.

Files:

- `apps/api/src/sibyl/persistence/surreal/auth_runtime.py`
- `apps/api/src/sibyl/api/routes/memory.py`
- `apps/api/src/sibyl/api/routes/context.py`
- `apps/api/src/sibyl/api/routes/entities.py`
- `apps/cli/src/sibyl_cli/main.py`
- `apps/cli/src/sibyl_cli/client.py`
- `packages/python/sibyl-core/src/sibyl_core/models/context.py`
- `packages/python/sibyl-core/src/sibyl_core/tools/context.py`
- `packages/python/sibyl-core/src/sibyl_core/services/native_memory.py`
- `apps/api/tests/test_routes_memory.py`
- `apps/api/tests/test_routes_context.py`
- `apps/cli/tests/test_context_pack.py`

Verify:

- `moon run api:test -- tests/test_routes_memory.py tests/test_routes_context.py`
- `moon run cli:test`
- `moon run core:test`

Exit criteria:

- Context-pack and memory-write decisions are inspectable.
- Audit events carry actor, scope, source, and policy metadata.
- Hidden relevant context can be indicated without leaking sensitive text.

### Wave B5: Promotion And Share Preview

Purpose: prepare controlled movement from private memory into shared contexts without shipping
unbounded sharing.

Implementation:

- Add promotion preview for private to project, delegated to project, and project to organization
  candidate moves.
- Require explicit target scope and target memory space for every promotion.
- Return stable allow/deny reasons before any write.
- Add share-preview response shape with redactions, hidden-but-relevant counts, and source IDs.
- Keep actual cross-org sharing disabled with `scope_not_enabled`.

Files:

- `apps/api/src/sibyl/api/routes/memory.py`
- `apps/api/src/sibyl/api/schemas.py`
- `packages/python/sibyl-core/src/sibyl_core/auth/memory_policy.py`
- `packages/python/sibyl-core/src/sibyl_core/services/native_memory.py`
- `packages/python/sibyl-core/src/sibyl_core/tools/reflect.py`
- `apps/api/tests/test_routes_memory.py`
- `packages/python/sibyl-core/tests/test_reflect.py`
- `packages/python/sibyl-core/tests/test_memory_policy.py`

Verify:

- `moon run api:test -- tests/test_routes_memory.py`
- `moon run core:test -- tests/test_reflect.py tests/test_memory_policy.py`
- `moon run core:bench-context`

Exit criteria:

- Promotion previews are policy-backed and source-grounded.
- Mixed-scope promotion denies unless the target scope is explicit and allowed.
- Private-leak fixtures remain at zero leaks.

### Wave B6: Memory Trust Release Gate

Purpose: prove the trust layer before post-v0.8 product expansion.

Implementation:

- Run no-leak fixtures across raw memory, context pack, wake, recall, reflect, MCP, and CLI.
- Verify project-private fixtures through REST and MCP.
- Verify audit/inspect receipts for allow and deny cases.
- Verify every trust-sensitive surface returns stable reason codes.
- Record final gate artifacts in Sibyl.

Verify:

- `moon run core:test`
- `moon run api:test`
- `moon run cli:test`
- `moon run core:bench-context`
- `moon run :check`
- CI green
- nightly regression green

Exit criteria:

- v0.8 can claim project-scoped, policy-backed, inspectable memory behavior.
- `synthesize`, sharing UX, and larger personal-corpus import can build on a stable trust layer.

## 6. Suggested Execution Order

1. A0: lock post-v0.7 baseline.
2. B0: update trust inventory against current code.
3. A1: quarantine Graphiti compatibility.
4. B1: introduce memory-scope policy boundaries; defer persisted MemorySpace CRUD.
5. B2: harden project RBAC and setup routes.
6. A2: replace Graphiti-shaped native graph managers.
7. A3: move embeddings to native ownership.
8. B3: unify policy context across API, CLI, MCP, and jobs.
9. B4: add audit and inspect surfaces.
10. A4: delete or move Graphiti ops into a compatibility island.
11. A5: close archive, coordination, and stale docs cleanup.
12. B5: add promotion and share preview.
13. A6 and B6: run release audits together.

A0 and B0 can run in parallel. A2/A3 and B1/B2 touch different centers and can also run in parallel
if agents have disjoint write ownership. B3 should wait for B1 and B2. A4 should wait for A1, A2,
and A3.

## 7. Task Tracking Shape

Sibyl tracking:

- Epic: `v0.8 Pure Surreal Closure`
  - ID: `epic_416f955f7f39`
  - Task `cc561455-0b5f-43a5-a266-2e7852593edc`: lock v0.8 baseline gates
  - Task `25c702de-95e5-452c-8705-d63389aea038`: quarantine Graphiti compatibility
  - Task `1fb2a343-6fc8-4f45-936c-2c0f895009b2`: replace Graphiti-shaped graph managers
  - Task `03e4a386-a556-497b-86bc-b5430e044905`: move embeddings to native ownership
  - Task `61515e7a-f4fd-4ab7-a41f-b8789bf69272`: delete or isolate Graphiti ops package
  - Task `bcfef650-1087-454e-aa30-be3a6bbc9b8a`: close archive, coordination, and legacy docs
    residue
  - Task `1114d0bb-0acc-443a-ab0d-1a830036a9b5`: run pure Surreal release audit
- Epic: `v0.8 Memory Trust Foundation`
  - ID: `epic_539eea7afeb3`
  - Task `373c0eae-fef4-4822-9130-481193d50454`: inventory trust-sensitive memory surfaces
  - Task `00a3beff-88d5-45d3-b5aa-dc52f01cb87a`: add memory-space control plane; deferred post-v0.8
    after the B1 scope correction
  - Task `0b7851f7-44c9-41e5-8036-7bd641d554aa`: harden project RBAC
  - Task `e4a44b56-10f1-4411-a677-5606920c0576`: unify API, CLI, MCP, and job policy context
  - Task `32d31cf2-70b4-4869-b683-8a6fcb5a8220`: add memory audit and inspect surfaces
  - Task `18fd25d9-e3cb-4798-b789-a09dba5e4e08`: add promotion and share preview
  - Task `f66e6310-8c3e-410e-805f-36c52d823910`: run memory trust release gate

Each task should complete with:

- changed files
- exact verification command receipts
- policy or compatibility decisions made
- any remaining risk or deferred follow-up

Tracking integrity follow-up:

- Task `a03051b5-4ac8-449f-b38a-ddb1974f5523`: fix epic progress aggregation for direct task
  tracking. New v0.8 tasks are linked to epics, but `sibyl epic show` currently reports `0/0` totals
  for the new epics. A0/B0 can start while this is tracked, but release receipts should not rely on
  epic rollups until this is fixed or explicitly accounted for.

## 8. Verification Matrix

| Surface                         | Gate                                                                                      |
| ------------------------------- | ----------------------------------------------------------------------------------------- |
| Graphiti boundary               | `moon run inventory-check inventory-typecheck inventory-test`                             |
| Default-loop proof              | `moon run core:no-graphiti-smoke`                                                         |
| Native graph managers           | `moon run core:test -- tests/test_graph_entities.py tests/test_graph_relationships.py`    |
| Native retrieval and embeddings | `moon run core:test -- tests/test_native_retrieval.py` plus `moon run core:bench-context` |
| API graph/runtime               | `moon run api:test`                                                                       |
| Memory policy                   | `moon run core:test -- tests/test_memory_policy.py`                                       |
| Memory API                      | `moon run api:test -- tests/test_routes_memory.py tests/test_routes_context.py`           |
| MCP context                     | `moon run api:test -- tests/test_mcp_auth.py tests/test_server_accessible_projects.py`    |
| CLI policy consumption          | `moon run cli:test`                                                                       |
| Task-learning jobs              | `moon run api:memory-trust-jobs-test`                                                     |
| Project RBAC                    | `moon run api:test -- tests/test_project_members.py tests/test_routes_search.py`          |
| Docs                            | `moon run docs:lint`                                                                      |
| Release                         | `moon run :check`, CI green, nightly green                                                |

## 9. Risk Register

| Risk                                                       | Why It Matters                                             | Mitigation                                                        |
| ---------------------------------------------------------- | ---------------------------------------------------------- | ----------------------------------------------------------------- |
| Compatibility code still imports Graphiti on default paths | Default installs become fragile and larger than advertised | Keep no-Graphiti smoke and inventory gates blocking               |
| Native graph replacements lose legacy visibility           | Older records may disappear from recall                    | Preserve legacy projection rules and fixture native hydration     |
| Embedding metadata drift makes evals noisy                 | Quality gates become untrustworthy                         | Record provider, model, dimensions, tokenizer, and index settings |
| Project RBAC hardening breaks existing dogfood workflows   | Sibyl uses graph project IDs heavily                       | Fix graph-ID resolution first and add owner/admin override tests  |
| MCP remains org-only                                       | It becomes a side channel around policy                    | Make MCP derive the same user/project policy context as REST      |
| Archive cleanup removes recovery paths too early           | Users need a migration and rollback story                  | Set archive policy before deleting code                           |
| Audit logging becomes too heavy for local use              | Trust features should not slow every recall                | Keep initial audit events compact and queryable by source/action  |

## 10. Open Questions

These were answered during execution where they affected v0.8 release scope:

- `organization` memory scope remains disabled until explicit organization memory spaces ship.
- Existing project-private graph entities need canonical project-record repair before strict
  enforcement; B2 added backfill and graph-ID resolution paths.
- Should Graphiti compatibility remain in this repository as an optional extra after v0.8, or move
  to an archive branch once A4 is complete?
- How long should retained `postgres.sql` restore support remain available after v0.8?
- Context-pack audit receipts store IDs plus compact metadata, not hidden source text.
- Share preview landed in both API and CLI, with actual sharing disabled.

## 11. Post-v0.8 Bridge

v0.8 should leave the system ready for:

- `synthesize`: source-grounded large-read artifacts from policy-filtered graph slices.
- Human trust UI: inspect, correct, hide, promote, redact, export, and delete memory.
- Team/shared memory spaces: deliberate sharing with previews and audit trail.
- Personal corpus ingestion: staged import for email, chat, notes, docs, and home-assistant memory.
- Live memory workspace: live capture feed, reflection progress, context-pack preview, and
  permission-change invalidation.

The sequencing matters. `synthesize` and sharing become powerful only after policy, provenance,
audit, and inspection are boring.

The executable post-v0.8 plan lives in
[`SIBYL_POST_V08_SYNTHESIS_AND_MEMORY_WORKSPACE_PLAN.md`](SIBYL_POST_V08_SYNTHESIS_AND_MEMORY_WORKSPACE_PLAN.md).

## 12. Execution Operating Model

This plan should be implemented as small, reviewable commits. Each commit should retire one release
risk and include the tests that prove it. When a wave needs multiple commits, use this loop:

1. Re-read the wave purpose, exit criteria, and current tracked task.
2. Map the touched files before editing and leave unrelated work alone.
3. Implement one narrow behavior change.
4. Run the tightest useful test first, then the wave gate when the slice is stable.
5. Commit with a Conventional Commit subject and a body that explains why the change matters.
6. Capture the learning or decision in Sibyl when the slice changes policy, compatibility, or
   operational behavior.

Non-trivial implementation slices require independent adversarial review before the task is reported
complete. The reviewer should receive the original wave goal, changed files, verification receipts,
and the expected deny or compatibility behavior. A self-check is useful, but it does not replace
that review.

## 13. Atomic Implementation Packets

These packets are the preferred order for the next execution pass. They are smaller than the waves
above so they can land cleanly.

### Packet B2.1: Project Record Backfill

Purpose: repair existing graph projects that predate canonical auth project records.

Files:

- `apps/api/src/sibyl/api/routes/admin.py`
- `apps/api/src/sibyl/persistence/surreal/auth_runtime.py`
- `apps/api/src/sibyl/persistence/auth_runtime.py`
- `apps/api/tests/test_routes_admin.py`
- `docs/architecture/PERMISSION_SYSTEM_AUDIT.md`

Implementation:

- Add an owner/admin-only dry-run and apply surface that lists graph project entities missing auth
  `projects` records.
- Create missing records with the acting owner/admin as owner, organization visibility, and viewer
  default role.
- Report created, existing, skipped, and failed project IDs without leaking private project content.
- Document when to run the repair and why stricter write gates depend on it.

Verify:

- `moon run api:test -- tests/test_routes_admin.py tests/test_surreal_auth_runtime.py`
- `moon run api:lint api:typecheck`
- Dry-run locally before any data write.

Exit criteria:

- Existing graph projects can be repaired without weakening `require_existing_project=True`.
- Local dogfood data can pass stricter project write gates after the repair is applied.

Receipt, 2026-05-13:

- Commit: `406d7cd9`.
- Changed files:
  - `apps/api/src/sibyl/api/routes/admin.py`
  - `apps/api/src/sibyl/api/schemas.py`
  - `apps/api/tests/test_routes_admin.py`
  - `docs/architecture/PERMISSION_SYSTEM_AUDIT.md`
- Verification:
  - `moon run api:test -- tests/test_routes_admin.py tests/test_surreal_auth_runtime.py` -> 66
    passed in 1.41s.
  - `moon run api:lint api:typecheck` -> lint passed; typecheck exited 0 with the existing 63 ty
    warnings.
  - `moon run docs:lint` -> passed.
- Review: Claude cross-model review PASS at
  `/tmp/claude-review-b21-project-record-backfill-1778708133.txt`.
- Remaining risk: live dogfood data still needs a dry-run and explicit apply decision before the
  linked project can use project-scoped writes again.

### Packet B2.2: Project Member Graph-ID Resolution

Purpose: make membership routes use the same graph project ID contract as entity and task routes.

Files:

- `apps/api/src/sibyl/api/routes/project_members.py`
- `apps/api/src/sibyl/persistence/surreal/auth_runtime.py`
- `apps/api/tests/test_project_members.py`

Implementation:

- Accept graph project IDs at route boundaries where the UI and CLI already use them.
- Resolve graph IDs to canonical auth project records before membership reads or writes.
- Require org membership before project membership can be granted.
- Preserve owner/admin overrides while denying unrelated org users.

Verify:

- `moon run api:test -- tests/test_project_members.py`
- `moon run api:test -- tests/test_route_access_seams.py`

Exit criteria:

- Project member management works against graph project IDs.
- Missing project records fail closed with stable reason or status.

Receipt, 2026-05-13:

- Commit: `d0cdea07`.
- Changed files:
  - `apps/api/src/sibyl/persistence/surreal/organization_runtime.py`
  - `apps/api/tests/test_organization_runtime.py`
- Verification:
  - `moon run api:test -- tests/test_routes_project_members.py tests/test_organization_runtime.py`
    -> 40 passed in 1.21s.
  - `moon run api:lint api:typecheck` -> lint passed; typecheck exited 0 with the existing 63 ty
    warnings.
- Review: Claude cross-model review PASS at
  `/tmp/claude-review-b22-project-members-org-invariant-1778708649.txt`.
- Remaining risk: removing an org member still needs a cleanup or cascade follow-up for stale
  `project_members` rows; the route now filters stale rows and still allows explicit removal.

### Packet B2.3: Setup Endpoint Gate

Purpose: prevent setup routes from becoming a post-initialization privilege bypass.

Files:

- `apps/api/src/sibyl/api/routes/setup.py`
- `apps/api/src/sibyl/persistence/setup_common.py`
- `apps/api/src/sibyl/persistence/surreal/setup.py`
- `apps/api/tests/test_setup_routes.py`
- `apps/api/tests/test_surreal_setup.py`
- `apps/web/src/lib/api.ts`
- `apps/web/src/app/setup/page.tsx`

Implementation:

- Gate setup actions after the first owner/admin organization is initialized.
- Keep first-run setup ergonomic for a clean local install.
- Return explicit already-initialized errors to the web client.
- Ensure web setup handling does not treat the gate as a generic network failure.

Verify:

- `moon run api:test -- tests/test_setup_routes.py`
- `moon run web:typecheck`

Exit criteria:

- Setup succeeds for a new install and denies after initialization.
- The web client can display or handle the initialized state cleanly.

B2.3 receipt, 2026-05-13:

- Setup mode now closes only after an owner/admin organization membership exists. This keeps partial
  first-run states recoverable when users or organizations exist without an initialized owner/admin
  org.
- `/setup/status` returns `setup_complete` and sets `needs_setup` from that initialized-org
  invariant. Public key validation through `validate_keys=true` is ignored once setup is complete so
  the status route cannot be used for unauthenticated external API pressure.
- `/setup/validate-keys` now uses the setup-or-owner/admin dependency instead of setup-or-any-auth.
- Setup/admin gating now accepts organization owner/admin roles after initialization and returns a
  structured `setup_already_initialized` detail when an initialized instance is hit without a token.
- The web setup page recognizes the initialized setup error and redirects to login rather than
  rendering the generic connection failure state.
- Review: Claude cross-model review PASS at `/tmp/claude-review-b23-setup-gate-1778710000.txt`; the
  public `/setup/status?validate_keys=true` follow-up was fixed before commit and re-reviewed as
  PASS at `/tmp/claude-review-b23-setup-gate-followup-1778710500.txt`.
- Verification:
  - `moon run api:test -- tests/test_setup_routes.py tests/test_surreal_setup.py`: 11 passed in
    1.13s.
  - `moon run api:test -- tests/test_setup_routes.py tests/test_surreal_setup.py tests/test_settings_routes.py tests/test_operations_runtime.py`:
    23 passed in 1.18s after the review follow-up.
  - `moon run web:test -- src/lib/api.test.ts`: 1 file and 3 tests passed.
  - `moon run api:lint api:typecheck`: lint passed; typecheck exited 0 with the existing 63 ty
    warnings.
  - `moon run web:typecheck`: types generated successfully.
  - `moon run web:lint`: checked 221 files with no fixes applied.

### Packet B2.4: Project-Private Leak Fixtures

Purpose: prove read-side project filtering across every B2 surface.

Files:

- `apps/api/tests/test_routes_entities.py`
- `apps/api/tests/test_routes_entities_read.py`
- `apps/api/tests/test_routes_search.py`
- `apps/api/tests/test_routes_context.py`

Implementation:

- Add fixtures with private project entities, unassigned entities, inaccessible project entities,
  and project entities whose own ID is the project scope.
- Cover list, direct get, search, related summaries, and context-pack candidate hydration.
- Assert hidden results are absent and deny responses carry stable status or reason.

Verify:

- `moon run api:test -- tests/test_routes_entities.py tests/test_routes_entities_read.py`
- `moon run api:test -- tests/test_routes_search.py tests/test_routes_context.py`

Exit criteria:

- No project-private fixture leaks through the B2 read surfaces.
- Tests cover both implicit accessible-project scopes and explicit requested project scopes.

B2.4 receipt, 2026-05-13:

- Added a shared core project-policy helper that treats project entities as scoped by their own
  graph entity ID when `project_id` metadata is absent.
- Search, explore list, explore related/traverse, explore dependencies, and context-pack related
  hydration now use project-aware policy IDs for project filters and accessible-project filters.
- REST explore multi-project filters now verify each requested project through
  `verify_entity_project_access()` instead of comparing against the default accessible-project set.
- Added no-leak fixtures for entity list, direct entity related hydration, search/explore route
  policy plumbing, core search/explore project entities, explore related/traverse, explore
  dependencies, and context-pack related hydration.
- Review: Claude cross-model review initially failed on explore related/traverse project-entity
  filtering, then passed after that fix. A final pass also verified the dependencies-mode follow-up:
  `/tmp/claude-review-b24-project-private-fixtures-final-1778781400.txt`.
- Verification:
  - `moon run api:test -- tests/test_routes_entities.py tests/test_routes_entities_read.py`: 26
    passed in 1.25s.
  - `moon run api:test -- tests/test_routes_search.py tests/test_routes_context.py`: 19 passed in
    1.28s.
  - `moon run core:test -- tests/test_tools.py tests/test_context_pack.py`: 1331 passed and 15
    skipped in 8.78s.
  - `moon run api:lint api:typecheck`: lint passed; typecheck exited 0 with the existing 63 ty
    warnings.
  - `moon run core:lint core:typecheck`: lint passed; typecheck exited 0 with the existing 26 ty
    warnings.
  - `git diff --check`: passed.

B2.5 route gate receipt, 2026-05-13:

- Route-gate coverage includes project-record backfill, project members, search/explore, entity
  list/get/write, setup gating, and context/reflect route policy plumbing.
- `moon run api:test -- tests/test_routes_admin.py tests/test_routes_project_members.py tests/test_routes_search.py`:
  35 passed in 1.25s.
- `moon run api:test -- tests/test_routes_entities.py tests/test_routes_entities_read.py tests/test_routes_entities_write.py tests/test_setup_routes.py tests/test_surreal_setup.py tests/test_routes_context.py`:
  58 passed in 1.25s.
- `moon run web:typecheck`: types generated successfully from cache.
- Independent review for the final B2.4/B2.5 policy closure passed at
  `/tmp/claude-review-b24-project-private-fixtures-final-1778781400.txt`.

### Packet B3.1: Policy Context Contract

Purpose: define the shared payload that REST, MCP, CLI, jobs, and core services pass around.

Files:

- `packages/python/sibyl-core/src/sibyl_core/auth/context.py`
- `packages/python/sibyl-core/src/sibyl_core/auth/memory_policy.py`
- `apps/api/src/sibyl/auth/mcp_auth.py`
- `apps/api/src/sibyl/server.py`
- `packages/python/sibyl-core/tests/test_memory_policy.py`
- `apps/api/tests/test_mcp_auth.py`

Implementation:

- Add fields for actor user ID, agent identity, delegated authority, organization role, project
  access, memory space, and source surface.
- Make missing actor, missing scope, and unverified membership produce stable deny reasons.
- Route MCP auth through the same context model used by REST.

Verify:

- `moon run core:test -- tests/test_memory_policy.py`
- `moon run api:test -- tests/test_mcp_auth.py tests/test_server_accessible_projects.py`

Exit criteria:

- Policy decisions can be compared across REST and MCP without special-case translation.

B3.1 receipt, 2026-05-13:

- Added `MemoryPolicyContext` as the shared actor, organization role, project access, delegation,
  memory-space, scope-key, agent, and source-surface payload for memory policy calls.
- REST raw memory routes and MCP remember authorization now evaluate memory writes/reads through the
  shared policy context while preserving legacy `authorize_memory_*` kwargs callers.
- Stable deny guards now cover missing actors, missing memory space, missing project/delegation
  scope keys, and unverified project/delegation membership.
- `moon run core:test -- tests/test_memory_policy.py tests/test_auth_contracts.py`: 1340 passed, 15
  skipped in 8.83s.
- `moon run api:test -- tests/test_routes_memory.py tests/test_server_accessible_projects.py tests/test_auth_mcp_token_verifier.py`:
  42 passed in 1.28s.
- `moon run core:lint core:typecheck`: lint passed; typecheck exited 0 with the existing 26 ty
  diagnostics.
- `moon run api:lint api:typecheck`: lint passed; typecheck exited 0 with the existing 63 ty
  diagnostics.
- `git diff --check`: passed.
- Independent review passed at `/tmp/claude-review-b31-policy-context-1778713220.txt`; follow-up
  regression tests were added for the review's test-gap notes.

### Packet B4.1: Audit Event Skeleton

Purpose: give memory trust work one compact audit record before adding more surfaces.

Files:

- `apps/api/src/sibyl/persistence/surreal/auth_runtime.py`
- `apps/api/src/sibyl/persistence/auth_runtime.py`
- `apps/api/src/sibyl/api/routes/memory.py`
- `apps/api/tests/test_routes_memory.py`
- `apps/api/tests/test_surreal_auth_runtime.py`

Implementation:

- Persist compact audit events for raw remember, raw recall, reflection promotion, non-policy
  promotion failures, memory policy denies, and project-filter denies.
- Include actor, organization, project or memory space, action, source IDs, derived IDs, policy
  decision, and reason.
- Keep event payloads bounded and queryable by actor, action, source, and time.
- Leave context render, wake, inspect API, and CLI inspect for the next B4 packet.

Verify:

- `moon run api:test -- tests/test_routes_memory.py`
- `moon run core:test -- tests/test_memory_policy.py`

Exit criteria:

- At least one allowed case and one denied case produce inspectable audit receipts.

B4.1 receipt, 2026-05-13:

- Added `log_memory_audit_event` to the auth runtime facade and Surreal auth backend. Payloads now
  bound top-level strings, source IDs, derived IDs, nested mappings, lists, and deep values before
  writing through `audit_logs`.
- Raw memory routes emit audit receipts for remember and recall successes, memory-policy denies, and
  project-filter denies. Reflection promotion emits success, policy-denial, and missing-candidate
  receipts without conflating action success with policy state.
- Audit failures are fail-open for user operations and warning-logged with exception context.
- `moon run api:test -- tests/test_routes_memory.py tests/test_surreal_auth_runtime.py`: 65 passed
  in 1.43s.
- `moon run core:test -- tests/test_memory_policy.py`: 1340 passed, 15 skipped.
- `moon run api:lint api:typecheck`: lint passed; typecheck exited 0 with the existing 63 ty
  diagnostics.
- Independent review passed at `/tmp/claude-review-b41-memory-audit-final2-20260513.txt`; no
  commit-blocking findings remained.
- Remaining B4 risk: memory audit routes still pass `request=None`, so IP address and user-agent
  capture remain follow-up work with the inspect surfaces.

### Packet B4.2: Audit Receipt Inspect

Purpose: make the B4.1 audit receipts inspectable by owners and admins without exposing hidden
memory content to ordinary readers.

Files:

- `apps/api/src/sibyl/api/routes/memory.py`
- `apps/api/src/sibyl/api/schemas.py`
- `apps/api/src/sibyl/persistence/auth_runtime.py`
- `apps/api/src/sibyl/persistence/surreal/auth_runtime.py`
- `apps/cli/src/sibyl_cli/client.py`
- `apps/cli/src/sibyl_cli/main.py`
- `apps/api/tests/test_routes_memory.py`
- `apps/api/tests/test_surreal_auth_runtime.py`
- `apps/cli/tests/test_main_capture.py`

Implementation:

- Add an owner/admin-only `GET /memory/audit` API endpoint returning compact audit receipts.
- Add filters for actor, action, source ID, derived ID, memory scope, project ID, policy state, and
  bounded result count.
- Add `sibyl memory-audit` so agents can inspect receipts from the CLI and emit JSON when needed.
- Keep filtering source and derived IDs from bounded `details` fields while querying `audit_logs`
  through static SurrealQL statements.

Verify:

- `moon run api:test -- tests/test_routes_memory.py tests/test_surreal_auth_runtime.py`
- `moon run cli:test -- tests/test_main_capture.py`
- `moon run api:lint api:typecheck cli:lint cli:typecheck`
- `moon run docs:format`
- `moon run docs:lint`
- `git diff --check`

Exit criteria:

- Owners and admins can list memory audit receipts with policy, source, derived, scope, project, and
  actor metadata.
- The CLI exposes the same filters as the API.
- Audit readback stays bounded and does not reveal hidden memory text.

B4.2 receipt, 2026-05-13:

- Added `MemoryAuditEventResponse` and `MemoryAuditListResponse` as the typed readback contract for
  compact audit receipts.
- Added `list_memory_audit_events` to the auth runtime facade and Surreal backend. The backend uses
  fixed SurrealQL query shapes for organization, actor, and action filters, then applies bounded
  in-process matching for source ID, derived ID, scope, project, policy state, and memory-prefixed
  actions.
- Added owner/admin-gated `GET /memory/audit` and `sibyl memory-audit` with matching filters and
  JSON output support.
- Review follow-up pushed the memory action prefix into the SurrealQL scan path, rejects
  non-`memory.*` action filters before querying, documents audit `details` as metadata-only, and
  renders truncated source/derived ID counts in the CLI table.
- `moon run api:test -- tests/test_routes_memory.py tests/test_surreal_auth_runtime.py`: 69 passed
  in 4.00s.
- `moon run cli:test -- tests/test_main_capture.py`: 157 passed in 1.44s.
- `moon run api:lint api:typecheck cli:lint cli:typecheck`: lint passed for API and CLI; CLI
  typecheck passed; API typecheck exited 0 with 63 existing ty diagnostics.
- Independent review passed at `/tmp/claude-review-b42-memory-audit-inspect-20260513.txt`; the
  scan-window and action-filter follow-ups were fixed and re-reviewed as PASS at
  `/tmp/claude-review-b42-memory-audit-inspect-followup-20260513.txt`.
- Remaining B4 risk: this packet inspects audit receipts only. Context render, wake, and source
  visibility inspect paths still need their own B4 packets, and IP/user-agent capture remains
  deferred from B4.1.

### Packet B4.3: Context Pack Render Audit

Purpose: make context pack rendering, including wake-context renders, leave the same compact
metadata-only audit trail as raw memory surfaces.

Files:

- `apps/api/src/sibyl/api/context_audit.py`
- `apps/api/src/sibyl/api/routes/context.py`
- `apps/api/src/sibyl/server.py`
- `apps/api/tests/test_routes_context.py`
- `apps/api/tests/test_server_accessible_projects.py`

Implementation:

- Add a shared context audit helper that records `memory.context_pack` receipts after a context pack
  is compiled and rendered.
- Cover REST `/context/pack` and the MCP `context` tool with the same receipt shape, using
  `source_surface` values of `context_pack` and `mcp_context`.
- Include actor, organization, memory scope, project, source IDs, derived item IDs, policy state,
  layer, intent, result count, section count, related-context settings, and accessible-project count
  without storing hidden memory text.
- Treat explicit project renders as `project` scope and unscoped context renders as `mixed` scope so
  blended private plus accessible-project context is not mislabeled as private-only.
- Use the existing owner/admin `memory-audit` inspect path from B4.2 for source and derived ID
  filtering.

Verify:

- `moon run api:test -- tests/test_routes_context.py tests/test_server_accessible_projects.py`
- `moon run api:lint api:typecheck`
- `moon run docs:format`
- `moon run docs:lint`
- `git diff --check`

Exit criteria:

- REST context pack renders emit metadata-only audit receipts that can be filtered through
  `GET /memory/audit` and `sibyl memory-audit`.
- MCP context renders emit the same receipt action with a distinct source surface.
- Wake renders are covered by receipt metadata with `details.layer == "wake"`.
- Audit failures remain fail-open and warning-logged through the shared helper.

B4.3 receipt, 2026-05-13:

- Added `sibyl.api.context_audit.log_context_pack_audit` to emit bounded source IDs, derived item
  IDs, policy metadata, render settings, result counts, and layer/intent metadata for context pack
  renders.
- REST `/context/pack` now records `memory.context_pack` receipts after successful render
  validation.
- The MCP `context` tool now delegates through `_compile_mcp_context_pack`, preserving behavior
  while adding the same audit receipt surface for MCP callers.
- Tests assert REST project wake receipts, REST mixed-scope receipts, MCP project wake receipts,
  source IDs, derived IDs, project scope, source surfaces, policy state, and accessible-project
  counts.
- `moon run api:test -- tests/test_routes_context.py tests/test_server_accessible_projects.py`: 32
  passed in 1.20s, then 33 passed in 1.18s after adding the mixed-scope guard.
- `moon run api:lint api:typecheck`: lint passed; typecheck exited 0 with the existing 63 ty
  diagnostics.
- `moon run docs:format docs:lint`: passed.
- `git diff --check`: passed.
- Independent review passed at `/tmp/claude-review-b43-context-pack-audit-20260513170901.txt`;
  follow-up review passed at
  `/tmp/claude-review-b43-context-pack-audit-followup-20260513171420.txt`; final exact-diff review
  passed at `/tmp/claude-review-b43-context-pack-audit-final-20260513172003.txt`.
- Sibyl memory captured as `procedure_b465e378996c` with raw source
  `fd079334-e1ec-436a-8e2b-3b8bc407b9cd`.
- Remaining B4 risk: IP address and user-agent capture remain deferred from B4.1. Source visibility
  is inspectable by source and derived IDs through the audit API and CLI, but audit receipts
  intentionally do not expose hidden memory text.

### Packet B4.4: Reflection Render Audit

Purpose: make reflection renders and optional reflection persistence leave compact metadata-only
audit receipts before B5 promotion/share work builds on the review queue.

Files:

- `apps/api/src/sibyl/api/context_audit.py`
- `apps/api/src/sibyl/api/routes/context.py`
- `apps/api/src/sibyl/server.py`
- `apps/api/tests/test_routes_context.py`
- `apps/api/tests/test_server_accessible_projects.py`

Implementation:

- Extend the shared context audit helper with `memory.reflect` receipts for reflection packs.
- Cover REST `/context/reflect` and the MCP `reflect` helper with source surfaces of
  `context_reflect` and `mcp_reflect`.
- Include actor, organization, memory scope, project, source IDs, persisted/review IDs, policy
  state, candidate counts, persisted counts, persist settings, active-task/link counts, and
  accessible-project count without storing reflection content.
- Treat explicit project reflection as `project` scope and unscoped reflection as `private` scope.
- Derive policy state from reflection candidate policy metadata when persistence policy runs, and
  otherwise record a successful render reason.

Verify:

- `moon run api:test -- tests/test_routes_context.py tests/test_server_accessible_projects.py`
- `moon run api:lint api:typecheck`
- `moon run docs:format`
- `moon run docs:lint`
- `git diff --check`

Exit criteria:

- REST reflection renders emit metadata-only audit receipts for inspect.
- MCP reflection renders emit the same receipt action with a distinct source surface.
- Reflection receipts include persisted/review IDs when persistence creates them.
- Audit failures remain fail-open and warning-logged through the shared helper.

B4.4 receipt, 2026-05-13:

- Added `sibyl.api.context_audit.log_reflection_audit` for bounded reflection source IDs, derived
  persisted/review IDs, policy metadata, persist settings, candidate counts, and link counts.
- REST `/context/reflect` records `memory.reflect` after successful response validation.
- MCP reflection now records `memory.reflect` with `source_surface=mcp_reflect` after rendering.
- Tests assert REST project reflection receipts, persisted IDs, raw source IDs, policy state, and
  MCP reflection receipts with active-task link counts.
- `moon run api:test -- tests/test_routes_context.py tests/test_server_accessible_projects.py`: 34
  passed in 1.21s, then 36 passed in 1.22s after adding render-only and policy-denied guards.
- `moon run api:lint api:typecheck`: lint passed; typecheck exited 0 with the existing 63 ty
  diagnostics.
- `moon run docs:format docs:lint`: passed.
- `git diff --check`: passed.
- Independent review passed at `/tmp/claude-review-b44-reflection-audit-20260513172938.txt`; final
  exact-diff review passed at `/tmp/claude-review-b44-reflection-audit-final-20260513173458.txt`.
- Sibyl memory captured as `procedure_e14255087d07` with raw source
  `1bbea26d-6219-40d5-8023-297dbd2cf2b2`.
- Remaining B4 risk: IP address and user-agent capture remain deferred from B4.1. Denied reflection
  project access still fails before render and does not emit `memory.reflect`; project filter denies
  are already audited on raw memory routes.

### Packet B4.5: REST Audit Request Attribution

Purpose: close the B4.1 REST attribution gap by threading the FastAPI request object into memory
audit receipts so the Surreal audit writer can capture IP address and user-agent metadata.

Files:

- `apps/api/src/sibyl/api/context_audit.py`
- `apps/api/src/sibyl/api/routes/context.py`
- `apps/api/src/sibyl/api/routes/memory.py`
- `apps/api/tests/test_routes_context.py`
- `apps/api/tests/test_routes_memory.py`
- `apps/api/tests/test_surreal_auth_runtime.py`

Implementation:

- Add a typed request auto-inject sentinel for direct route-function calls while keeping FastAPI
  route annotations as `Request`.
- Thread REST request attribution through raw remember, raw recall, reflection promotion, memory
  policy-deny, project-filter-deny, context pack, and reflection audit receipts.
- Keep MCP audit receipts requestless until FastMCP exposes a trustworthy request object to the tool
  layer.
- Leave audit `details` content-only metadata unchanged; IP address and user-agent remain top-level
  audit-log fields owned by the backend writer.

Verify:

- `moon run api:test -- tests/test_routes_memory.py tests/test_routes_context.py tests/test_surreal_auth_runtime.py`
- `moon run api:lint api:typecheck`
- `moon run docs:format`
- `moon run docs:lint`
- `git diff --check`

Exit criteria:

- REST memory and context audit receipts can carry backend-extracted IP address and user-agent.
- Existing direct route tests can still call route functions without constructing FastAPI request
  objects.
- Audit receipt details remain bounded, inspectable, and free of hidden memory text.

B4.5 receipt, 2026-05-13:

- REST raw memory routes now pass request attribution into `memory.remember`, `memory.recall`,
  `memory.reflect.promote`, and deny receipts emitted before the operation runs.
- REST `/context/pack` and `/context/reflect` now pass the FastAPI request into their shared audit
  helpers, and those helpers forward it to `log_memory_audit_event`.
- The Surreal audit writer already stores request client IP and `user-agent` as top-level audit-log
  fields; tests now prove that extraction path directly.
- `moon run api:test -- tests/test_routes_memory.py tests/test_routes_context.py tests/test_surreal_auth_runtime.py`:
  87 passed in 1.48s.
- `moon run api:lint api:typecheck`: lint passed; typecheck exited 0 with the existing 63 ty
  diagnostics.
- Independent review passed at `/tmp/claude-review-b45-request-attribution-20260513175105.txt`;
  minor notes were either clarified in code or recorded as follow-up risk.
- Sibyl memory captured as `procedure_f2898d799f05` with raw source
  `44afec97-002b-4b3c-be7e-66df4bab48a6`.
- Remaining B4 risk: MCP memory/context audit receipts still do not include request attribution
  because the current tool layer does not provide a request object. Denied context and reflection
  project access can still fail before action-specific render receipts are emitted. Deployments
  behind a reverse proxy still need proxy-header handling if audit IPs should represent the original
  client instead of the proxy.

### Remaining Packet Map

The packets below are the remaining full execution plan for v0.8. Each packet should land as one
atomic commit unless the implementation exposes a smaller natural boundary. Every packet needs
targeted tests, lint/typecheck for touched packages, `git diff --check`, a receipt in this document,
and independent review before the owning task is marked complete.

Receipt updates in this document are part of the packet's atomic commit when they record that
packet's behavior and verification. Standalone planning updates stay doc-only.

Status notes:

- B2 implementation is closed by the receipts above. The Sibyl task state may still lag the packet
  receipts and should not be used as release evidence by itself.
- B3 has the shared policy context contract, MCP parity, CLI policy consumption, and task-learning
  job policy payloads through B3.4.
- B4 has audit storage, audit readback, context render audit, reflection render audit, REST request
  attribution, source inspect, and denied-render audit. The remaining B4 risk is that MCP denied
  render receipts stay requestless until the MCP tool layer exposes request metadata.
- B5 reflection promotion preview, share preview, and CLI preview surfaces have receipts.
- Track A pure-Surreal closure packets have local receipts through A6. Pushed-main CI and nightly
  receipts remain the release blocker.

### Packet B3.2: MCP Tool Policy Parity

Purpose: make every trust-sensitive MCP memory tool call through the same policy context contract as
REST.

Depends on:

- B3.1 policy context contract.
- B2 project access hardening.

Files:

- `apps/api/src/sibyl/server.py`
- `apps/api/src/sibyl/auth/mcp_auth.py`
- `packages/python/sibyl-core/src/sibyl_core/tools/add.py`
- `packages/python/sibyl-core/src/sibyl_core/tools/context.py`
- `packages/python/sibyl-core/src/sibyl_core/tools/reflect.py`
- `packages/python/sibyl-core/src/sibyl_core/tools/search.py`
- `packages/python/sibyl-core/src/sibyl_core/tools/manage.py`
- `apps/api/tests/test_server_accessible_projects.py`
- `apps/api/tests/test_auth_mcp_token_verifier.py`
- `packages/python/sibyl-core/tests/test_tools.py`
- `packages/python/sibyl-core/tests/test_context_pack.py`

Implementation:

- Thread `MemoryPolicyContext` through MCP `remember`, `recall`, `context`, `reflect`, `search`,
  `explore`, and task-learning surfaces.
- Preserve existing MCP response shapes while adding stable deny reasons in metadata where the tool
  already returns structured output.
- Make delegated-agent calls carry agent identity and delegated authority instead of collapsing to
  organization membership alone.
- Ensure accessible project IDs are computed once per MCP request and passed into core services.
- Add negative tests for missing actor, missing scope key, inaccessible project, and disabled
  organization/team/shared scopes.

Split rule:

- If this touches more than one tool cluster deeply, split into one commit for add/search/explore,
  one commit for context/reflect, and one commit for task-learning. Each split still uses the same
  exit criteria and review contract.

Verify:

- `moon run api:test -- tests/test_server_accessible_projects.py tests/test_mcp_auth.py`
- `moon run core:test -- tests/test_tools.py tests/test_context_pack.py tests/test_memory_policy.py`
- `moon run api:lint api:typecheck core:lint core:typecheck`

Exit criteria:

- MCP cannot read or write project-private memory unless the same REST policy would allow it.
- MCP deny reasons match the shared memory policy contract.
- Agent identity and delegation metadata survive into audit receipts when available.

B3.2 receipt, 2026-05-14:

- Added a top-level MCP add helper so the registered MCP `add` tool now resolves project access
  before calling the core graph write path.
- MCP `add` now requires a project when credentials are project-restricted, rejects missing actor
  context through the shared `principal_mismatch` policy reason, and returns `policy_reason` in the
  structured response.
- MCP `add` copies metadata before adding organization and actor attribution, avoiding caller-owned
  metadata mutation while preserving the existing core add response shape.
- Added a top-level MCP manage helper so task, epic, and task-analysis actions resolve the target
  project before calling the core manage path.
- MCP `manage` now applies the shared project write policy to task learning and workflow actions,
  blocks inaccessible task projects with `unverified_membership`, blocks missing actor context with
  `principal_mismatch`, and copies caller data before adding organization/user attribution.
- Added MCP tests for delegated identity preservation, project-scoped add metadata, restricted
  unscoped add denial, add missing actor denial, task-learning metadata, inaccessible task-project
  denial, project-ID action admin scope, and manage missing actor denial.
- The B3.2 verification path now uses `tests/test_auth_mcp_token_verifier.py`; the old
  `tests/test_mcp_auth.py` path no longer exists.
- `moon run api:test -- tests/test_server_accessible_projects.py tests/test_auth_mcp_token_verifier.py`:
  34 passed in 0.86s.
- `moon run core:test -- tests/test_tools.py tests/test_context_pack.py tests/test_memory_policy.py`:
  885 passed, 14 skipped, and 20 deselected in 5.38s.
- `moon run api:lint api:typecheck core:lint core:typecheck`: lint passed; typecheck exited 0 with
  the existing 62 API and 25 core diagnostics.
- `moon run memory-trust-gate`: PASS with 6 checks and 0 failed.
- `moon run docs:lint`: passed.
- Independent review passed at `/tmp/claude-review-b32-mcp-parity.5Etjcn`. Follow-up review passed
  at `/tmp/claude-review-b32-mcp-parity-followup.wJkktL` after MCP manage policy checks and admin
  project-ID coverage were added.
- Residual accepted risk: MCP source operations (`crawl`, `sync`, `refresh`, and `link_graph*`)
  remain organization-scoped rather than project-policy scoped. They are not memory capture or
  task-learning writes in this packet.

### Packet B3.3: CLI Policy Consumption

Purpose: make the CLI display API policy decisions instead of reinterpreting policy locally.

Depends on:

- B3.1 policy context contract.
- B3.2 MCP parity for shared response metadata.
- B4.2 audit inspect response shape.

Files:

- `apps/cli/src/sibyl_cli/client.py`
- `apps/cli/src/sibyl_cli/main.py`
- `apps/cli/tests/test_main_capture.py`
- `apps/cli/tests/test_context_pack.py`
- `apps/api/src/sibyl/api/schemas.py`

Implementation:

- Normalize CLI output for allowed, denied, hidden, and redacted memory decisions.
- Render stable reason codes in human output and preserve exact API metadata in JSON output.
- Avoid duplicating scope policy checks in CLI commands. The CLI should send intent and scope, then
  render the API decision.
- Cover `remember`, `recall`, `context`, `reflect`, `memory-audit`, and task-learning commands.

Verify:

- `moon run cli:test`
- `moon run cli:lint cli:typecheck`
- `moon run api:test -- tests/test_routes_memory.py tests/test_routes_context.py`

Exit criteria:

- CLI policy behavior can be compared directly with REST fixtures.
- JSON output includes reason codes and source IDs needed by agents.
- Human output makes hidden or denied context understandable without leaking hidden text.

B3.3 receipt, 2026-05-14:

- CLI memory capture now renders API-provided policy reasons instead of reinterpreting policy
  locally. Raw and diary `remember` responses print `Policy: <reason>`, and graph-backed `remember`
  preserves raw memory source IDs and `raw_policy_reason` in JSON output.
- Raw and diary recall/search render `policy=<reason>` per raw-memory item while passing query,
  scope, diary, agent, project, and limit intent to the API.
- Context pack and reflection commands render the API/server markdown and response metadata rather
  than rechecking memory scope in the CLI.
- `memory-audit` supports API-backed filters, including `--policy allowed|denied`, and displays
  allowed/denied state plus bounded source and derived ID summaries.
- `memory-inspect` renders API `policy_allowed`, `policy_reason`, and `content_redacted` fields so
  hidden or project-private content remains redacted while source and audit metadata stay
  inspectable.
- Promotion and share preview CLI commands render API preview decisions, source IDs, denied/missing
  IDs, redaction counts, hidden-relevant counts, reason codes, and optional audit IDs without
  enabling non-preview sharing.
- Task completion with `--learnings` still delegates policy to the API/job path. Human output now
  says task learning capture was queued, and JSON output preserves API policy metadata for scripted
  callers.
- `moon run cli:test`: 167 passed in 1.11s.
- `moon run cli:lint cli:typecheck`: CLI lint and typecheck passed.
- `moon run api:test -- tests/test_routes_memory.py tests/test_routes_context.py`: 48 passed in
  1.22s.
- `moon run docs:lint`: all matched files use Prettier style.
- Independent final review passed at `/tmp/claude-review-b33-final.2Aou9B`; the earlier review at
  `/tmp/claude-review-b33-cli-policy.MUdCLb` flagged the async task-learning wording, and the CLI
  now says the capture was queued.
- Residual accepted risk: CLI error output for nonzero JSON invocations still uses the shared prose
  error renderer. Future scripted clients may want a structured error envelope on stderr.

### Packet B3.4: Job Policy Payloads

Purpose: stop asynchronous memory writes from losing actor, project, and delegation context after
the initiating request exits.

Depends on:

- B3.1 policy context contract.
- B4.1 audit event skeleton.

Files:

- `apps/api/src/sibyl/api/routes/tasks.py`
- `apps/api/src/sibyl/coordination/broker.py`
- `apps/api/src/sibyl/coordination/_local/broker.py`
- `apps/api/src/sibyl/coordination/_redis/broker.py`
- `apps/api/src/sibyl/jobs/entities.py`
- `apps/api/src/sibyl/jobs/queue.py`
- `apps/api/src/sibyl/server.py`
- `apps/api/tests/test_jobs_entities.py`
- `apps/api/tests/test_jobs_queue.py`
- `apps/api/tests/test_routes_tasks.py`
- `apps/api/tests/test_server_accessible_projects.py`
- `apps/api/tests/test_tools_manage.py`
- `packages/python/sibyl-core/src/sibyl_core/tools/manage.py`
- `packages/python/sibyl-core/tests/test_tools_manage.py`

Implementation:

- Add a serializable policy-context payload to task-learning jobs.
- Carry the same policy payload from REST task completion and MCP `manage complete_task`.
- Fail closed when a task-learning job receives a project-scoped write without actor and project
  policy context.
- Record audit receipts for job allow and deny outcomes with `source_surface=job`.
- Keep payloads compact and avoid storing raw memory text inside job metadata.
- Leave reflection persistence and promotion on their current synchronous path; their policy
  receipts are covered by the B4/B5 route receipts.

Verify:

- `moon run core:test -- tests/test_tools_manage.py`
- `moon run api:test -- tests/test_tools_manage.py tests/test_server_accessible_projects.py tests/test_jobs_entities.py tests/test_routes_tasks.py tests/test_jobs_queue.py tests/test_coordination_local.py`
- `moon run api:lint api:typecheck core:lint core:typecheck`
- `moon run memory-trust-gate`
- `moon run docs:lint`
- `git diff --check`

Exit criteria:

- Async task-learning writes apply the same policy as synchronous route calls.
- Task-learning job retries cannot bypass project membership or disabled-scope checks.
- Audit receipts identify the originating actor and job source surface.

B3.4 receipt, 2026-05-14:

- Added a serializable memory policy context payload for queued task-learning jobs.
- REST task completion now captures the verified task project, actor, organization role, accessible
  project set from `list_accessible_project_graph_ids`, and `source_surface=task_learning_job`
  before enqueueing learning episode and procedure jobs.
- MCP `manage complete_task` now passes `organization_id` into `manage`, authorizes the same policy
  context before transition, and queues policy-stamped learning jobs instead of creating synchronous
  derived memories. The manage boundary also rejects cross-organization policy payloads before
  reading or transitioning the task.
- Redis and local queue brokers now preserve that policy payload when enqueueing
  `create_learning_episode` and `create_learning_procedure`.
- Learning jobs restore the payload into `MemoryPolicyContext`, authorize the graph write through
  `authorize_memory_write`, and fail before native graph writes when the payload is missing or does
  not verify project membership, when the payload project does not match the queued task, or when
  the payload organization does not match the job group.
- Allowed learning jobs add policy metadata to the derived episode/procedure and emit metadata-only
  `memory.task_learning.episode` / `memory.task_learning.procedure` audit receipts with
  `source_surface=job`.
- Denied manage and job paths emit metadata-only deny receipts before returning or raising, without
  storing raw task learning text in job metadata.
- Reflection persistence and reflection promotion are synchronous in the current Surreal-native
  runtime, so B3.4 does not add reflection/promotion job payloads; their policy receipts are covered
  by the B4/B5 route receipts.
- Cross-model review `/tmp/claude-review-b34-job-policy-followup.kBTzHk` found the route payload
  needed real accessible-project membership, manage denials needed audit receipts, and workers
  needed an organization mismatch guard; all three are now covered by tests.
  `/tmp/claude-review-b34-postfix.2HxzAB` passed after the project-mismatch hardening and
  metadata-only audit detail assertions. `/tmp/claude-review-b34-final2.iBbCrz` passed the final
  commit-blocking review with no findings.
- `moon run core:test -- tests/test_tools_manage.py`: 888 passed, 14 skipped, 20 deselected in
  5.22s.
- `moon run api:test -- tests/test_tools_manage.py tests/test_server_accessible_projects.py tests/test_jobs_entities.py tests/test_routes_tasks.py tests/test_jobs_queue.py tests/test_coordination_local.py`:
  106 passed, 6 deselected in 1.33s.
- `moon run api:lint api:typecheck core:lint core:typecheck`: exit 0; lint clean, with the existing
  ty warning baseline still reported.
- `moon run memory-trust-gate`: PASS, 6 passed and 0 failed.
- `moon run docs:lint`: all matched files use Prettier style.
- `git diff --check`: clean.

Residual risks:

- Project-less MCP `manage complete_task` with learnings still fails closed under the existing
  project-scoped MCP write policy; that is a pre-existing policy gap for private task learning.
- `sibyl-core` still lazily imports API queue helpers for async task-learning jobs. The import is
  isolated and tested, but the layering should be retired during the pure Surreal service split.
- Existing queued jobs without policy payloads fail closed; operators should expect denial receipts
  instead of silent learning writes for old in-flight jobs.
- Audit writes remain best-effort for service availability. Authorization failure remains closed,
  but receipt persistence can still fail independently.

### Packet B4.6: Memory Source Inspect

Purpose: let owners/admins inspect a memory source, its derived records, visibility, and policy
metadata without reading hidden content by accident.

Depends on:

- B4.2 audit receipt inspect.
- B4.3 and B4.4 render audit receipts.

Files:

- `apps/api/src/sibyl/api/routes/memory.py`
- `apps/api/src/sibyl/api/schemas.py`
- `apps/api/src/sibyl/persistence/auth_runtime.py`
- `apps/api/src/sibyl/persistence/surreal/auth_runtime.py`
- `packages/python/sibyl-core/src/sibyl_core/services/native_memory.py`
- `apps/cli/src/sibyl_cli/client.py`
- `apps/cli/src/sibyl_cli/main.py`
- `apps/api/tests/test_routes_memory.py`
- `apps/api/tests/test_surreal_auth_runtime.py`
- `apps/cli/tests/test_main_capture.py`

Implementation:

- Add an owner/admin `GET /memory/inspect/{source_id}` endpoint.
- Return raw source metadata, derived IDs, derived types, review state, memory scope, scope key,
  project ID, freshness timestamps, policy metadata, and recent audit receipt summaries.
- Redact content fields unless the actor is allowed to read that source through normal memory
  policy.
- Add `sibyl memory-inspect <source-id>` with table and JSON output.
- Keep source ID, derived ID, and audit ID filters bounded and static-query backed.

Verify:

- `moon run api:test -- tests/test_routes_memory.py tests/test_surreal_auth_runtime.py`
- `moon run cli:test -- tests/test_main_capture.py`
- `moon run api:lint api:typecheck cli:lint cli:typecheck`

Exit criteria:

- Owners/admins can explain why a memory exists and where it was used.
- Hidden or project-private content stays redacted for actors without read permission.
- Inspect output includes enough source and policy metadata for release evidence.

B4.6 receipt, 2026-05-14:

- Added owner/admin `GET /memory/inspect/{source_id:path}` for source-level memory inspection.
- The endpoint scopes raw memory lookup to the current organization, tries raw memory UUID first,
  and falls back to the indexed raw `source_id` provenance field.
- Inspect responses include raw source metadata, memory scope, scope key, project ID, review state,
  entity type, freshness timestamps, policy state, derived IDs, derived types, derived record
  summaries, and recent audit receipt summaries.
- Raw content is returned only when normal memory read policy allows the actor to read the source.
  Cross-principal private memory and project memory without verified project access are redacted.
- Redacted inspect responses also scrub nested audit receipt `details` while preserving audit IDs,
  actions, policy reasons, source IDs, and derived IDs.
- Inspect emits a metadata-only `memory.inspect` audit receipt with request attribution, policy
  state, source IDs, derived IDs, and redaction state.
- Added `sibyl memory-inspect <source-id>` table and JSON output. The CLI URL-encodes source IDs so
  provenance IDs containing `/`, `:`, or `%` route correctly.
- Added `get_raw_memory_by_source_id` as a static Surreal lookup using `source_id`,
  `organization_id`, `ORDER BY captured_at DESC`, and `LIMIT 1`.
- `moon run api:test -- tests/test_routes_memory.py tests/test_surreal_auth_runtime.py`: 78 passed
  in 1.44s.
- `moon run cli:test -- tests/test_main_capture.py`: 165 passed in 1.14s.
- `moon run api:lint api:typecheck cli:lint cli:typecheck core:lint core:typecheck`: lint passed;
  typecheck exited 0 with the existing 62 API and 25 core ty diagnostics.
- `moon run memory-trust-gate`: PASS with 6 checks, 0 failed.
- `moon run docs:lint`: passed after Prettier formatting.
- `moon run :check`: 34 tasks completed; API reported 1413 passed, 1 skipped, and 16 deselected.
- Independent review passed at `/tmp/claude-review-b46-source-inspect.e74jNf`. Follow-up review
  passed at `/tmp/claude-review-b46-source-inspect-followup.ENhCmS` after source-ID fallback, audit
  detail redaction, CLI URL encoding, and private-principal coverage were added.
- Residual accepted risk: metadata remains inspectable even when raw content is redacted, matching
  the current content-vs-metadata contract. Delegated scope content remains redacted until delegated
  membership lists are wired into the REST memory policy context.

### Packet B4.7: Denied Render Audit

Purpose: close the remaining B4 gap where project access can fail before context or reflection
render-specific audit receipts are emitted.

Depends on:

- B4.3 context pack render audit.
- B4.4 reflection render audit.
- B4.5 REST request attribution.

Files:

- `apps/api/src/sibyl/api/context_audit.py`
- `apps/api/src/sibyl/api/routes/context.py`
- `apps/api/tests/test_routes_context.py`
- `apps/api/tests/test_surreal_auth_runtime.py`

Implementation:

- Add compact `memory.context_pack.deny` and `memory.reflect.deny` receipts for project-access
  failures that happen before render.
- Include requested project IDs, actor ID, organization ID, memory scope, route action, reason code,
  and request attribution.
- Avoid source IDs when no source selection has occurred yet.
- Keep failures fail-open for audit writes and fail-closed for the user request.

Verify:

- `moon run api:test -- tests/test_routes_context.py tests/test_surreal_auth_runtime.py`
- `moon run api:lint api:typecheck`

Exit criteria:

- Denied context and reflection project access leaves an inspectable audit trail.
- Request attribution is present for REST denied-render receipts.
- Deny receipts do not leak hidden source text or source IDs that were never authorized.

B4.7 receipt, 2026-05-14:

- Added `log_denied_render_audit` as a metadata-only context audit helper for pre-render denial
  receipts.
- REST `/context/pack` now emits `memory.context_pack.deny` when requested project access fails
  before context compilation.
- REST `/context/reflect` now emits `memory.reflect.deny` when requested project access fails before
  reflection rendering.
- The denial handler covers both the structured `ProjectAccessDeniedError` and the deprecated
  production `ProjectAuthorizationError` while the old runtime exception is still present.
- Deny receipts include actor, organization, request attribution, project scope, requested project
  ID, route action, source surface, policy denial state, and stable reason code. They intentionally
  carry empty source and derived ID lists because no source selection has happened yet.
- Tests also assert that an audit writer failure still leaves the user request fail-closed with the
  original project denial.
- `moon run api:test -- tests/test_routes_context.py tests/test_surreal_auth_runtime.py`: 70 passed
  in 1.43s.
- `moon run api:lint api:typecheck`: lint passed; typecheck exited 0 with the existing 62 ty
  diagnostics.
- `moon run memory-trust-gate`: PASS with 6 checks, 0 failed.
- Independent review initially found that the handler missed the deprecated production
  `ProjectAuthorizationError`; the follow-up review passed at
  `/tmp/claude-review-b47-denied-render-followup.zKY7ME`.
- Remaining B4 risk: MCP denied render receipts remain requestless until the MCP tool layer exposes
  a trustworthy request object.

### Packet B5.1: Reflection Promotion Preview

Purpose: add a dry-run surface for promotion decisions before any native memory write happens.

Depends on:

- B3.1 policy context contract.
- B4.1 audit event skeleton.
- B4.2 audit receipt inspect.

Files:

- `packages/python/sibyl-core/src/sibyl_core/services/native_memory.py`
- `packages/python/sibyl-core/tests/test_native_memory.py`
- `packages/python/sibyl-core/tests/test_memory_policy.py`
- `apps/api/src/sibyl/api/routes/memory.py`
- `apps/api/src/sibyl/api/schemas.py`
- `apps/api/tests/test_routes_memory.py`

Implementation:

- Factor promotion candidate resolution into a shared planner used by preview and write paths.
- Return target scope, target scope key, raw source IDs, source input scopes, review state, policy
  reasons, and metadata without calling persistence helpers.
- Require explicit target scope and target scope key for mixed-scope or broader-scope moves.
- Emit `memory.reflect.promote.preview` audit receipts for allow and deny cases.
- Preserve existing promotion write behavior by making the write path consume the same planner.

Verify:

- `moon run core:test -- tests/test_native_memory.py tests/test_memory_policy.py`
- `moon run api:test -- tests/test_routes_memory.py`
- `moon run core:lint core:typecheck api:lint api:typecheck`

Exit criteria:

- Promotion preview is source-grounded, policy-backed, and non-mutating.
- Preview and promotion write paths cannot drift on candidate resolution.
- Audit receipts explain the preview decision without exposing hidden source content.

B5.1 receipt, 2026-05-13:

- Added `NativeReflectionPromotionPreview` and a shared internal promotion planner so preview and
  write paths resolve candidates, raw source IDs, target scope, scope key, project target, and
  denial reasons through one code path.
- Added `preview_reflection_candidate_promotion` to evaluate the target policy with
  `_authorize_reflection_write` and return source-scope metadata without calling
  `persist_reflection_candidate_native` or `save_raw_memory`.
- Added `POST /memory/reflection/promote/preview` with the same organization role gate, project
  target verification, accessible-project calculation, and request attribution pattern as the write
  promotion route.
- Added `ReflectionPromotionPreviewResponse` with target scope, target scope key, raw source IDs,
  policy reasons, input scopes, source count, review state, and bounded metadata.
- Tests cover allowed preview, missing-candidate preview, project-membership denial, no-write
  guarantees, REST project target verification, response shape, and `memory.reflect.promote.preview`
  audit receipts.
- `moon run api:test -- tests/test_routes_memory.py`: 21 passed in 1.31s.
- `moon run core:test -- tests/test_native_memory.py tests/test_memory_policy.py`: 1343 passed, 15
  skipped in 8.84s.
- `moon run api:lint api:typecheck core:lint core:typecheck`: API and core lint passed; typecheck
  exited 0 with the existing 63 API and 26 core diagnostics.
- Independent review passed at `/tmp/claude-review-b51-promotion-preview-20260513182458.txt`.
  Post-review polish added direct missing-candidate preview coverage, no-write assertions on the
  deny test, and a route formatting cleanup.
- Final focused review of the post-polish diff passed at
  `/tmp/claude-review-b51-promotion-preview-final-20260513183135.txt`.
- Remaining B5 risk: unauthorized project targets still fail with the existing route-level 403
  instead of returning a structured `allowed=false` preview response, the preview response uses
  `promote_to_scope`/`promote_to_scope_key` while the write response uses
  `memory_scope`/`scope_key`. B5.3 later exposed the preview flows from the CLI.

### Packet B5.2: Share Preview Contract

Purpose: provide a stable response shape for future sharing UX while keeping actual sharing disabled
in v0.8.

Depends on:

- B5.1 promotion preview.
- B4.6 source inspect.

Files:

- `packages/python/sibyl-core/src/sibyl_core/auth/memory_policy.py`
- `packages/python/sibyl-core/src/sibyl_core/services/native_memory.py`
- `apps/api/src/sibyl/api/routes/memory.py`
- `apps/api/src/sibyl/api/schemas.py`
- `apps/api/tests/test_routes_memory.py`
- `packages/python/sibyl-core/tests/test_memory_policy.py`
- `packages/python/sibyl-core/tests/test_native_memory.py`

Implementation:

- Add a share-preview service that accepts source IDs, target scope, target scope key, and intended
  recipient context.
- Return `allowed=false` with `scope_not_enabled` for organization, team, shared, public, and
  cross-organization share requests until explicit policy ships.
- Include redaction counts, hidden-but-relevant counts, visible source IDs, denied source IDs, and
  reason codes.
- Emit `memory.share.preview` audit receipts.
- Keep the response contract ready for UI and CLI clients without enabling write APIs.

Verify:

- `moon run core:test -- tests/test_memory_policy.py tests/test_native_memory.py`
- `moon run api:test -- tests/test_routes_memory.py`
- `moon run api:lint api:typecheck core:lint core:typecheck`

Exit criteria:

- Share preview proves what would be visible, hidden, or denied.
- Cross-org and broad sharing remain disabled with stable reason codes.
- Private-leak fixtures stay at zero leaks.

B5.2 receipt, 2026-05-13:

- Added `NativeMemorySharePreview` and `preview_memory_share` as a dry-run sharing contract that
  accepts source IDs, target scope, target scope key, and optional recipient organization context.
- The preview loads each source by raw-memory ID, evaluates source read policy before exposing it as
  visible, and returns denied source IDs for unreadable or missing inputs without exposing hidden
  source content or hidden source scope metadata.
- The contract returns redaction counts, hidden-but-relevant counts, visible source IDs, denied
  source IDs, typed missing source IDs, visible input scopes, source denial reasons, and policy
  reason metadata.
- Actual sharing remains disabled in v0.8. Cross-organization and broad share targets return
  `allowed=false` with stable `scope_not_enabled` or `share_not_enabled` reasons instead of mutating
  memory.
- Added `POST /memory/share/preview` with the normal memory write role gate, user authentication,
  project target verification, accessible-project context, and `memory.share.preview` audit
  receipts.
- Added REST schemas for share preview request/response so the future CLI and UI can consume the
  same stable shape.
- Tests cover disabled organization preview, private source redaction, missing sources, visible
  project sources, cross-organization denial, no-write guarantees, REST authentication, response
  shape, service arguments, and audit receipt fields.
- `moon run core:test -- tests/test_native_memory.py tests/test_memory_policy.py`: 1347 passed, 15
  skipped in 8.74s.
- `moon run api:test -- tests/test_routes_memory.py`: 23 passed in 1.20s.
- `moon run api:lint api:typecheck core:lint core:typecheck`: API and core lint passed; typecheck
  exited 0 with the existing 63 API and 26 core diagnostics.
- Independent review passed at `/tmp/claude-review-b52-share-preview-final-20260513185408.txt` after
  privacy hardening removed hidden source scope metadata from REST-visible `input_scopes`.
- Remaining B5 risk: actual share writes remain disabled, unauthorized project targets still fail
  through route-level project authorization rather than returning structured preview denial, and any
  future CLI/UI surface must not render internal `policy_decisions` raw because denied-source policy
  decisions can carry hidden scope keys for internal auditing.

### Packet B5.3: Promotion And Share CLI Surface

Purpose: expose preview decisions to agents and humans from the CLI without enabling direct sharing.

Depends on:

- B5.1 promotion preview.
- B5.2 share preview contract.

Files:

- `apps/cli/src/sibyl_cli/client.py`
- `apps/cli/src/sibyl_cli/main.py`
- `apps/cli/tests/test_main_capture.py`
- `apps/api/tests/test_routes_memory.py`

Implementation:

- Add `sibyl memory-promote --preview` for reflection candidates.
- Add `sibyl memory-share --preview` for future share decisions.
- Render target scope, source IDs, allow/deny state, reason codes, redaction counts, and audit
  receipt IDs when available.
- Keep non-preview share commands unavailable or explicitly denied.

Verify:

- `moon run cli:test -- tests/test_main_capture.py`
- `moon run cli:lint cli:typecheck`
- `moon run api:test -- tests/test_routes_memory.py`

Exit criteria:

- Agents can ask for promotion/share decisions without mutating memory.
- CLI JSON is stable enough for prompt hooks and future UI wiring.
- Actual broad sharing remains disabled.

B5.3 receipt, 2026-05-13:

- Added CLI client helpers for `POST /memory/reflection/promote/preview` and
  `POST /memory/share/preview`.
- Added root CLI commands `sibyl memory-promote --preview` and `sibyl memory-share --preview`.
  Non-preview invocations fail locally before opening an API client.
- Promotion preview renders allow/deny state, denial reason, candidate ID, review state, target
  scope/key, source IDs, policy reasons, and audit IDs when the API provides one.
- Share preview renders allow/deny state, target scope/key, source IDs, visible IDs, denied IDs,
  missing IDs, redaction counts, hidden-but-relevant counts, policy reasons, and audit IDs when the
  API provides one.
- CLI rendering uses typed preview response fields and does not render internal `policy_decisions`,
  preserving the B5.2 hidden-scope boundary.
- Tests cover promotion preview rendering, project target inference from the linked project, related
  ID/task forwarding, non-preview promotion denial, share preview rendering, share source ID parsing
  for CSV and positional values, project target inference, and non-preview share denial.
- `moon run cli:test -- tests/test_main_capture.py`: 162 passed in 1.06s.
- `moon run cli:lint cli:typecheck`: CLI lint and typecheck passed.
- `moon run api:test -- tests/test_routes_memory.py`: 23 passed in 1.20s.
- Independent review passed at `/tmp/claude-review-b53-cli-preview-20260513190657.txt`. Remaining
  non-blocking follow-up: the CLI has a forward-compatible audit-ID row, but the preview APIs do not
  currently return audit receipt IDs.

### Packet B6.1: Memory Trust Gate Harness

Purpose: make the release trust gate one commandable harness instead of a manually assembled
checklist.

Depends on:

- B3 through B5.

Files:

- `moon.yml`
- `tools/trust/memory_trust_gate.py`
- `tools/tests/test_memory_trust_gate.py`
- `packages/python/sibyl-core/moon.yml`
- `packages/python/sibyl-core/tests/test_memory_policy.py`
- `packages/python/sibyl-core/tests/test_native_memory.py`
- `packages/python/sibyl-core/tests/test_context_pack.py`
- `packages/python/sibyl-core/tests/test_session_bundle.py`
- `apps/api/moon.yml`
- `apps/api/tests/test_routes_memory.py`
- `apps/api/tests/test_surreal_auth_runtime.py`
- `apps/api/tests/test_routes_context.py`
- `apps/api/tests/test_routes_session.py`
- `apps/api/tests/test_server_accessible_projects.py`
- `apps/api/tests/test_auth_mcp_token_verifier.py`
- `apps/api/tests/test_mcp_oauth_session_refresh.py`
- `apps/api/tests/test_mcp_oauth_multi_org_selection.py`
- `apps/cli/moon.yml`
- `apps/cli/tests/test_main_capture.py`
- `apps/cli/tests/test_main_search.py`
- `apps/cli/tests/test_context_pack.py`
- `apps/cli/tests/test_session.py`
- `apps/cli/tests/test_user_prompt_hook.py`
- `docs/architecture/SIBYL_V08_PURE_SURREAL_CLOSURE_AND_MEMORY_TRUST_PLAN.md`

Implementation:

- Add a `memory-trust-gate` moon task backed by a small Python harness that runs trust-sensitive
  package slice tasks through `moon run`.
- Include raw memory, context pack, wake, recall, reflect, MCP, CLI, promotion preview, share
  preview, audit, and inspect coverage in the harness metadata.
- Make the gate print a concise receipt summary suitable for release notes, including pass/fail
  status, elapsed time per slice, and covered surfaces.
- Keep each slice pointed at an explicit package test task so failures stay actionable.

Verify:

- `moon run memory-trust-gate`
- `moon run :check`
- `git diff --check`

Release note:

- B6 owns the memory trust claim. A6 still owns final baseline, benchmark, inventory, CI, and
  nightly release receipts on the final tree.

B6.1 receipt, 2026-05-14:

- `memory-trust-gate` is a root moon task backed by `tools.trust.memory_trust_gate`.
- The gate runs explicit package slice tasks:
  - `core:memory-trust-policy-test`: memory policy plus native promotion/share preview coverage.
  - `core:memory-trust-context-test`: context pack, wake, recall, and raw-memory blend coverage.
  - `api:memory-trust-rest-test`: raw memory REST, preview, audit, and inspect coverage.
  - `api:memory-trust-context-test`: context pack, session wake, reflection, and audit coverage.
  - `api:memory-trust-mcp-test`: MCP scoping, memory write, reflection, and auth coverage.
  - `api:memory-trust-jobs-test`: task-learning job policy payloads, local queue preservation, and
    job audit receipts.
  - `cli:memory-trust-test`: CLI remember, recall, wake, reflect, prompt hook, preview, audit, and
    inspect coverage.
- `moon run inventory-lint inventory-typecheck memory-trust-gate-test`: tool lint and typecheck
  passed; harness tests passed with 8 tests.
- `moon run api:memory-trust-jobs-test`: 85 passed in 1.54s.
- `moon run memory-trust-gate`: PASS with 7 slices and covered surfaces `audit`, `cli`,
  `context pack`, `inspect`, `jobs`, `mcp`, `memory policy`, `promotion preview`, `prompt hook`,
  `raw memory`, `recall`, `reflect`, `share preview`, `task learning`, and `wake`.
- Follow-up after independent review: the gate requires and reports `prompt hook` and job coverage,
  converts runner exceptions into FAIL receipts, and keeps the uncached root gate free of decorative
  `inputs` metadata.

Exit criteria:

- v0.8 has one repeatable local gate for the memory trust claim.
- Release notes can cite the gate plus CI/nightly receipts.

### Packet A1.1: Compatibility Boundary Guard

Purpose: make accidental Graphiti imports in default runtime modules fail fast.

Depends on:

- A0 baseline.

Files:

- `tools/inventory/runtime_surface.py`
- `tools/tests/test_runtime_surface.py`
- `packages/python/sibyl-core/pyproject.toml`
- `moon.yml`
- `docs/architecture/SURREALDB_GRAPHITI_EXIT_INVENTORY.md`

Implementation:

- Teach inventory checks to classify default modules, compatibility modules, migrations, admin
  tools, tests, and archived docs.
- Fail default-runtime inventory when a default module imports Graphiti or Graphiti-shaped adapter
  classes.
- Add a compatibility allowlist with ownership notes and explicit deletion or retention criteria.
- Keep `graphiti-core` outside default runtime dependencies.

Verify:

- `moon run inventory-check inventory-typecheck inventory-test`
- `moon run core:no-graphiti-smoke`
- `uv lock --check`

Exit criteria:

- New default-path Graphiti imports fail CI.
- Retained Graphiti imports are named, owned, and optional.

### Packet A1.2: Compatibility Test Island

Purpose: keep compatibility tests available without making default tests require Graphiti.

Depends on:

- A1.1 compatibility boundary guard.

Files:

- `moon.yml`
- `packages/python/sibyl-core/pyproject.toml`
- `packages/python/sibyl-core/tests/**`
- `apps/api/tests/**`
- `docs/architecture/SURREALDB_GRAPHITI_EXIT_INVENTORY.md`

Implementation:

- Move Graphiti-dependent tests under a marker or named moon task.
- Ensure default `core:test`, `api:test`, and `:check` work without installing the compatibility
  extra.
- Add a separate compatibility test task that installs or assumes `sibyl-core[compatibility]`.
- Document which tests exist only for archive, migration, or compare workflows.

Verify:

- `moon run core:test`
- `moon run api:test`
- `moon run core:no-graphiti-smoke`
- explicit compatibility test task

Exit criteria:

- Default tests prove the Surreal-only runtime.
- Compatibility tests are opt-in and named honestly.

### Packet A2.1: Native Entity Hydration

Purpose: remove Graphiti node classes from default entity lookup and list hydration.

Depends on:

- A1 compatibility boundary.
- B2 project filtering.

Files:

- `packages/python/sibyl-core/src/sibyl_core/graph/entities.py`
- `packages/python/sibyl-core/src/sibyl_core/services/native_graph.py`
- `packages/python/sibyl-core/src/sibyl_core/retrieval/native.py`
- `apps/api/src/sibyl/persistence/graph_runtime.py`
- `packages/python/sibyl-core/tests/test_graph_entities.py`
- `apps/api/tests/test_routes_entities.py`
- `apps/api/tests/test_routes_entities_read.py`

Implementation:

- Hydrate entity records directly from Surreal rows instead of `EntityNode` or other Graphiti
  classes.
- Preserve legacy row compatibility with explicit normalization helpers.
- Keep project policy fields, source IDs, confidence, validity, and timestamps intact.
- Add fixtures for native rows, legacy-shaped rows, missing optional fields, and project entities.

Verify:

- `moon run core:test -- tests/test_graph_entities.py`
- `moon run api:test -- tests/test_routes_entities.py tests/test_routes_entities_read.py`
- `moon run core:no-graphiti-smoke`

Exit criteria:

- Default entity reads do not import Graphiti node classes.
- Legacy-shaped records still hydrate correctly through native helpers.

### Packet A2.2: Native Relationship And Temporal Reads

Purpose: move relationship CRUD, traversal, and temporal reads fully onto native Surreal
relationships.

Depends on:

- A2.1 native entity hydration.

Files:

- `packages/python/sibyl-core/src/sibyl_core/graph/relationships.py`
- `packages/python/sibyl-core/src/sibyl_core/services/native_graph.py`
- `packages/python/sibyl-core/src/sibyl_core/retrieval/native.py`
- `packages/python/sibyl-core/tests/test_graph_relationships.py`
- `apps/api/tests/test_routes_search.py`
- `apps/api/tests/test_routes_context.py`

Implementation:

- Replace Graphiti edge models with native `relates_to`, `mentions`, and temporal relationship
  records.
- Preserve relationship confidence, validity intervals, source IDs, and provenance.
- Cover traverse, related summary, dependency, search, and context hydration paths.
- Keep archive compatibility isolated behind explicit conversion helpers.

Verify:

- `moon run core:test -- tests/test_graph_relationships.py tests/test_native_retrieval.py`
- `moon run api:test -- tests/test_routes_search.py tests/test_routes_context.py`
- `moon run core:no-graphiti-smoke`

Exit criteria:

- Default relationship paths do not import Graphiti edge classes.
- Temporal and traversal behavior remains covered by native fixtures.

### Packet A3.1: Native Embedding Service

Purpose: make embedding provider selection, dimensions, cache keys, and metadata owned by Sibyl.

Depends on:

- A1 compatibility boundary.

Files:

- `packages/python/sibyl-core/src/sibyl_core/retrieval/native.py`
- `packages/python/sibyl-core/src/sibyl_core/services/native_graph.py`
- `packages/python/sibyl-core/src/sibyl_core/graph/cached_embedder.py`
- `packages/python/sibyl-core/src/sibyl_core/graph/gemini_embedder.py`
- `packages/python/sibyl-core/src/sibyl_core/graph/client.py`
- `packages/python/sibyl-core/tests/test_native_retrieval.py`
- `packages/python/sibyl-core/tests/test_graph_client.py`

Implementation:

- Add a native embedding provider interface that is not shaped like Graphiti embedder classes.
- Move Gemini, OpenAI, deterministic test, and cached embedding behavior behind native providers.
- Store embedding provider, model, dimensions, tokenizer estimate method, and cache key metadata
  with vector writes and eval reports.
- Keep Graphiti-compatible embedders only in the compatibility island until A4 decides deletion or
  retention.

Verify:

- `moon run core:test -- tests/test_native_retrieval.py tests/test_graph_client.py`
- `moon run core:no-graphiti-smoke`
- `moon run core:bench-context`

Exit criteria:

- Native vector writes and searches do not use Graphiti embedder interfaces.
- Benchmark artifacts expose enough embedding metadata to compare runs honestly.

### Packet A3.2: Benchmark Metadata Gate

Purpose: make context and AI-memory benchmark claims release-safe.

Depends on:

- A3.1 native embedding service.

Files:

- `benchmarks/context_pack_eval.py`
- `benchmarks/context_pack_cases.json`
- `benchmarks/ai_memory/**`
- `docs/testing/benchmark-methodology.md`
- `moon.yml`

Implementation:

- Add or update `bench-gate` checks for required metadata fields.
- Require retrieval mode, embedding provider/model/dimensions, tokenizer method, dataset name,
  corpus hash, repeat count, and auth manifest ID.
- Separate pre-Graphiti, post-Graphiti, native, and compare labels so charts cannot mix incompatible
  runs.
- Document where benchmark artifacts live and which are release-citable.

Verify:

- `moon run core:bench-context -- --cases benchmarks/context_pack_cases.json --auth-manifest .moon/cache/baseline-runtime-manifest.json --label retrieval-compare --repeat 20 --metadata retrieval_mode=compare`
- `moon run bench-gate`
- `moon run docs:lint`

Exit criteria:

- Every benchmark claim in release notes can point to a gated artifact.
- Mixed or under-metadataed benchmark outputs fail the gate.

### Packet A4.1: Graphiti Ops Decision

Purpose: delete unneeded Graphiti ops modules or move retained modules into a named compatibility
namespace.

Depends on:

- A1 through A3.

Files:

- `packages/python/sibyl-core/src/sibyl_core/graph/surreal/compat/ops/**`
- `packages/python/sibyl-core/src/sibyl_core/backends/surreal/driver.py`
- `packages/python/sibyl-core/src/sibyl_core/graph/search_interface.py`
- `packages/python/sibyl-core/src/sibyl_core/graph/mock_llm.py`
- `tools/inventory/runtime_surface.py`
- `tools/tests/test_runtime_surface.py`
- `docs/architecture/SURREALDB_GRAPHITI_EXIT_INVENTORY.md`

Implementation:

- Classify every Graphiti ops module as delete, migrate, admin-only, or compatibility-retain.
- Move retained modules into the compatibility island and update imports.
- Delete stale Graphiti comments from default runtime files when the referenced behavior is gone.
- Update inventory docs with final owned import counts.

Verify:

- `moon run inventory-check inventory-typecheck inventory-test`
- `moon run core:no-graphiti-smoke`
- `moon run core:test`
- explicit compatibility test task

Exit criteria:

- No unowned Graphiti ops code remains in default paths.
- Compatibility retention has a named owner, task, and test gate.

### Packet A5.1: Archive And Restore Policy

Purpose: keep historical recovery possible without ambient PostgreSQL, FalkorDB, or Redis data-plane
assumptions.

Depends on:

- A4.1 Graphiti ops decision.

Files:

- `apps/api/src/sibyl/cli/migrate.py`
- `apps/api/src/sibyl/jobs/backup.py`
- `packages/python/sibyl-core/src/sibyl_core/migrate/archive.py`
- `apps/api/tests/test_migrate.py`
- `packages/python/sibyl-core/tests/test_archive_migration.py`
- `docs/guide/surrealdb-migration-release-notes.md`
- `docs/architecture/SURREALDB_PHASE3_BURNDOWN.md`

Implementation:

- Make archive import and restore commands require explicit input files, source type, and mode.
- Label PostgreSQL and FalkorDB restore paths as historical migration only.
- Add dry-run output that reports counts and unsupported payloads before any write.
- Ensure backup docs describe Surreal-native backup/restore as the default.

Verify:

- `moon run api:test -- tests/test_migrate.py`
- `moon run core:test -- tests/test_archive_migration.py`
- `moon run docs:lint`

Exit criteria:

- Default recovery docs are Surreal-native.
- Historical imports are explicit and cannot run from ambient service defaults.

Receipt, 2026-05-14:

- Commit: this packet's `fix(migrate): require explicit archive restore policy` commit.
- Changed files:
  - `apps/api/src/sibyl/cli/migrate.py`
  - `apps/api/tests/test_cli_migrate.py`
  - `README.md`
  - `docs/api/auth-authorization.md`
  - `docs/architecture/SURREALDB_PHASE2_LIVE_GATES.md`
  - `docs/architecture/SURREALDB_PHASE3_BURNDOWN.md`
  - `docs/deployment/docker-compose.md`
  - `docs/deployment/troubleshooting.md`
  - `docs/guide/installation.md`
  - `docs/guide/migrating-from-falkor.md`
  - `docs/guide/storage-modes.md`
  - `docs/guide/surrealdb-migration-release-notes.md`
  - `docs/testing/benchmark-methodology.md`
  - `tools/dev/run-surreal-dev.sh`
  - `tools/tests/test_dev_scripts.py`
- Verification:
  - `moon run api:test -- tests/test_cli_migrate.py` -> 43 passed in 1.52s.
  - `moon run docs:lint` -> all matched files use Prettier code style.
  - `moon run root:dev-script-test` -> 5 passed in 0.19s.
  - `moon run api:lint` -> all checks passed.
  - `moon run api:typecheck` -> exited 0 with existing 62 ty warnings.
  - `moon run core:test -- tests/test_migrate_archive.py` -> 885 passed, 14 skipped, 20 deselected
    in 5.28s.
  - `moon run api:test` -> 1405 passed, 1 skipped, 16 deselected in 10.97s.
  - `moon run core:no-graphiti-smoke` -> 2 passed in 2.56s.
  - `git diff --check` -> passed.
  - `moon run :check` -> 34 tasks completed.
- Review: Claude cross-model review PASS at `/tmp/claude-review-a51-docs-final.YEQf5b`; the only
  finding was heredoc indentation in `tools/dev/run-surreal-dev.sh`, fixed before commit.
- Policy or compatibility decision: archive import, rehearsal, and cutover require explicit
  `--source-type` and `--target-mode`; `postgres.sql` restore is historical-only and requires
  `--restore-database-dump --source-type legacy-archive --target-mode postgres-rehearsal`.
- Follow-up closed by A5.2: the broader retained-term inventory now covers active docs and
  deployment surfaces.

### Packet A5.2: Legacy Docs And Compose Sweep

Purpose: remove stale default-runtime instructions for legacy services.

Depends on:

- A5.1 archive and restore policy.

Files:

- `README.md`
- `apps/api/README.md`
- `apps/cli/README.md`
- `apps/web/README.md`
- `docs/guide/why-surreal.md`
- `docs/guide/surrealdb-migration-release-notes.md`
- `docker-compose*.yml`
- `compose.e2e.yml`
- `.github/workflows/*`
- `charts/**`
- `tools/inventory/runtime_surface.py`
- `tools/tests/test_runtime_surface.py`

Implementation:

- Audit active docs, compose files, CI, and charts for `postgres`, `falkor`, `redis`, `valkey`,
  `Graphiti`, and `graphiti`.
- Keep Redis/Valkey documented only as explicit coordination opt-in.
- Keep Graphiti and FalkorDB references only in historical, migration, benchmark, or compatibility
  sections.
- Add a docs inventory note for any retained legacy terms.
- Add or update an allowlist-backed inventory check so retained legacy terms must carry an explicit
  owner and reason.

Verify:

- Discovery starter:
  `rg -n "postgres|falkor|redis|valkey|Graphiti|graphiti" README.md apps docs docker-compose*.yml compose.e2e.yml .github charts`
- `moon run inventory-check inventory-typecheck inventory-test`
- `moon run docs:lint`
- `moon run :check`

Exit criteria:

- A new user following active docs starts a Surreal-only default stack.
- Retained legacy references are labeled and intentional.

Receipt, 2026-05-14:

- Commit: this packet's retained legacy-term inventory commit.
- Changed files:
  - `tools/inventory/runtime_surface.py`
  - `tools/tests/test_runtime_surface.py`
  - `docs/research/rust-port/INVENTORY.md`
  - `docs/architecture/SIBYL_V08_PURE_SURREAL_CLOSURE_AND_MEMORY_TRUST_PLAN.md`
  - `moon.yml`
- Verification:
  - `moon run inventory-check` -> generated snapshot current, Graphiti exit inventory covers 21
    import files, and retained legacy-term inventory covers 87 active doc/config files.
  - `moon run inventory-test` -> 26 passed in 6.90s.
  - `moon run inventory-lint` -> all checks passed; 28 files already formatted.
  - `moon run inventory-typecheck` -> all checks passed.
  - `moon run docs:lint` -> all matched files use Prettier code style.
  - `git diff --check` -> passed.
  - `moon run :check` -> 34 tasks completed. API reported 1405 passed, 1 skipped, 16 deselected; web
    reported 21 test files and 91 tests passed; inventory reported 26 passed.
- Review: Claude cross-model review PASS at `/tmp/claude-review-a52-closed.0K0C5w`. Follow-up notes
  were non-blocking and centered on future scan-scope hardening for extensionless config files and
  wildcard allowlist entries.
- Policy or compatibility decision: active docs, deployment configs, root project instructions,
  environment templates, root moon tasks, dev scripts, package docs, and source/packaged Sibyl skill
  docs now appear in the retained legacy-term inventory whenever they mention retired or optional
  legacy services. Every retained reference must render with an owner and reason on the same
  generated inventory row, and unowned references fail `inventory-check`.
- Remaining risk closed by the 0.8.1 inventory hardening pass: the retained legacy-term scanner now
  covers tracked Dockerfiles, JSON, TOML, Helm `.tpl`, and root package config files.

### Packet A6.1: Pure Surreal Release Audit

Purpose: prove the default runtime, default docs, default dependencies, and default CI are
Surreal-only.

Depends on:

- A1 through A5.
- B6 trust gate, if v0.8 releases both tracks together.

Files:

- `docs/architecture/SIBYL_V08_PURE_SURREAL_CLOSURE_AND_MEMORY_TRUST_PLAN.md`
- `docs/architecture/SURREALDB_GRAPHITI_EXIT_INVENTORY.md`
- `docs/architecture/SURREALDB_PHASE3_BURNDOWN.md`
- release notes draft

Implementation:

- Run the full local release gate from a clean checkout or clean worktree.
- Confirm default dependency metadata excludes Graphiti, FalkorDB, PostgreSQL, and Redis/Valkey as
  data-plane requirements.
- Confirm inventory, no-Graphiti smoke, docs sweep, benchmark gates, and memory trust gates have
  current receipts.
- Record CI, docs deploy, and nightly run IDs after the final pushed main commit.
- Write the binary release recommendation: ship or hold.

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
- nightly regression green on `main`

Exit criteria:

- v0.8 can claim a Surreal-only default runtime and policy-backed, inspectable memory.
- Any retained compatibility or historical surface is opt-in, named, documented, and separately
  tested.

Receipt, 2026-05-14:

- Local tree under audit: this release-audit refresh packet after `e0bb7ac3`
  (`fix(cli): clarify queued task learning`). The final commit for this packet records the exact
  tree hash.
- Pushed release evidence baseline: `main` commit `4855ba8a`.
- CI, docs, and nightly coverage for the release baseline:
  - Main CI run `25870913035` completed successfully on `4855ba8a`.
  - Docs deploy run `25877971558` completed successfully on `4855ba8a`.
  - Nightly regression run `25877971585` completed successfully on `4855ba8a`.
- The follow-up 0.8.1 docs and inventory hardening work is intentionally outside those pushed-main
  receipts until it gets its own CI run.
- Dependency boundary:
  - `graphiti-core[anthropic,google-genai]>=0.28.2` appears only in `sibyl-core[compatibility]` and
    the `sibyl-core` dev dependency group.
  - Default package metadata does not list FalkorDB, PostgreSQL, Redis, or Valkey as data-plane
    dependencies. The remaining `requires_redis` package reference is an API pytest marker.
- Verification:
  - `moon run inventory-check` -> generated snapshot current, Graphiti exit inventory covers 21
    import files, and retained legacy-term inventory covers 87 active doc/config files.
  - `moon run inventory-test` -> 26 passed in 7.47s.
  - `moon run inventory-typecheck` -> all checks passed.
  - `moon run inventory-lint` -> all checks passed; 28 files already formatted.
  - `moon run docs:lint` -> all matched files use Prettier code style.
  - `moon run core:no-graphiti-smoke` -> 2 passed.
  - `moon run memory-trust-gate` -> PASS, 7 checks, 0 failed. The gate covers core memory policy,
    context pack behavior, REST memory surfaces, context-session behavior, MCP access, task-learning
    jobs, and CLI memory.
  - `moon run core:test` -> 888 passed, 14 skipped, 20 deselected in 5.40s.
  - `moon run api:test` -> 1428 passed, 1 skipped, 16 deselected in 11.35s.
  - `moon run cli:test` -> 167 passed in 1.09s.
  - `moon run core:test api:test cli:test :check` -> 34 tasks completed, 24 cache hits.
  - `moon run baseline-seed` -> wrote `.moon/cache/baseline-runtime-manifest.json`.
  - `moon run baseline-replay-runtime` initially exposed stale MCP baseline expectations: project
    scoped MCP credentials now require an explicit project for `add`, and `link_graph_status` now
    succeeds with MCP org context. The baseline fixture and capture generator were updated, and the
    replay then passed across auth, REST, graph, search, and MCP fixtures.
  - `moon run bench-gate` -> Gate passed for `benchmarks/results/ai-memory/manifest.json`.
  - `moon run core:bench-context -- --cases benchmarks/context_pack_cases.json --auth-manifest .moon/cache/baseline-runtime-manifest.json --label retrieval-compare --repeat 20 --metadata retrieval_mode=compare`
    -> 160 cases, 20 repeats, pass rate 1.000, 0 failed, mean latency 19.8 ms, p95 latency 38.6 ms.
- Review:
  - A5.2 retained legacy-term inventory review: Claude cross-model review PASS at
    `/tmp/claude-review-a52-closed.0K0C5w`.
  - Final release-audit refresh review: Claude cross-model review PASS at
    `/tmp/claude-review-v08-final.h4YPjN`; it checked the B1 scope correction, job gate, MCP
    baseline refresh, and A6/B6 receipts.
- Policy or compatibility decision: the local tree is Surreal-only by default and memory-trust gates
  now include task-learning jobs. Persisted MemorySpace CRUD is explicitly post-v0.8 and is not part
  of the release claim.
- Binary recommendation: ship v0.8 from `4855ba8a` when Bliss is ready to cut the tag and release.
- Remaining risk: the next patch-release baseline should re-run CI, docs deploy, nightly, and the
  inventory guard after the 0.8.1 hardening commit lands.

### Packet A6.2: 0.8.1 Inventory Guard Hardening

Purpose: close the post-audit scanner blind spot for active config formats that can carry retained
legacy-service references.

Receipt, 2026-05-14:

- The retained legacy-term scanner now scans tracked Dockerfiles, JSON, TOML, Helm `.tpl`, root
  package config files, and `.devcontainer` files.
- The scanner filters to `git ls-files --cached` paths so untracked audit drafts and ignored
  research dumps do not become release blockers.
- The allowlist now stays scoped to files that actually retain legacy terms; scan-only files are
  covered by a separate regression assertion.
- Verification: `moon run inventory` wrote the generated inventory snapshot, and
  `moon run inventory-test` reported 27 passed in 14.06s.

## 14. Evidence Ledger

Every wave should leave a receipt block in this document or in the corresponding audit doc. Use this
shape so release notes can be assembled without archaeology:

```text
Wave:
Commit:
Date:
Changed files:
Verification:
  - command -> result
Review:
  - reviewer/tool -> PASS/FAIL and file path
Policy or compatibility decision:
Remaining risk:
Sibyl memory:
```

Release evidence must distinguish local receipts from CI receipts. A local green `main` is not the
same as a pushed green `origin/main`; CI and nightly run IDs should be recorded before release
claims are made.

## 15. Release Review

Before cutting v0.8, run one explicit review over the whole release:

- Confirm every required release gate in section 2 has a current receipt.
- Confirm all Graphiti imports are either deleted or owned by a named compatibility island.
- Confirm no default docs mention FalkorDB, PostgreSQL, or Redis/Valkey as required data services.
- Confirm `MemoryScope`/`MemoryPolicyContext`, project RBAC, policy context, audit, and inspect
  surfaces fail closed; persisted MemorySpace CRUD remains post-v0.8.
- Confirm project-private leak fixtures pass through REST, MCP, CLI, context, wake, recall, and
  reflection promotion paths.
- Confirm benchmark and AI-memory claims only cite artifacts that pass their gates.
- Confirm Sibyl tasks and decisions carry the final receipts and residual risks.

The release recommendation should be binary: ship v0.8 or hold it. If the answer is hold, name the
smallest blocking packet and the command that will prove it is fixed.
