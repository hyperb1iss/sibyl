# Runtime Inventory

Generated from code by `tools/inventory/runtime_surface.py`. Do not hand-edit.

## Summary
- REST routers: 24
- Top-level HTTP routes: 2
- WebSocket routes: 1
- MCP tools: 8
- MCP resources: 2
- SQLModel tables: 0
- Raw SQL query usage files: 0
- Session-backed storage access files: 0
- Graphiti import files: 21
- Dependency records: 4

## API Surface

### Mounted REST routers
- `backups_router`
- `entities_router`
- `tasks_router`
- `session_router`
- `epics_router`
- `search_router`
- `context_router`
- `graph_router`
- `admin_router`
- `auth_router`
- `crawler_router`
- `orgs_router`
- `org_members_router`
- `org_invitations_router`
- `project_members_router`
- `invitations_router`
- `rag_router`
- `jobs_router`
- `logs_router`
- `memory_router`
- `metrics_router`
- `settings_router`
- `setup_router`
- `users_router`

### Top-level HTTP routes
- `GET /` → `root`
- `GET /health` → `health_check`

### WebSocket routes
- `/ws` → `websocket_handler`

## MCP Surface

### Tools
- `add` in `apps/api/src/sibyl/server.py`
- `context` in `apps/api/src/sibyl/server.py`
- `explore` in `apps/api/src/sibyl/server.py`
- `logs` in `apps/api/src/sibyl/server.py`
- `manage` in `apps/api/src/sibyl/server.py`
- `reflect` in `apps/api/src/sibyl/server.py`
- `remember` in `apps/api/src/sibyl/server.py`
- `search` in `apps/api/src/sibyl/server.py`

### Resources
- `sibyl://health` via `health_resource` in `apps/api/src/sibyl/server.py`
- `sibyl://stats` via `stats_resource` in `apps/api/src/sibyl/server.py`

## Storage Coupling

### SQLModel tables

### Raw SQL query usage files

### Session-backed storage access files

