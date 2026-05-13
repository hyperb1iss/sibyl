# SurrealDB Graphiti Exit Inventory

Status: Wave 6 inventory baseline.

This document is hand-authored removal intent. The generated runtime source of truth remains
`docs/research/rust-port/INVENTORY.md`, and `moon run inventory-check` fails when a generated
Graphiti import is not classified here.

## Coverage Rule

Every generated Graphiti import path must appear as a backticked path in this document, except
`packages/python/sibyl-core/src/sibyl_core/graph/surreal/ops/*`, which covers the Graphiti operation
adapter package as one named compatibility surface.

## Default Loop Position

- `remember`: raw capture, summarized `sibyl add`, API entity creation, and async create jobs write
  through native Surreal collaborators on the default path.
- `recall`, `context`, and `wake`: native retrieval runs through direct Surreal fulltext, raw
  recall, graph expansion, and native related-item hydration without importing Graphiti.
- `reflect`: persisted reflection sources and candidates write native graph records when
  `SIBYL_NATIVE_WRITE=enabled`. Review-mode raw candidate storage remains the explicit review path.
- Graphiti remains only in named compatibility, compare-mode, admin, and migration surfaces until
  their removal conditions below are met.

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

### `apps/api/src/sibyl/persistence/graph_runtime.py`

- Behavior: API graph runtime facade around entity, relationship, and graph traversal managers.
- Default-loop usage: compatibility surface for admin, metrics, and graph route reads.
- Status: retained compatibility adapter.
- Removal condition: API graph runtime resolves to native Surreal managers with no Graphiti edge or
  error model imports.
- Owner: v0.7 Graphiti exit.
- Verify: `moon run api:test`.

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

### `packages/python/sibyl-core/src/sibyl_core/graph/surreal/ops/*`

- Behavior: Surreal implementations of Graphiti node, edge, saga, community, and graph operation
  contracts.
- Default-loop usage: compatibility substrate beneath remaining Graphiti client paths.
- Status: retained compatibility adapter package.
- Removal condition: no default or fallback memory path constructs Graphiti or calls Graphiti model
  operation interfaces.
- Owner: v0.7 Graphiti exit.
- Verify: `moon run core:test -- tests/graph/surreal`.

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
default `remember`, `recall`, `context`, `wake`, and `reflect` loops, and a no-Graphiti smoke test
blocks Graphiti imports for those flows.

## No-Graphiti Smoke Plan

The import-blocking smoke test is now the default-loop proof alongside `moon run inventory-check`.
It starts a fresh Python process, blocks `graphiti_core` imports, and exercises native Surreal
memory writes, wake/recall context retrieval, related expansion, and persisted reflection.

Smoke command:

- `moon run core:no-graphiti-smoke`

Smoke file:

- `packages/python/sibyl-core/tests/test_no_graphiti_default_loop.py`

Default-loop cases:

- `remember`: summarized remember/add writes native graph records.
- `recall`: native retrieval runs with `SIBYL_RETRIEVAL_MODE=native`.
- `context`: context packs and wake packs run through native retrieval and raw memory recall.
- `wake`: wake-layer context uses the same native context-pack path with wake limits.
- `reflect`: persisted reflection runs with `SIBYL_NATIVE_WRITE=enabled`.

Closure condition:

- The smoke test installs an import blocker for `graphiti_core`, exercises the five default-loop
  cases above with native flags, and fails on any import or construction path that reaches Graphiti.
