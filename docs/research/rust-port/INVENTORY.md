# Runtime Inventory

Generated from code by `tools/inventory/runtime_surface.py`. Do not hand-edit.

## Summary
- REST routers: 24
- Top-level HTTP routes: 2
- WebSocket routes: 1
- MCP tools: 8
- MCP resources: 2
- SQLModel tables: 24
- Raw SQL query usage files: 30
- Session-backed storage access files: 1
- Graphiti import files: 23
- Dependency records: 8

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
- `User`
- `LoginHistory`
- `PasswordResetToken`
- `Organization`
- `OrganizationMember`
- `ApiKey`
- `UserSession`
- `AuditLog`
- `RawCapture`
- `OrganizationInvitation`
- `DeviceAuthorizationRequest`
- `OAuthConnection`
- `Team`
- `TeamMember`
- `Project`
- `ProjectMember`
- `TeamProject`
- `ApiKeyProjectScope`
- `CrawlSource`
- `CrawledDocument`
- `DocumentChunk`
- `SystemSetting`
- `BackupSettings`
- `Backup`

### Raw SQL query usage files
- `apps/api/src/sibyl/db/connection.py` — session imports: `AsyncSession`, `async_sessionmaker`; query imports: `text`; session calls: `execute`; query calls: `text`
- `apps/api/src/sibyl/db/models.py` — session imports: none; query imports: `text`; session calls: none; query calls: `text`
- `apps/api/src/sibyl/db/project_sync.py` — session imports: `AsyncSession`; query imports: `delete`, `select`, `update`; session calls: `add`, `execute`; query calls: `delete`, `select`, `update`
- `apps/api/src/sibyl/db/sync.py` — session imports: `AsyncSession`; query imports: `select`; session calls: `add`, `execute`; query calls: `select`
- `apps/api/src/sibyl/persistence/auth_archive.py` — session imports: none; query imports: `text`; session calls: `execute`; query calls: `text`
- `apps/api/src/sibyl/persistence/content_archive.py` — session imports: none; query imports: `text`; session calls: `execute`; query calls: `text`
- `apps/api/src/sibyl/persistence/legacy/auth.py` — session imports: `AsyncSession`; query imports: `select`; session calls: `execute`, `get`; query calls: `select`
- `apps/api/src/sibyl/persistence/legacy/auth_managers/api_keys.py` — session imports: `AsyncSession`; query imports: `select`; session calls: none; query calls: `select`
- `apps/api/src/sibyl/persistence/legacy/auth_managers/device_authorization.py` — session imports: `AsyncSession`; query imports: `select`; session calls: none; query calls: `select`
- `apps/api/src/sibyl/persistence/legacy/auth_managers/invitations.py` — session imports: `AsyncSession`; query imports: `select`; session calls: none; query calls: `select`
- `apps/api/src/sibyl/persistence/legacy/auth_managers/memberships.py` — session imports: `AsyncSession`; query imports: `select`; session calls: none; query calls: `select`
- `apps/api/src/sibyl/persistence/legacy/auth_managers/organizations.py` — session imports: `AsyncSession`; query imports: `select`; session calls: none; query calls: `select`
- `apps/api/src/sibyl/persistence/legacy/auth_managers/password_reset.py` — session imports: `AsyncSession`; query imports: `select`; session calls: none; query calls: `select`
- `apps/api/src/sibyl/persistence/legacy/auth_managers/sessions.py` — session imports: `AsyncSession`; query imports: `select`; session calls: none; query calls: `select`
- `apps/api/src/sibyl/persistence/legacy/auth_managers/users.py` — session imports: `AsyncSession`; query imports: `select`; session calls: none; query calls: `select`
- `apps/api/src/sibyl/persistence/legacy/auth_runtime.py` — session imports: none; query imports: `select`; session calls: none; query calls: `select`
- `apps/api/src/sibyl/persistence/legacy/backups.py` — session imports: `AsyncSession`; query imports: `select`; session calls: `add`, `commit`, `delete`, `execute`, `get`, `refresh`; query calls: `select`
- `apps/api/src/sibyl/persistence/legacy/crawler.py` — session imports: none; query imports: `select`; session calls: none; query calls: `select`
- `apps/api/src/sibyl/persistence/legacy/entities.py` — session imports: `AsyncSession`; query imports: `select`; session calls: `execute`, `refresh`; query calls: `select`
- `apps/api/src/sibyl/persistence/legacy/org_members.py` — session imports: none; query imports: `select`; session calls: `execute`; query calls: `select`
- `apps/api/src/sibyl/persistence/legacy/orgs.py` — session imports: none; query imports: `select`; session calls: `execute`; query calls: `select`
- `apps/api/src/sibyl/persistence/legacy/project_members.py` — session imports: `AsyncSession`; query imports: `select`; session calls: `add`, `delete`, `execute`, `get`; query calls: `select`
- `apps/api/src/sibyl/persistence/legacy/project_sync.py` — session imports: none; query imports: `select`; session calls: `commit`, `execute`; query calls: `select`
- `apps/api/src/sibyl/persistence/legacy/rag.py` — session imports: none; query imports: `select`; session calls: none; query calls: `select`
- `apps/api/src/sibyl/persistence/legacy/rls.py` — session imports: none; query imports: `text`; session calls: `execute`; query calls: `text`
- `apps/api/src/sibyl/persistence/legacy/settings.py` — session imports: none; query imports: `select`; session calls: `execute`; query calls: `select`
- `apps/api/src/sibyl/persistence/legacy/setup.py` — session imports: none; query imports: `select`; session calls: `execute`, `get`; query calls: `select`
- `apps/api/src/sibyl/persistence/legacy/sidecar_startup.py` — session imports: none; query imports: `text`; session calls: `execute`; query calls: `text`
- `apps/api/src/sibyl/persistence/legacy/system_settings.py` — session imports: `AsyncSession`; query imports: `select`; session calls: `add`, `delete`, `execute`, `refresh`; query calls: `select`
- `apps/api/src/sibyl/persistence/legacy/users.py` — session imports: `AsyncSession`; query imports: `select`; session calls: `commit`, `delete`, `execute`, `get`; query calls: `select`

