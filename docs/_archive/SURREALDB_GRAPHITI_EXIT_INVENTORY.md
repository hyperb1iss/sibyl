# SurrealDB Graphiti Exit Inventory

Status: v0.7/v0.8 inventory baseline; 1.0 deletion checklist.

This document is hand-authored removal intent. The generated runtime source of truth remains
`docs/research/rust-port/INVENTORY.md`, and `moon run inventory-check` fails when a generated
Graphiti import is not classified here.

2026-05-15 1.0 update: the target is no longer "Graphiti absent from the default loop but retained
as an optional compatibility island." The 1.0 roadmap requires total supported-runtime deletion: no
`graphiti-core` dependency, no `graphiti_core` imports, no compatibility extra, and no live Graphiti
test surface. Legacy Graphiti-shaped archive data should remain readable through Sibyl-owned
projection/import code that does not import Graphiti.

## A0 Baseline Receipt

Recorded on 2026-05-13 at local commit `1de0b408`.

- `moon run inventory-check inventory-typecheck inventory-test`: generated inventory is current, 21
  Graphiti import files are covered here, inventory typecheck passed, and inventory tests reported
  14 passed.
- `moon run core:no-graphiti-smoke`: 2 passed.
- `moon run :check`: 33 tasks completed, including 5 executed tasks and 28 cache hits. Core reported
  1327 passed and 15 skipped; API reported 1639 passed and 1 skipped; CLI reported 156 passed; web
  reported 88 passed.
- `graphiti-core` remains isolated to `sibyl-core[compatibility]` and the `sibyl-core` dev
  dependency group. It is absent from default `sibyl-core` runtime dependencies.
- Green remote receipts exist for `origin/main` at `d2d3d926`: CI run `25801942331`, docs deploy run
  `25801942466`, and scheduled nightly run `25791871706`. Local `main` is ahead of `origin/main`, so
  local gates are the receipt for the unpushed checkpoint.

## Coverage Rule

Every generated Graphiti import path must match the code allowlist in
`tools/inventory/runtime_surface.py` and appear as a backticked path in this document. A path that
is only documented here still fails the inventory gate unless the code allowlist classifies it. The
group `packages/python/sibyl-core/src/sibyl_core/graph/surreal/compat/ops/*` covers the Graphiti
operation adapter package as one named compatibility surface.

## Compatibility Allowlist

These are the only retained Graphiti import surfaces. Each entry has a machine-enforced class,
owner, and deletion or retention criterion.

- `packages/python/sibyl-core/src/sibyl_core/backends/surreal/driver.py`
  - Class: `compatibility`
  - Owner: v0.7 Graphiti exit
  - Criteria: Graphiti client construction is deleted and native services own graph access.
- `packages/python/sibyl-core/src/sibyl_core/graph/client.py`
  - Class: `compatibility`
  - Owner: v0.7 Graphiti exit
  - Criteria: Native graph client replaces Graphiti construction and provider adapters.
- `packages/python/sibyl-core/src/sibyl_core/graph/entities.py`
  - Class: `compatibility`
  - Owner: v0.7 native memory
  - Criteria: Native write, exact lookup, semantic search, and entity hydration cover the seeded
    graph behavior without Graphiti node APIs.
- `packages/python/sibyl-core/src/sibyl_core/graph/relationships.py`
  - Class: `compatibility`
  - Owner: v0.7 native write adapter
  - Criteria: Native relation manager owns relates_to, mentions, and relationship model hydration.
- `packages/python/sibyl-core/src/sibyl_core/graph/search_interface.py`
  - Class: `compatibility`
  - Owner: v0.7 native retrieval
  - Criteria: Compare mode no longer calls Graphiti search and seeded native retrieval is the
    default path.
- `packages/python/sibyl-core/src/sibyl_core/graph/cached_embedder.py`
  - Class: `compatibility`
  - Owner: v0.7 native retrieval
  - Criteria: Native embedding service owns caching without Graphiti embedder types.
- `packages/python/sibyl-core/src/sibyl_core/graph/gemini_embedder.py`
  - Class: `compatibility`
  - Owner: v0.7 native retrieval
  - Criteria: Native embedding service supports Gemini directly.