### Graphiti import files
- `packages/python/sibyl-core/src/sibyl_core/backends/surreal/driver.py` — class: `compatibility`; imports: `graphiti_core.driver.driver`
- `packages/python/sibyl-core/src/sibyl_core/graph/cached_embedder.py` — class: `compatibility`; imports: `graphiti_core.embedder.client`
- `packages/python/sibyl-core/src/sibyl_core/graph/client.py` — class: `compatibility`; imports: `graphiti_core`, `graphiti_core.driver.driver`, `graphiti_core.embedder.client`, `graphiti_core.helpers`, `graphiti_core.llm_client`, `graphiti_core.llm_client.anthropic_client`, `graphiti_core.llm_client.config`, `graphiti_core.llm_client.openai_client`
- `packages/python/sibyl-core/src/sibyl_core/graph/entities.py` — class: `compatibility`; imports: `graphiti_core.nodes`, `graphiti_core.search.search_config_recipes`
- `packages/python/sibyl-core/src/sibyl_core/graph/gemini_embedder.py` — class: `compatibility`; imports: `graphiti_core.embedder.client`
- `packages/python/sibyl-core/src/sibyl_core/graph/mock_llm.py` — class: `test`; imports: `graphiti_core.llm_client.client`, `graphiti_core.llm_client.config`, `graphiti_core.prompts.models`
- `packages/python/sibyl-core/src/sibyl_core/graph/relationships.py` — class: `compatibility`; imports: `graphiti_core.edges`, `graphiti_core.errors`
- `packages/python/sibyl-core/src/sibyl_core/graph/search_interface.py` — class: `compatibility`; imports: `graphiti_core.driver.record_parsers`, `graphiti_core.driver.search_interface.search_interface`, `graphiti_core.nodes`
- `packages/python/sibyl-core/src/sibyl_core/graph/surreal/compat/ops/_common.py` — class: `compatibility`; imports: `graphiti_core.driver.query_executor`
- `packages/python/sibyl-core/src/sibyl_core/graph/surreal/compat/ops/community_edge_ops.py` — class: `compatibility`; imports: `graphiti_core.driver.operations.community_edge_ops`, `graphiti_core.driver.query_executor`, `graphiti_core.edges`, `graphiti_core.errors`, `graphiti_core.helpers`
- `packages/python/sibyl-core/src/sibyl_core/graph/surreal/compat/ops/community_node_ops.py` — class: `compatibility`; imports: `graphiti_core.driver.operations.community_node_ops`, `graphiti_core.driver.query_executor`, `graphiti_core.driver.record_parsers`, `graphiti_core.errors`, `graphiti_core.nodes`
- `packages/python/sibyl-core/src/sibyl_core/graph/surreal/compat/ops/entity_edge_ops.py` — class: `compatibility`; imports: `graphiti_core.driver.operations.entity_edge_ops`, `graphiti_core.driver.query_executor`, `graphiti_core.driver.record_parsers`, `graphiti_core.edges`, `graphiti_core.errors`
- `packages/python/sibyl-core/src/sibyl_core/graph/surreal/compat/ops/entity_node_ops.py` — class: `compatibility`; imports: `graphiti_core.driver.operations.entity_node_ops`, `graphiti_core.driver.query_executor`, `graphiti_core.driver.record_parsers`, `graphiti_core.errors`, `graphiti_core.nodes`
- `packages/python/sibyl-core/src/sibyl_core/graph/surreal/compat/ops/episode_node_ops.py` — class: `compatibility`; imports: `graphiti_core.driver.operations.episode_node_ops`, `graphiti_core.driver.query_executor`, `graphiti_core.driver.record_parsers`, `graphiti_core.errors`, `graphiti_core.nodes`
- `packages/python/sibyl-core/src/sibyl_core/graph/surreal/compat/ops/episodic_edge_ops.py` — class: `compatibility`; imports: `graphiti_core.driver.operations.episodic_edge_ops`, `graphiti_core.driver.query_executor`, `graphiti_core.edges`, `graphiti_core.errors`, `graphiti_core.helpers`
- `packages/python/sibyl-core/src/sibyl_core/graph/surreal/compat/ops/graph_operations_interface.py` — class: `compatibility`; imports: `graphiti_core.driver.graph_operations.graph_operations`, `graphiti_core.driver.record_parsers`, `graphiti_core.edges`, `graphiti_core.nodes`
- `packages/python/sibyl-core/src/sibyl_core/graph/surreal/compat/ops/graph_ops.py` — class: `compatibility`; imports: `graphiti_core.driver.operations.graph_ops`, `graphiti_core.driver.operations.graph_utils`, `graphiti_core.driver.query_executor`, `graphiti_core.driver.record_parsers`, `graphiti_core.nodes`
- `packages/python/sibyl-core/src/sibyl_core/graph/surreal/compat/ops/has_episode_edge_ops.py` — class: `compatibility`; imports: `graphiti_core.driver.operations.has_episode_edge_ops`, `graphiti_core.driver.query_executor`, `graphiti_core.edges`, `graphiti_core.errors`, `graphiti_core.helpers`
- `packages/python/sibyl-core/src/sibyl_core/graph/surreal/compat/ops/next_episode_edge_ops.py` — class: `compatibility`; imports: `graphiti_core.driver.operations.next_episode_edge_ops`, `graphiti_core.driver.query_executor`, `graphiti_core.edges`, `graphiti_core.errors`, `graphiti_core.helpers`
- `packages/python/sibyl-core/src/sibyl_core/graph/surreal/compat/ops/saga_node_ops.py` — class: `compatibility`; imports: `graphiti_core.driver.operations.saga_node_ops`, `graphiti_core.driver.query_executor`, `graphiti_core.errors`, `graphiti_core.helpers`, `graphiti_core.nodes`
- `packages/python/sibyl-core/src/sibyl_core/tools/admin.py` — class: `admin`; imports: `graphiti_core.edges`, `graphiti_core.nodes`

## Dependency Inventory

### Legacy and transition dependencies
- none

### Graph runtime dependencies
| Project | Scope | Dependency |
| ------- | ----- | ---------- |
| `packages/python/sibyl-core/pyproject.toml` | `dependency-group:dev` | `graphiti-core[anthropic,google-genai]>=0.28.2` |
| `packages/python/sibyl-core/pyproject.toml` | `optional:compatibility` | `graphiti-core[anthropic,google-genai]>=0.28.2` |

### Target SurrealDB dependencies
| Project | Scope | Dependency |
| ------- | ----- | ---------- |
| `apps/api/pyproject.toml` | `default` | `surrealdb>=1.0.8,<3.0` |
| `packages/python/sibyl-core/pyproject.toml` | `default` | `surrealdb>=1.0.8,<3.0` |