### Session-backed storage access files
- `apps/api/src/sibyl/persistence/legacy/auth_managers/audit.py` — session imports: `AsyncSession`; query imports: none; session calls: none; query calls: none

### Graphiti import files
- `apps/api/src/sibyl/jobs/entities.py` — `graphiti_core.edges`
- `apps/api/src/sibyl/persistence/graph_runtime.py` — `graphiti_core.edges`, `graphiti_core.errors`
- `packages/python/sibyl-core/src/sibyl_core/backends/surreal/driver.py` — `graphiti_core.driver.driver`
- `packages/python/sibyl-core/src/sibyl_core/graph/cached_embedder.py` — `graphiti_core.embedder.client`
- `packages/python/sibyl-core/src/sibyl_core/graph/client.py` — `graphiti_core`, `graphiti_core.driver.driver`, `graphiti_core.driver.falkordb`, `graphiti_core.driver.falkordb_driver`, `graphiti_core.embedder.client`, `graphiti_core.embedder.openai`, `graphiti_core.helpers`, `graphiti_core.llm_client`, `graphiti_core.llm_client.anthropic_client`, `graphiti_core.llm_client.config`, `graphiti_core.llm_client.openai_client`, `graphiti_core.search.search_utils`
- `packages/python/sibyl-core/src/sibyl_core/graph/entities.py` — `graphiti_core.nodes`, `graphiti_core.search.search_config_recipes`
- `packages/python/sibyl-core/src/sibyl_core/graph/gemini_embedder.py` — `graphiti_core.embedder.client`
- `packages/python/sibyl-core/src/sibyl_core/graph/mock_llm.py` — `graphiti_core.llm_client.client`, `graphiti_core.llm_client.config`, `graphiti_core.prompts.models`
- `packages/python/sibyl-core/src/sibyl_core/graph/relationships.py` — `graphiti_core.edges`, `graphiti_core.errors`
- `packages/python/sibyl-core/src/sibyl_core/graph/search_interface.py` — `graphiti_core.driver.record_parsers`, `graphiti_core.driver.search_interface.search_interface`, `graphiti_core.edges`, `graphiti_core.helpers`, `graphiti_core.nodes`, `graphiti_core.search`, `graphiti_core.search.search_utils`
- `packages/python/sibyl-core/src/sibyl_core/graph/surreal/ops/_common.py` — `graphiti_core.driver.query_executor`
- `packages/python/sibyl-core/src/sibyl_core/graph/surreal/ops/community_edge_ops.py` — `graphiti_core.driver.operations.community_edge_ops`, `graphiti_core.driver.query_executor`, `graphiti_core.edges`, `graphiti_core.errors`, `graphiti_core.helpers`
- `packages/python/sibyl-core/src/sibyl_core/graph/surreal/ops/community_node_ops.py` — `graphiti_core.driver.operations.community_node_ops`, `graphiti_core.driver.query_executor`, `graphiti_core.driver.record_parsers`, `graphiti_core.errors`, `graphiti_core.nodes`
- `packages/python/sibyl-core/src/sibyl_core/graph/surreal/ops/entity_edge_ops.py` — `graphiti_core.driver.operations.entity_edge_ops`, `graphiti_core.driver.query_executor`, `graphiti_core.driver.record_parsers`, `graphiti_core.edges`, `graphiti_core.errors`
- `packages/python/sibyl-core/src/sibyl_core/graph/surreal/ops/entity_node_ops.py` — `graphiti_core.driver.operations.entity_node_ops`, `graphiti_core.driver.query_executor`, `graphiti_core.driver.record_parsers`, `graphiti_core.errors`, `graphiti_core.nodes`
- `packages/python/sibyl-core/src/sibyl_core/graph/surreal/ops/episode_node_ops.py` — `graphiti_core.driver.operations.episode_node_ops`, `graphiti_core.driver.query_executor`, `graphiti_core.driver.record_parsers`, `graphiti_core.errors`, `graphiti_core.nodes`
- `packages/python/sibyl-core/src/sibyl_core/graph/surreal/ops/episodic_edge_ops.py` — `graphiti_core.driver.operations.episodic_edge_ops`, `graphiti_core.driver.query_executor`, `graphiti_core.edges`, `graphiti_core.errors`, `graphiti_core.helpers`
- `packages/python/sibyl-core/src/sibyl_core/graph/surreal/ops/graph_operations_interface.py` — `graphiti_core.driver.graph_operations.graph_operations`, `graphiti_core.driver.record_parsers`, `graphiti_core.edges`, `graphiti_core.nodes`
- `packages/python/sibyl-core/src/sibyl_core/graph/surreal/ops/graph_ops.py` — `graphiti_core.driver.operations.graph_ops`, `graphiti_core.driver.operations.graph_utils`, `graphiti_core.driver.query_executor`, `graphiti_core.driver.record_parsers`, `graphiti_core.nodes`
- `packages/python/sibyl-core/src/sibyl_core/graph/surreal/ops/has_episode_edge_ops.py` — `graphiti_core.driver.operations.has_episode_edge_ops`, `graphiti_core.driver.query_executor`, `graphiti_core.edges`, `graphiti_core.errors`, `graphiti_core.helpers`
- `packages/python/sibyl-core/src/sibyl_core/graph/surreal/ops/next_episode_edge_ops.py` — `graphiti_core.driver.operations.next_episode_edge_ops`, `graphiti_core.driver.query_executor`, `graphiti_core.edges`, `graphiti_core.errors`, `graphiti_core.helpers`
- `packages/python/sibyl-core/src/sibyl_core/graph/surreal/ops/saga_node_ops.py` — `graphiti_core.driver.operations.saga_node_ops`, `graphiti_core.driver.query_executor`, `graphiti_core.errors`, `graphiti_core.helpers`, `graphiti_core.nodes`
- `packages/python/sibyl-core/src/sibyl_core/tasks/workflow.py` — `graphiti_core.edges`

## Dependency Inventory

### Legacy and transition dependencies
| Project | Dependency |
| ------- | ---------- |
| `apps/api/pyproject.toml` | `alembic>=1.17.2` |
| `apps/api/pyproject.toml` | `arq>=0.26.3` |
| `apps/api/pyproject.toml` | `asyncpg>=0.31.0` |
| `apps/api/pyproject.toml` | `pgvector>=0.4.2` |
| `apps/api/pyproject.toml` | `sqlmodel>=0.0.27` |
| `packages/python/sibyl-core/pyproject.toml` | `graphiti-core[falkordb,anthropic,google-genai]>=0.28.2` |

### Target SurrealDB dependencies
| Project | Dependency |
| ------- | ---------- |
| `apps/api/pyproject.toml` | `surrealdb>=1.0.8,<3.0` |
| `packages/python/sibyl-core/pyproject.toml` | `surrealdb>=1.0.8,<3.0` |