- `packages/python/sibyl-core/src/sibyl_core/graph/mock_llm.py`
  - Class: `test`
  - Owner: v0.7 reflection
  - Criteria: Native reflection tests no longer instantiate Graphiti extraction clients.
- `packages/python/sibyl-core/src/sibyl_core/graph/surreal/compat/ops/*`
  - Class: `compatibility`
  - Owner: v0.7 Graphiti exit
  - Criteria: No default or fallback memory path constructs Graphiti or calls Graphiti model
    operation interfaces.

## Compatibility Test Island

Default test tasks avoid collecting the named Graphiti compatibility files and skip mixed-file cases
marked `graphiti_compatibility`:

- `moon run core:test`
- `moon run api:test`
- `moon run :check`

The retained Graphiti test surface is opt-in:

- `moon run core:graphiti-compatibility-test`
- `moon run api:graphiti-compatibility-test`
- `moon run graphiti-compatibility-test`

Core compatibility tests:

- `packages/python/sibyl-core/tests/graph/surreal`
- `packages/python/sibyl-core/tests/test_graph_batch.py`
- `packages/python/sibyl-core/tests/test_graph_client.py`
- `packages/python/sibyl-core/tests/test_graph_entities.py`
- `packages/python/sibyl-core/tests/test_graph_relationships.py`
- `packages/python/sibyl-core/tests/test_graph_runtime_services.py`
- `packages/python/sibyl-core/tests/test_log_safety.py`
- `packages/python/sibyl-core/tests/test_migrate_archive.py`
- `packages/python/sibyl-core/tests/test_search_interface.py`
- `packages/python/sibyl-core/tests/test_surreal_authentication.py`
- `packages/python/sibyl-core/tests/test_surreal_observability.py`

Core marked compatibility tests:

- `packages/python/sibyl-core/tests/test_models.py`
- `packages/python/sibyl-core/tests/test_retrieval_advanced.py`
- `packages/python/sibyl-core/tests/test_tools_admin.py`
- `packages/python/sibyl-core/tests/test_tools_manage.py`

API compatibility tests:

- `apps/api/tests/test_communities.py`
- `apps/api/tests/test_e2e_workflows.py`
- `apps/api/tests/test_graph_communities_lod.py`
- `apps/api/tests/test_graph_entities.py`
- `apps/api/tests/test_graph_relationships.py`
- `apps/api/tests/test_harness.py`
- `apps/api/tests/test_legacy_graph_persistence.py`
- `apps/api/tests/test_tools_core.py`

API marked compatibility tests:

- `apps/api/tests/test_cli_db.py`
- `apps/api/tests/test_cli_export.py`
- `apps/api/tests/test_models.py`
- `apps/api/tests/test_settings_api_key_loading.py`
- `apps/api/tests/test_tools_manage.py`

These tests remain for archive, migration, graph admin, and compare/fallback coverage while the
default runtime continues moving to native Surreal surfaces.

## Default Loop Position

- `remember`: raw capture, summarized `sibyl add`, API entity creation, and async create jobs write
  through native Surreal collaborators on the default path.
- `recall`, `context`, `wake`, and `explore`: native retrieval and list/detail exploration run
  through direct Surreal fulltext, raw recall, graph expansion, and native related-item hydration
  without importing Graphiti.
- `temporal`: history, timeline, and conflict reads query native `relates_to` edges through the
  Surreal graph runtime without Graphiti edge ops.
- `health` and `stats`: org-scoped MCP health checks and graph stats count native Surreal entity
  records without entering the compatibility graph service.
- `manage`: task updates, task prioritization, dependency cycle detection, and source graph-linking
  orchestration bind native Surreal managers by default. Test-only compatibility factories can still
  be patched for legacy unit coverage.
- `link_graph` and crawler graph integration: document/entity linking creates document entities and
  `DOCUMENTED_IN` relationships through native Surreal managers by default.
- `reflect`: persisted reflection sources and candidates write native graph records by default. Set
  `SIBYL_NATIVE_WRITE=disabled` to use the compatibility path. Review-mode raw candidate storage
  remains the explicit review path.
