# Sibyl v0.8 Pure Surreal Closure and Memory Trust Plan

- Status: draft execution plan
- Target release: v0.8
- Planning source: `plan_e464fd1e7b11`
- Tracking task: `c64a358e-aef4-4b32-8735-28f03047a13e`
- Related docs:
  - `docs/architecture/SIBYL_NORTHSTAR.md`
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

Verified on 2026-05-13 after the v0.7 merge:

- `moon run inventory-check inventory-typecheck inventory-test` passes with 14 tests.
- `moon run core:no-graphiti-smoke` passes with 2 tests.
- Main CI, docs deploy, and scheduled nightly regression are green.
- Default `sibyl-core` runtime dependencies do not include `graphiti-core`; Graphiti is isolated to
  the `compatibility` optional extra and dev/test dependency group.
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

Required release gates:

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
- CI green on `main`
- Nightly regression green on `main`

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

- Audit `packages/python/sibyl-core/src/sibyl_core/graph/surreal/ops/*` after A1-A3.
- Delete modules with no compatibility owner.
- Move retained modules under a clearly named compatibility namespace if they still support
  migration, admin, or explicit compare workflows.
- Remove stale comments that imply Graphiti is the active graph runtime.
- Update inventory coverage rules after the package move or deletion.

Files:

- `packages/python/sibyl-core/src/sibyl_core/graph/surreal/ops/*`
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

Exit criteria:

- Every memory surface has an explicit policy-context status.
- Missing context is tracked as implementation work, not tribal knowledge.

### Wave B1: MemorySpace Control Plane

Purpose: introduce first-class memory spaces as policy boundaries.

Implementation:

- Add `MemorySpace` records to the Surreal auth/control plane.
- Model membership for private, delegated, project, team, organization, shared, and public scopes.
- Keep team, organization, shared, and public write/share behavior disabled until explicit policy
  cases are implemented.
- Project graph memory should resolve to a project memory space.
- Add graph projection only for explanation and traversal, not as the source of authorization truth.

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

- Memory-space CRUD and membership basics exist.
- Policy helpers can resolve space visibility without graph lookups.
- Disabled scopes return stable deny reasons.

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
4. B1: introduce memory spaces as policy boundaries.
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

Recommended Sibyl tracking:

- Epic: `v0.8 Pure Surreal Closure`
  - Task: lock v0.8 baseline gates
  - Task: quarantine Graphiti compatibility
  - Task: replace Graphiti-shaped graph managers
  - Task: move embeddings to native ownership
  - Task: delete or isolate Graphiti ops package
  - Task: close archive, coordination, and legacy docs residue
  - Task: run pure Surreal release audit
- Epic: `v0.8 Memory Trust Foundation`
  - Task: inventory trust-sensitive memory surfaces
  - Task: add memory-space control plane
  - Task: harden project RBAC
  - Task: unify API, CLI, MCP, and job policy context
  - Task: add memory audit and inspect surfaces
  - Task: add promotion and share preview
  - Task: run memory trust release gate

Each task should complete with:

- changed files
- exact verification command receipts
- policy or compatibility decisions made
- any remaining risk or deferred follow-up

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

These should be answered during B0/A0 before broad implementation:

- Should `organization` memory scope become readable in v0.8, or remain disabled until explicit
  organization memory spaces ship?
- Should project-private graph entities without registered project records be denied for all
  non-admin users, or migrated automatically before enforcement?
- Should Graphiti compatibility remain in this repository as an optional extra after v0.8, or move
  to an archive branch once A4 is complete?
- How long should retained `postgres.sql` restore support remain available after v0.8?
- Should audit events for context-pack reads store item IDs only, or item IDs plus compact reason
  metadata?
- Should share preview land in CLI first, API first, or both?

## 11. Post-v0.8 Bridge

v0.8 should leave the system ready for:

- `synthesize`: source-grounded large-read artifacts from policy-filtered graph slices.
- Human trust UI: inspect, correct, hide, promote, redact, export, and delete memory.
- Team/shared memory spaces: deliberate sharing with previews and audit trail.
- Personal corpus ingestion: staged import for email, chat, notes, docs, and home-assistant memory.
- Live memory cockpit: live capture feed, reflection progress, context-pack preview, and
  permission-change invalidation.

The sequencing matters. `synthesize` and sharing become powerful only after policy, provenance,
audit, and inspection are boring.
