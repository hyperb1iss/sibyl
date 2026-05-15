# Runtime Inventory

Generated from code by `tools/inventory/runtime_surface.py`. Do not hand-edit.

## Summary
- REST routers: 25
- Top-level HTTP routes: 2
- WebSocket routes: 1
- MCP tools: 11
- MCP resources: 2
- SQLModel tables: 0
- Raw SQL query usage files: 0
- Session-backed storage access files: 0
- Graphiti import files: 21
- Retained legacy term files: 90
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
- `synthesis_router`
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
- `synthesis_draft` in `apps/api/src/sibyl/server.py`
- `synthesis_plan` in `apps/api/src/sibyl/server.py`
- `synthesis_verify` in `apps/api/src/sibyl/server.py`

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

## Retained Legacy Term Inventory

Every active doc or deployment config that mentions retired or optional legacy services
must carry an owner and reason here.

| File | Terms | Matches | Owner | Reason |
| ---- | ----- | ------- | ----- | ------ |
| `.env.example` | `falkor`, `graphiti`, `postgres`, `redis` | 17 | dev env templates | Environment templates keep legacy ports, migration knobs, and optional Redis/Valkey secrets. |
| `.env.quickstart.example` | `falkor`, `postgres` | 2 | dev env templates | Environment templates keep legacy ports, migration knobs, and optional Redis/Valkey secrets. |
| `.env.quickstart.test` | `falkor`, `postgres` | 2 | dev env templates | Environment templates keep legacy ports, migration knobs, and optional Redis/Valkey secrets. |
| `.env.test.example` | `falkor`, `postgres` | 10 | dev env templates | Environment templates keep legacy ports, migration knobs, and optional Redis/Valkey secrets. |
| `AGENTS.md` | `graphiti`, `redis`, `valkey` | 4 | project instructions | Project agent guides preserve ports, archive shapes, and compatibility boundaries. |
| `CLAUDE.md` | `graphiti`, `redis`, `valkey` | 4 | project instructions | Project agent guides preserve ports, archive shapes, and compatibility boundaries. |
| `README.md` | `falkor`, `graphiti`, `postgres`, `redis`, `valkey` | 20 | v0.8 pure Surreal closure | Default quickstart plus explicit legacy migration and optional Redis coordination notes. |
| `Tiltfile` | `redis`, `valkey` | 14 | local Kubernetes/Tilt dev | Local Tilt and Helm dev keep Redis/Valkey as explicit coordination while Surreal owns data. |
| `apps/api/README.md` | `postgres`, `redis` | 12 | v0.8 packaged docs | Packaged README and skill docs retain migration and optional coordination language. |
| `apps/api/moon.yml` | `graphiti` | 7 | v0.8 packaged docs | Packaged README and skill docs retain migration and optional coordination language. |
| `apps/api/pyproject.toml` | `graphiti`, `redis` | 4 | v0.8 deployment config | Compose and chart files retain Redis as an explicit coordination profile or chart option. |
| `apps/cli/README.md` | `redis` | 1 | v0.8 packaged docs | Packaged README and skill docs retain migration and optional coordination language. |
| `apps/cli/src/sibyl_cli/data/skills/sibyl/EXAMPLES.md` | `redis` | 3 | v0.8 packaged docs | Packaged README and skill docs retain migration and optional coordination language. |
| `apps/cli/src/sibyl_cli/data/skills/sibyl/SKILL.md` | `redis` | 4 | v0.8 packaged docs | Packaged README and skill docs retain migration and optional coordination language. |
| `charts/sibyl/Chart.yaml` | `valkey` | 1 | v0.8 deployment config | Compose and chart files retain Redis as an explicit coordination profile or chart option. |
| `charts/sibyl/templates/backend-deployment.yaml` | `redis` | 6 | v0.8 deployment config | Compose and chart files retain Redis as an explicit coordination profile or chart option. |
| `charts/sibyl/templates/configmap.yaml` | `redis` | 13 | v0.8 deployment config | Compose and chart files retain Redis as an explicit coordination profile or chart option. |
| `charts/sibyl/templates/redis-secret.yaml` | `redis` | 8 | v0.8 deployment config | Compose and chart files retain Redis as an explicit coordination profile or chart option. |
| `charts/sibyl/templates/worker-deployment.yaml` | `redis` | 6 | v0.8 deployment config | Compose and chart files retain Redis as an explicit coordination profile or chart option. |
| `charts/sibyl/values.yaml` | `redis`, `valkey` | 17 | v0.8 deployment config | Compose and chart files retain Redis as an explicit coordination profile or chart option. |
| `docker-compose.prod.yml` | `falkor`, `redis`, `valkey` | 21 | v0.8 deployment config | Compose and chart files retain Redis as an explicit coordination profile or chart option. |
| `docker-compose.quickstart.yml` | `falkor`, `redis`, `valkey` | 21 | v0.8 deployment config | Compose and chart files retain Redis as an explicit coordination profile or chart option. |
| `docker-compose.yml` | `redis`, `valkey` | 11 | v0.8 deployment config | Compose and chart files retain Redis as an explicit coordination profile or chart option. |
| `docs/api/auth-authorization.md` | `postgres` | 3 | v0.8 API/CLI docs | API and CLI docs reference memory history, migration payloads, or optional coordination. |
| `docs/api/mcp-add.md` | `graphiti`, `postgres`, `redis` | 9 | v0.8 API/CLI docs | API and CLI docs reference memory history, migration payloads, or optional coordination. |
| `docs/api/mcp-explore.md` | `postgres` | 1 | v0.8 API/CLI docs | API and CLI docs reference memory history, migration payloads, or optional coordination. |
| `docs/api/rest-projects.md` | `postgres`, `redis` | 5 | v0.8 API/CLI docs | API and CLI docs reference memory history, migration payloads, or optional coordination. |
| `docs/api/rest-tasks.md` | `redis` | 1 | v0.8 API/CLI docs | API and CLI docs reference memory history, migration payloads, or optional coordination. |
| `docs/architecture/PERMISSION_SYSTEM_AUDIT.md` | `falkor`, `postgres` | 23 | v0.8 architecture | Architecture and release plans preserve migration, benchmark, and compatibility history. |
| `docs/architecture/PERMISSION_SYSTEM_PLAN.md` | `falkor`, `postgres`, `redis` | 13 | v0.8 architecture | Architecture and release plans preserve migration, benchmark, and compatibility history. |
| `docs/architecture/SIBYL_NORTHSTAR.md` | `falkor`, `graphiti`, `postgres`, `redis` | 38 | v0.8 architecture | Architecture and release plans preserve migration, benchmark, and compatibility history. |
| `docs/architecture/SIBYL_POST_V08_SYNTHESIS_AND_MEMORY_WORKSPACE_PLAN.md` | `graphiti` | 2 | v0.8 architecture | Architecture and release plans preserve migration, benchmark, and compatibility history. |
| `docs/architecture/SIBYL_V08_PURE_SURREAL_CLOSURE_AND_MEMORY_TRUST_PLAN.md` | `falkor`, `graphiti`, `postgres`, `redis`, `valkey` | 163 | v0.8 architecture | Architecture and release plans preserve migration, benchmark, and compatibility history. |
| `docs/architecture/SIBYL_V08_PURE_SURREAL_CLOSURE_EXECUTION_PLAN.md` | `falkor`, `graphiti`, `postgres`, `redis`, `valkey` | 88 | v0.8 architecture | Architecture and release plans preserve migration, benchmark, and compatibility history. |
| `docs/architecture/SURREALDB_GRAPHITI_EXIT_BENCHMARK_EVIDENCE.md` | `graphiti` | 23 | v0.8 architecture | Architecture and release plans preserve migration, benchmark, and compatibility history. |
| `docs/architecture/SURREALDB_GRAPHITI_EXIT_INVENTORY.md` | `graphiti` | 101 | v0.8 architecture | Architecture and release plans preserve migration, benchmark, and compatibility history. |
| `docs/architecture/SURREALDB_NATIVE_MEMORY_CORE_SPEC.md` | `falkor`, `graphiti`, `postgres` | 48 | v0.8 architecture | Architecture and release plans preserve migration, benchmark, and compatibility history. |
| `docs/architecture/SURREALDB_PHASE1_BUGS.md` | `postgres`, `redis` | 3 | v0.8 architecture | Architecture and release plans preserve migration, benchmark, and compatibility history. |
| `docs/architecture/SURREALDB_PHASE2_AUTH_MIGRATION.md` | `falkor`, `postgres`, `redis` | 69 | v0.8 architecture | Architecture and release plans preserve migration, benchmark, and compatibility history. |
| `docs/architecture/SURREALDB_PHASE2_LIVE_GATES.md` | `postgres` | 7 | v0.8 architecture | Architecture and release plans preserve migration, benchmark, and compatibility history. |
| `docs/architecture/SURREALDB_PHASE3_BURNDOWN.md` | `falkor`, `graphiti`, `postgres` | 62 | v0.8 architecture | Architecture and release plans preserve migration, benchmark, and compatibility history. |
| `docs/architecture/SURREALDB_V07_GRAPHITI_EXIT_AND_PURE_SURREAL_PLAN.md` | `falkor`, `graphiti`, `postgres`, `redis` | 92 | v0.8 architecture | Architecture and release plans preserve migration, benchmark, and compatibility history. |
| `docs/architecture/TASKIQ_MIGRATION_PLAN.md` | `redis` | 57 | v0.8 architecture | Architecture and release plans preserve migration, benchmark, and compatibility history. |
| `docs/cli/add.md` | `falkor`, `graphiti`, `postgres`, `redis` | 9 | v0.8 API/CLI docs | API and CLI docs reference memory history, migration payloads, or optional coordination. |
| `docs/cli/entity.md` | `postgres`, `redis` | 3 | v0.8 API/CLI docs | API and CLI docs reference memory history, migration payloads, or optional coordination. |
| `docs/cli/project.md` | `postgres` | 2 | v0.8 API/CLI docs | API and CLI docs reference memory history, migration payloads, or optional coordination. |
| `docs/cli/search.md` | `postgres` | 1 | v0.8 API/CLI docs | API and CLI docs reference memory history, migration payloads, or optional coordination. |
| `docs/cli/task-create.md` | `redis` | 2 | v0.8 API/CLI docs | API and CLI docs reference memory history, migration payloads, or optional coordination. |
| `docs/cli/task-lifecycle.md` | `postgres`, `redis` | 7 | v0.8 API/CLI docs | API and CLI docs reference memory history, migration payloads, or optional coordination. |
| `docs/deployment/docker-compose.md` | `postgres`, `redis`, `valkey` | 39 | v0.8 deployment docs | Deployment docs retain optional Redis/Valkey coordination and historical restore notes. |
| `docs/deployment/environment.md` | `graphiti`, `postgres`, `redis`, `valkey` | 51 | v0.8 deployment docs | Deployment docs retain optional Redis/Valkey coordination and historical restore notes. |
| `docs/deployment/helm-chart.md` | `postgres`, `redis`, `valkey` | 15 | v0.8 deployment docs | Deployment docs retain optional Redis/Valkey coordination and historical restore notes. |
| `docs/deployment/index.md` | `falkor`, `postgres`, `redis`, `valkey` | 7 | v0.8 deployment docs | Deployment docs retain optional Redis/Valkey coordination and historical restore notes. |
| `docs/deployment/kubernetes.md` | `falkor`, `postgres`, `redis`, `valkey` | 15 | v0.8 deployment docs | Deployment docs retain optional Redis/Valkey coordination and historical restore notes. |
| `docs/deployment/monitoring.md` | `graphiti`, `redis` | 2 | v0.8 deployment docs | Deployment docs retain optional Redis/Valkey coordination and historical restore notes. |
| `docs/deployment/tilt-minikube.md` | `falkor`, `postgres`, `redis`, `valkey` | 19 | v0.8 deployment docs | Deployment docs retain optional Redis/Valkey coordination and historical restore notes. |
| `docs/deployment/troubleshooting.md` | `graphiti`, `postgres`, `redis` | 14 | v0.8 deployment docs | Deployment docs retain optional Redis/Valkey coordination and historical restore notes. |
| `docs/guide/capturing-knowledge.md` | `falkor`, `redis` | 11 | v0.8 docs | User guides label legacy services as historical migration or explicit coordination opt-in. |
| `docs/guide/claude-code.md` | `redis` | 2 | v0.8 docs | User guides label legacy services as historical migration or explicit coordination opt-in. |
| `docs/guide/entity-types.md` | `redis` | 1 | v0.8 docs | User guides label legacy services as historical migration or explicit coordination opt-in. |
| `docs/guide/index.md` | `redis` | 2 | v0.8 docs | User guides label legacy services as historical migration or explicit coordination opt-in. |
| `docs/guide/installation.md` | `falkor`, `postgres`, `redis`, `valkey` | 19 | v0.8 docs | User guides label legacy services as historical migration or explicit coordination opt-in. |
| `docs/guide/knowledge-graph.md` | `falkor`, `graphiti`, `redis` | 11 | v0.8 docs | User guides label legacy services as historical migration or explicit coordination opt-in. |
| `docs/guide/mcp-configuration.md` | `postgres`, `redis`, `valkey` | 7 | v0.8 docs | User guides label legacy services as historical migration or explicit coordination opt-in. |
| `docs/guide/migrating-from-falkor.md` | `falkor`, `postgres` | 16 | v0.8 docs | User guides label legacy services as historical migration or explicit coordination opt-in. |
| `docs/guide/quick-start.md` | `redis` | 3 | v0.8 docs | User guides label legacy services as historical migration or explicit coordination opt-in. |
| `docs/guide/semantic-search.md` | `redis` | 1 | v0.8 docs | User guides label legacy services as historical migration or explicit coordination opt-in. |
| `docs/guide/setting-up-prompts.md` | `redis`, `valkey` | 3 | v0.8 docs | User guides label legacy services as historical migration or explicit coordination opt-in. |
| `docs/guide/sources.md` | `redis` | 2 | v0.8 docs | User guides label legacy services as historical migration or explicit coordination opt-in. |
| `docs/guide/storage-modes.md` | `falkor`, `postgres`, `redis`, `valkey` | 14 | v0.8 docs | User guides label legacy services as historical migration or explicit coordination opt-in. |
| `docs/guide/surrealdb-migration-release-notes.md` | `falkor`, `graphiti`, `postgres` | 28 | v0.8 docs | User guides label legacy services as historical migration or explicit coordination opt-in. |
| `docs/guide/task-management.md` | `redis` | 1 | v0.8 docs | User guides label legacy services as historical migration or explicit coordination opt-in. |
| `docs/guide/why-surreal.md` | `falkor`, `graphiti`, `postgres`, `redis` | 17 | v0.8 docs | User guides label legacy services as historical migration or explicit coordination opt-in. |
| `docs/guide/working-with-agents.md` | `redis` | 6 | v0.8 docs | User guides label legacy services as historical migration or explicit coordination opt-in. |
| `docs/index.md` | `redis` | 1 | v0.8 docs | Top-level docs mention current Surreal default and historical migration context. |
| `docs/testing/benchmark-methodology.md` | `falkor`, `graphiti`, `postgres` | 8 | benchmark evidence | Benchmark comparison flow names historical migration rehearsal mode. |
| `infra/local/README.md` | `redis`, `valkey` | 11 | local Kubernetes/Tilt dev | Local Tilt and Helm dev keep Redis/Valkey as explicit coordination while Surreal owns data. |
| `infra/local/secrets.yaml.example` | `redis`, `valkey` | 2 | dev env templates | Environment templates keep legacy ports, migration knobs, and optional Redis/Valkey secrets. |
| `infra/local/sibyl-values.yaml` | `redis`, `valkey` | 4 | local Kubernetes/Tilt dev | Local Tilt and Helm dev keep Redis/Valkey as explicit coordination while Surreal owns data. |
| `infra/local/valkey-values.yaml` | `redis`, `valkey` | 4 | local Kubernetes/Tilt dev | Local Tilt and Helm dev keep Redis/Valkey as explicit coordination while Surreal owns data. |
| `moon.yml` | `graphiti` | 6 | v0.7 Graphiti exit | Root moon tasks retain the explicit Graphiti compatibility test island. |
| `packages/python/sibyl-core/COVERAGE_PLAN.md` | `falkor` | 3 | v0.7 Graphiti exit | Core package docs and tasks preserve compatibility coverage and historical Graphiti context. |
| `packages/python/sibyl-core/README.md` | `graphiti` | 1 | v0.7 Graphiti exit | Core package docs and tasks preserve compatibility coverage and historical Graphiti context. |
| `packages/python/sibyl-core/moon.yml` | `graphiti` | 9 | v0.7 Graphiti exit | Core package docs and tasks preserve compatibility coverage and historical Graphiti context. |
| `packages/python/sibyl-core/pyproject.toml` | `graphiti` | 2 | v0.7 Graphiti exit | Core package docs and tasks preserve compatibility coverage and historical Graphiti context. |
| `pyproject.toml` | `graphiti` | 4 | repo package config | Root package configs retain compatibility extras and dev dependency boundaries. |
| `setup-dev.sh` | `falkor`, `postgres` | 3 | dev bootstrap | Dev scripts mention legacy migration checks and optional Redis coordination. |
| `skills/sibyl/EXAMPLES.md` | `redis` | 3 | v0.8 skill docs | Source skill docs retain examples that mention Redis as historical troubleshooting context. |
| `skills/sibyl/SKILL.md` | `redis` | 4 | v0.8 skill docs | Source skill docs retain examples that mention Redis as historical troubleshooting context. |
| `tools/dev/run-surreal-dev.sh` | `falkor`, `postgres`, `redis` | 33 | dev bootstrap | Dev scripts mention legacy migration checks and optional Redis coordination. |

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