- Graphiti remains only in named compatibility, compare-mode, admin, and migration surfaces until
  their removal conditions below are met.
- `graphiti-core` is owned by the `sibyl-core[compatibility]` extra and the core dev dependency
  group. It is not a default `sibyl-core` runtime dependency.

## Legacy Projection Rule

Pre-v0.7 `Episodic` and `Entity` records are projectable into native retrieval only when scope can
be assigned without guessing. Records with project metadata, source ownership, or raw source links
inherit that project or owner scope. Historical records without recoverable ownership project as
`memory_scope = organization`, `principal_id = null`, and `source_id = graphiti:<episode_uuid>`.
Records without a recoverable source ID are excluded from native retrieval until a migration assigns
source metadata.

## Call Sites

### `apps/api/src/sibyl/jobs/entities.py`

- Behavior: async entity creation, explicit relationships, and task-learning artifact links.
- Default-loop usage: native async write path for `sibyl add` and task completion artifacts.
- Status: native default loop.
- Removal condition: replace the remaining compatibility fallback for non-structured entity types
  after native exact lookup, deduplication, and search cover graph behavior.
- Owner: v0.7 native write adapter.
- Verify: `moon run api:test -- tests/test_jobs_entities.py`.

### `packages/python/sibyl-core/src/sibyl_core/backends/surreal/driver.py`

- Behavior: SurrealDB driver implementing Graphiti driver contracts.
- Default-loop usage: compatibility substrate whenever a Graphiti client is still constructed.
- Status: retained compatibility adapter.
- Removal condition: Graphiti client construction is deleted and native services own graph access.
- Owner: v0.7 Graphiti exit.
- Verify: `moon run core:test`.

### `packages/python/sibyl-core/src/sibyl_core/graph/client.py`

- Behavior: Graphiti client construction, LLM client selection, embedder setup, and driver cloning.
- Default-loop usage: compatibility graph client for remaining legacy write and search surfaces.
- Status: fallback.
- Removal condition: native graph client replaces Graphiti construction and provider adapters.
- Owner: v0.7 Graphiti exit.
- Verify: `moon run core:test -- tests/test_graph_client.py`.

### `packages/python/sibyl-core/src/sibyl_core/graph/entities.py`

- Behavior: entity CRUD, legacy `add_episode`, direct node save, and Graphiti hybrid search
  fallback.
- Default-loop usage: fallback for `add` and graph search; native context retrieval bypasses it in
  native mode.
- Status: fallback.
- Removal condition: native write, exact lookup, semantic search, and entity hydration cover the
  seeded graph behavior without Graphiti node APIs.
- Owner: v0.7 native memory.
- Verify: `moon run core:test -- tests/test_graph_entities.py`.

### `packages/python/sibyl-core/src/sibyl_core/graph/relationships.py`

- Behavior: relationship CRUD and edge hydration through Graphiti edge models.
- Default-loop usage: fallback for explicit graph relationship writes and reads.
- Status: fallback.
- Removal condition: native relation manager owns `relates_to`, `mentions`, and relationship model
  hydration.
- Owner: v0.7 native write adapter.
- Verify: `moon run core:test -- tests/test_graph_relationships.py`.

### `packages/python/sibyl-core/src/sibyl_core/graph/search_interface.py`

- Behavior: Surreal-backed implementation of Graphiti search interface methods.
- Default-loop usage: compare/fallback search scaffolding, not native retrieval's primary path.
- Status: retained compatibility adapter.
- Removal condition: compare mode no longer calls Graphiti search and seeded native retrieval is the
  default path.
- Owner: v0.7 native retrieval.
- Verify: `moon run core:test -- tests/graph/surreal/test_search_interface.py`.

### `packages/python/sibyl-core/src/sibyl_core/graph/cached_embedder.py`

- Behavior: cache wrapper for Graphiti-compatible embedders.
- Default-loop usage: support code for Graphiti client construction.
- Status: retained compatibility adapter.
- Removal condition: native embedding service owns caching without Graphiti embedder types.
- Owner: v0.7 native retrieval.
- Verify: `moon run core:test -- tests/test_graph_client.py`.

### `packages/python/sibyl-core/src/sibyl_core/graph/gemini_embedder.py`

- Behavior: Gemini embedder adapter shaped for Graphiti's embedder interface.
- Default-loop usage: support code for Graphiti client construction.
- Status: retained compatibility adapter.
- Removal condition: native embedding service supports Gemini directly.
- Owner: v0.7 native retrieval.
- Verify: `moon run core:test -- tests/test_graph_client.py`.

### `packages/python/sibyl-core/src/sibyl_core/graph/mock_llm.py`

- Behavior: mock Graphiti LLM client for tests and local extraction without provider calls.
- Default-loop usage: support code for Graphiti extraction compatibility.
- Status: retained compatibility adapter.
- Removal condition: native reflection tests no longer instantiate Graphiti extraction clients.
- Owner: v0.7 reflection.
- Verify: `moon run core:test -- tests/test_graph_client.py tests/test_reflect.py`.

### `packages/python/sibyl-core/src/sibyl_core/graph/surreal/compat/ops/*`

- Behavior: Surreal implementations of Graphiti node, edge, saga, community, and graph operation
  contracts.
- Default-loop usage: compatibility substrate beneath remaining Graphiti client paths.
- Status: retained compatibility adapter package.
- Classification: `compatibility-retain`
- Removal condition: no default or fallback memory path constructs Graphiti or calls Graphiti model
  operation interfaces.
- Owner: v0.7 Graphiti exit.
- Verify: `moon run core:test -- tests/graph/surreal`.

#### `packages/python/sibyl-core/src/sibyl_core/graph/surreal/compat/ops/__init__.py`

- Classification: `compatibility-retain`
- Behavior: package marker for the Graphiti operation adapter modules.
- Owner: v0.8 Graphiti ops disposition.
- Removal condition: delete with the ops package after the Surreal driver stops exposing Graphiti
  operation factories.
- Verify: `moon run inventory-check inventory-typecheck inventory-test`.

#### `packages/python/sibyl-core/src/sibyl_core/graph/surreal/compat/ops/_common.py`

- Classification: `compatibility-retain`
- Behavior: shared query, RecordID, embedding, and record-normalization helpers for Graphiti-shaped
  operation adapters.
- Owner: v0.8 Graphiti ops disposition.
- Removal condition: move with retained compatibility modules or delete when no Graphiti operation
  adapter imports it.
- Verify: `moon run core:graphiti-compatibility-file-test -- tests/graph/surreal`.

#### `packages/python/sibyl-core/src/sibyl_core/graph/surreal/compat/ops/community_edge_ops.py`

- Classification: `compatibility-retain`
- Behavior: Graphiti `CommunityEdgeOperations` over the `has_member` relation.
- Owner: v0.8 Graphiti ops disposition.
- Removal condition: native community membership reads and writes cover admin and compatibility
  callers without Graphiti `CommunityEdge` models.
- Verify: `moon run core:graphiti-compatibility-file-test -- tests/graph/surreal`.

#### `packages/python/sibyl-core/src/sibyl_core/graph/surreal/compat/ops/community_node_ops.py`

- Classification: `compatibility-retain`
- Behavior: Graphiti `CommunityNodeOperations` over the `community` table.
- Owner: v0.8 Graphiti ops disposition.
- Removal condition: native community services own community node persistence, hydration, and lookup
  without Graphiti `CommunityNode` parsers.
- Verify: `moon run core:graphiti-compatibility-file-test -- tests/graph/surreal`.

#### `packages/python/sibyl-core/src/sibyl_core/graph/surreal/compat/ops/entity_edge_ops.py`

- Classification: `migrate-to-native`
- Behavior: Graphiti `EntityEdgeOperations` over native `relates_to` records.
- Owner: v0.8 native relationship manager.
- Removal condition: native relationship manager covers edge save, lookup, semantic payload
  hydration, and temporal reads for all default and admin callers.
- Verify: `moon run core:test -- tests/test_native_relationship_manager.py`.

#### `packages/python/sibyl-core/src/sibyl_core/graph/surreal/compat/ops/entity_node_ops.py`

- Classification: `migrate-to-native`
- Behavior: Graphiti `EntityNodeOperations` over native `entity` records.
- Owner: v0.8 native entity manager.
- Removal condition: native entity manager covers save, bulk save, lookup, embedding hydration, and
  dynamic attribute projection for all default and admin callers.
- Verify: `moon run core:test -- tests/test_native_entity_manager.py`.

#### `packages/python/sibyl-core/src/sibyl_core/graph/surreal/compat/ops/episode_node_ops.py`

- Classification: `historical migration`
- Behavior: Graphiti `EpisodeNodeOperations` over legacy `episode` records.
- Owner: v0.8 archive migration.
- Removal condition: historical Graphiti episode imports project directly into native raw-memory and
  entity records without constructing `EpisodicNode`.
- Verify: `moon run core:test -- tests/test_migrate_archive.py`.

#### `packages/python/sibyl-core/src/sibyl_core/graph/surreal/compat/ops/episodic_edge_ops.py`

- Classification: `migrate-to-native`
- Behavior: Graphiti `EpisodicEdgeOperations` over native `mentions` relations.
- Owner: v0.8 native relationship manager.
- Removal condition: native relationship manager owns episode-to-entity mention creation, hydration,
  and lookup for default writes and historical imports.
- Verify: `moon run core:test -- tests/test_native_relationship_manager.py`.

#### `packages/python/sibyl-core/src/sibyl_core/graph/surreal/compat/ops/graph_operations_interface.py`

- Classification: `compatibility-retain`
- Behavior: adapter that maps Graphiti classmethod-style graph operations back onto Surreal driver
  operation properties.
- Owner: v0.8 Graphiti ops disposition.
- Removal condition: no Graphiti node or edge class is constructed against the Surreal driver.
- Verify: `moon run core:graphiti-compatibility-file-test -- tests/graph/surreal`.

#### `packages/python/sibyl-core/src/sibyl_core/graph/surreal/compat/ops/graph_ops.py`

- Classification: `admin-only`
- Behavior: Graphiti `GraphMaintenanceOperations` for clear-data, schema bootstrap, index deletion,
  legacy community clustering, and community cleanup.
- Owner: v0.8 graph admin.
- Removal condition: native graph admin tools own maintenance and community clustering without
  Graphiti maintenance interfaces.
- Verify: `moon run core:test -- tests/test_tools_admin.py`.

#### `packages/python/sibyl-core/src/sibyl_core/graph/surreal/compat/ops/has_episode_edge_ops.py`

- Classification: `historical migration`
- Behavior: Graphiti `HasEpisodeEdgeOperations` over saga-to-episode `has_episode` relations.
- Owner: v0.8 archive migration.
- Removal condition: saga and episode sequence imports are either dropped as legacy-only metadata or
  projected through native archive restore code.
- Verify: `moon run core:test -- tests/test_migrate_archive.py`.

#### `packages/python/sibyl-core/src/sibyl_core/graph/surreal/compat/ops/next_episode_edge_ops.py`

- Classification: `historical migration`
- Behavior: Graphiti `NextEpisodeEdgeOperations` over episode-to-episode `next_episode` relations.
- Owner: v0.8 archive migration.
- Removal condition: episode ordering imports are either dropped as legacy-only metadata or
  projected through native archive restore code.
- Verify: `moon run core:test -- tests/test_migrate_archive.py`.

#### `packages/python/sibyl-core/src/sibyl_core/graph/surreal/compat/ops/saga_node_ops.py`

- Classification: `historical migration`
- Behavior: Graphiti `SagaNodeOperations` over legacy `saga` records.
- Owner: v0.8 archive migration.
- Removal condition: saga imports are either dropped as legacy-only metadata or projected through
  native archive restore code.
- Verify: `moon run core:test -- tests/test_migrate_archive.py`.

### `packages/python/sibyl-core/src/sibyl_core/tasks/workflow.py`

- Behavior: task workflow transitions and completion learning extraction links.
- Default-loop usage: task completion creates learning episodes and procedures with direct native
  writes, then links them through explicit native relationships and episode mentions.
- Status: native default loop.
- Removal condition: episode mention writes move fully behind the native relationship manager and no
  longer need Surreal driver compatibility helpers.
- Owner: v0.7 reflection.
- Verify: `moon run api:test -- tests/test_tasks_workflow.py`.

## Exit Gate

Wave 6 exits when generated inventory and this hand-authored inventory agree, native mode owns the
default `remember`, `recall`, `context`, `wake`, `explore`, `temporal`, `health`, `manage`,
`link_graph`, and `reflect` loops, and a no-Graphiti smoke test blocks Graphiti imports for those
flows.

## 1.0 Deletion Gate

The v0.7/v0.8 exit gate proved Graphiti was out of the default loop. The v1.0 gate deletes the
remaining supported Graphiti surface:

- every allowlisted `graphiti_core` import is deleted or replaced with Sibyl-native code
- `sibyl-core[compatibility]` no longer installs `graphiti-core`
- dev dependency groups, CI, Docker, Helm, and docs no longer install Graphiti
- compatibility tests are replaced with native archive/import regression tests
- inventory checks fail if Graphiti appears outside historical docs or explicit migration-format
  labels
- the no-Graphiti smoke test becomes a deletion proof for the whole supported runtime, not only the
  default loop

## Dependency Boundary

Default `sibyl-core` installs do not depend on `graphiti-core`. Before 1.0, retained Graphiti code
may still live behind the `compatibility` optional extra plus the `sibyl-core` dev dependency group.
For 1.0, those retained surfaces must be deleted or replaced by native import/projection code. The
generated runtime inventory records dependency scope (`default`, `optional:compatibility`, or
`dependency-group:dev`) and the 1.0 inventory tests should fail if `graphiti-core` appears in any
supported dependency set.

Dependency files:

- `packages/python/sibyl-core/pyproject.toml`
- `uv.lock`

Verify:

- `moon run inventory-check inventory-typecheck inventory-test`
- `uv lock --check`

## No-Graphiti Smoke Plan

The import-blocking smoke test is now the default-loop proof alongside `moon run inventory-check`.
It starts a fresh Python process, blocks `graphiti_core` imports, and exercises native Surreal
memory writes, wake/recall context retrieval, related expansion, temporal history, health/stats,
manage task updates, prioritization, dependency cycle detection, document graph-link helpers,
persisted reflection, CLI import, default MCP tool-module imports, MCP server construction, API app
construction, combined API/MCP app construction, worker entrypoint import, crawler graph-integration
and pipeline imports, and prompt-hook import.

Smoke command:

- `moon run core:no-graphiti-smoke`

Smoke file:

- `packages/python/sibyl-core/tests/test_no_graphiti_default_loop.py`

Default-loop cases:

- `remember`: summarized remember/add writes native graph records.
- `recall`: native retrieval runs with `SIBYL_RETRIEVAL_MODE=native`.
- `context`: context packs and wake packs run through native retrieval and raw memory recall.
- `wake`: wake-layer context uses the same native context-pack path with wake limits.
- `explore`: native entity listing filters through direct Surreal manager reads.
- `temporal`: native relationship history reads source/target names from `relates_to`.
- `health/stats`: native graph health and entity counts run through the Surreal runtime.
- `manage`: native task updates, project prioritization, and dependency cycle detection run without
  Graphiti manager construction.
- `link_graph`: document graph-link helper writes document entities and relationships through native
  Surreal managers.
- `reflect`: persisted reflection runs natively by default, with `SIBYL_NATIVE_WRITE=disabled` as
  the named compatibility rollback.
- `entrypoints`: CLI, default MCP tool modules, MCP server construction, API app construction,
  combined API/MCP app construction, worker entrypoint import, and prompt-hook imports stay
  Graphiti-free, including crawler graph-integration and pipeline imports.

Closure condition:

- The smoke test installs an import blocker for `graphiti_core`, exercises the core default-loop
  cases plus native explore, temporal, health/stats, manage actions, document graph-link helpers,
  default entrypoints, default API/MCP app factories, and default MCP tool-module imports with
  native flags, and fails on any import or construction path that reaches Graphiti.
