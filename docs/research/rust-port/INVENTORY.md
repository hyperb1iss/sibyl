# Runtime Inventory

Generated from code by `tools/inventory/runtime_surface.py`. Do not hand-edit.

## Summary
- REST routers: 28
- Top-level HTTP routes: 2
- WebSocket routes: 1
- MCP tools: 11
- MCP resources: 2
- SQLModel tables: 0
- Raw SQL query usage files: 0
- Session-backed storage access files: 0
- Graphiti import files: 0
- Retained legacy term files: 95
- Dependency records: 3

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
- `ai_settings_router`
- `auth_router`
- `crawler_router`
- `orgs_router`
- `org_members_router`
- `org_invitations_router`
- `project_members_router`
- `invitations_router`
- `rag_router`
- `resolve_router`
- `jobs_router`
- `logs_router`
- `memory_router`
- `metrics_router`
- `settings_router`
- `synthesis_router`
- `telemetry_router`
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

## Retained Legacy Term Inventory

Every active doc or deployment config that mentions retired or optional legacy services
must carry an owner and reason here.

| File | Terms | Matches | Owner | Reason |
| ---- | ----- | ------- | ----- | ------ |
| `.env.example` | `falkor`, `graphiti`, `postgres`, `redis` | 17 | dev env templates | Environment templates keep legacy ports, migration knobs, and optional Redis/Valkey secrets. |
| `.env.quickstart.example` | `falkor`, `postgres` | 2 | dev env templates | Environment templates keep legacy ports, migration knobs, and optional Redis/Valkey secrets. |
| `.env.quickstart.test` | `falkor`, `postgres` | 2 | dev env templates | Environment templates keep legacy ports, migration knobs, and optional Redis/Valkey secrets. |
| `.env.test.example` | `falkor`, `postgres` | 10 | dev env templates | Environment templates keep legacy ports, migration knobs, and optional Redis/Valkey secrets. |
| `AGENTS.md` | `graphiti`, `redis`, `valkey` | 5 | project instructions | Project agent guides preserve ports, archive shapes, and compatibility boundaries. |
| `CLAUDE.md` | `graphiti`, `redis`, `valkey` | 5 | project instructions | Project agent guides preserve ports, archive shapes, and compatibility boundaries. |
| `README.md` | `graphiti`, `postgres`, `redis`, `valkey` | 14 | v0.8 pure Surreal closure | Default quickstart plus explicit legacy migration and optional Redis coordination notes. |
| `Tiltfile` | `redis`, `valkey` | 14 | local Kubernetes/Tilt dev | Local Tilt and Helm dev keep Redis/Valkey as explicit coordination while Surreal owns data. |
| `apps/api/README.md` | `postgres`, `redis` | 12 | v0.8 packaged docs | Packaged README and skill docs retain migration and optional coordination language. |
| `apps/api/examples/README.md` | `valkey` | 1 | v0.8 packaged docs | Packaged README and skill docs retain migration and optional coordination language. |
| `apps/api/pyproject.toml` | `redis` | 2 | v0.8 deployment config | Compose and chart files retain Redis as an explicit coordination profile or chart option. |
| `apps/cli/README.md` | `redis` | 1 | v0.8 packaged docs | Packaged README and skill docs retain migration and optional coordination language. |
| `apps/cli/src/sibyl_cli/data/skill-packs/core.md` | `falkor`, `postgres`, `redis` | 10 | v0.8 packaged docs | Packaged README and skill docs retain migration and optional coordination language. |
| `apps/cli/src/sibyl_cli/data/skill-packs/examples.md` | `redis` | 3 | v0.8 packaged docs | Packaged README and skill docs retain migration and optional coordination language. |
| `apps/cli/src/sibyl_cli/data/skill-packs/migration.md` | `falkor`, `graphiti`, `postgres`, `redis` | 75 | v0.8 packaged docs | Packaged README and skill docs retain migration and optional coordination language. |
| `charts/sibyl/Chart.yaml` | `valkey` | 1 | v0.8 deployment config | Compose and chart files retain Redis as an explicit coordination profile or chart option. |
| `charts/sibyl/templates/backend-deployment.yaml` | `redis` | 6 | v0.8 deployment config | Compose and chart files retain Redis as an explicit coordination profile or chart option. |
| `charts/sibyl/templates/bootstrap-job.yaml` | `redis` | 6 | v0.8 deployment config | Compose and chart files retain Redis as an explicit coordination profile or chart option. |
| `charts/sibyl/templates/configmap.yaml` | `redis` | 13 | v0.8 deployment config | Compose and chart files retain Redis as an explicit coordination profile or chart option. |
| `charts/sibyl/templates/networkpolicy.yaml` | `redis` | 6 | v0.8 deployment config | Compose and chart files retain Redis as an explicit coordination profile or chart option. |
| `charts/sibyl/templates/redis-secret.yaml` | `redis` | 8 | v0.8 deployment config | Compose and chart files retain Redis as an explicit coordination profile or chart option. |
| `charts/sibyl/templates/worker-deployment.yaml` | `redis` | 6 | v0.8 deployment config | Compose and chart files retain Redis as an explicit coordination profile or chart option. |
| `charts/sibyl/values.yaml` | `redis`, `valkey` | 20 | v0.8 deployment config | Compose and chart files retain Redis as an explicit coordination profile or chart option. |
| `docker-compose.prod.yml` | `falkor`, `redis`, `valkey` | 20 | v0.8 deployment config | Compose and chart files retain Redis as an explicit coordination profile or chart option. |
| `docker-compose.quickstart.yml` | `falkor`, `redis`, `valkey` | 20 | v0.8 deployment config | Compose and chart files retain Redis as an explicit coordination profile or chart option. |
| `docker-compose.yml` | `redis`, `valkey` | 11 | v0.8 deployment config | Compose and chart files retain Redis as an explicit coordination profile or chart option. |
| `docs/admin/installing.md` | `redis`, `valkey` | 8 | enterprise readiness | Enterprise docs mention legacy services only as migration context or optional coordination. |
| `docs/api/auth-authorization.md` | `postgres` | 4 | v0.8 API/CLI docs | API and CLI docs reference memory history, migration payloads, or optional coordination. |
| `docs/api/index.md` | `redis` | 1 | v0.8 API/CLI docs | API and CLI docs reference memory history, migration payloads, or optional coordination. |
| `docs/api/mcp-add.md` | `falkor`, `graphiti`, `postgres`, `redis` | 11 | v0.8 API/CLI docs | API and CLI docs reference memory history, migration payloads, or optional coordination. |
| `docs/api/mcp-explore.md` | `postgres` | 1 | v0.8 API/CLI docs | API and CLI docs reference memory history, migration payloads, or optional coordination. |
| `docs/api/mcp-reflect.md` | `postgres` | 2 | v0.8 API/CLI docs | API and CLI docs reference memory history, migration payloads, or optional coordination. |
| `docs/api/rest-memory.md` | `postgres` | 1 | v0.8 API/CLI docs | API and CLI docs reference memory history, migration payloads, or optional coordination. |
| `docs/api/rest-projects.md` | `postgres`, `redis` | 5 | v0.8 API/CLI docs | API and CLI docs reference memory history, migration payloads, or optional coordination. |
| `docs/api/rest-tasks.md` | `redis` | 1 | v0.8 API/CLI docs | API and CLI docs reference memory history, migration payloads, or optional coordination. |
| `docs/architecture/ENTERPRISE_READINESS_PLAN_2026-05-21.md` | `graphiti`, `valkey` | 5 | enterprise readiness | Enterprise docs mention legacy services only as migration context or optional coordination. |
| `docs/architecture/PACKAGING_PLAN_2026-05-19.md` | `falkor`, `postgres`, `redis`, `valkey` | 10 | v0.8 architecture | Architecture and release plans preserve migration, benchmark, and compatibility history. |
| `docs/architecture/PERMISSION_SYSTEM_AUDIT.md` | `falkor`, `postgres` | 23 | v0.8 architecture | Architecture and release plans preserve migration, benchmark, and compatibility history. |
| `docs/architecture/SIBYL_1_0_RC_PLAN.md` | `graphiti`, `redis` | 30 | v1.0 RC evidence freeze | Active RC plan names legacy terms only as blockers, evidence boundaries, and optional coordination risks. |
| `docs/architecture/SIBYL_1_0_RC_RELEASE_PACKET.md` | `falkor`, `graphiti`, `postgres`, `redis` | 7 | v1.0 RC evidence freeze | Active RC packet cites legacy terms only in release receipts and pending external checks. |
| `docs/architecture/SIBYL_1_0_ROADMAP.md` | `graphiti`, `redis` | 34 | v0.8 architecture | Architecture and release plans preserve migration, benchmark, and compatibility history. |
| `docs/architecture/SIBYL_NORTHSTAR.md` | `falkor`, `graphiti`, `postgres`, `redis` | 43 | v0.8 architecture | Architecture and release plans preserve migration, benchmark, and compatibility history. |
| `docs/architecture/SIBYL_POST_V010_RELEASE_REMAP_SPEC.md` | `graphiti`, `redis` | 37 | v0.8 architecture | Architecture and release plans preserve migration, benchmark, and compatibility history. |
| `docs/architecture/SIBYL_REFLECTION_OS_PLAN.md` | `graphiti` | 4 | v0.8 architecture | Architecture and release plans preserve migration, benchmark, and compatibility history. |
| `docs/architecture/TASKIQ_MIGRATION_PLAN.md` | `redis` | 57 | v0.8 architecture | Architecture and release plans preserve migration, benchmark, and compatibility history. |
| `docs/architecture/retrieval-system.md` | `graphiti` | 3 | v0.8 architecture | Architecture and release plans preserve migration, benchmark, and compatibility history. |
| `docs/cli/add.md` | `postgres`, `redis` | 3 | v0.8 API/CLI docs | API and CLI docs reference memory history, migration payloads, or optional coordination. |
| `docs/cli/entity.md` | `postgres`, `redis` | 3 | v0.8 API/CLI docs | API and CLI docs reference memory history, migration payloads, or optional coordination. |
| `docs/cli/index.md` | `redis` | 1 | v0.8 API/CLI docs | API and CLI docs reference memory history, migration payloads, or optional coordination. |
| `docs/cli/project.md` | `postgres` | 2 | v0.8 API/CLI docs | API and CLI docs reference memory history, migration payloads, or optional coordination. |
| `docs/cli/reflect.md` | `postgres` | 1 | v0.8 API/CLI docs | API and CLI docs reference memory history, migration payloads, or optional coordination. |
| `docs/cli/remember.md` | `postgres`, `redis` | 2 | v0.8 API/CLI docs | API and CLI docs reference memory history, migration payloads, or optional coordination. |
| `docs/cli/search.md` | `postgres` | 1 | v0.8 API/CLI docs | API and CLI docs reference memory history, migration payloads, or optional coordination. |
| `docs/cli/task-create.md` | `redis` | 2 | v0.8 API/CLI docs | API and CLI docs reference memory history, migration payloads, or optional coordination. |
| `docs/cli/task-lifecycle.md` | `postgres`, `redis` | 7 | v0.8 API/CLI docs | API and CLI docs reference memory history, migration payloads, or optional coordination. |
| `docs/deployment/docker-compose.md` | `postgres`, `redis`, `valkey` | 40 | v0.8 deployment docs | Deployment docs retain optional Redis/Valkey coordination and historical restore notes. |
| `docs/deployment/environment.md` | `graphiti`, `postgres`, `redis`, `valkey` | 56 | v0.8 deployment docs | Deployment docs retain optional Redis/Valkey coordination and historical restore notes. |
| `docs/deployment/helm-chart.md` | `postgres`, `redis`, `valkey` | 17 | v0.8 deployment docs | Deployment docs retain optional Redis/Valkey coordination and historical restore notes. |
| `docs/deployment/index.md` | `falkor`, `postgres`, `redis`, `valkey` | 14 | v0.8 deployment docs | Deployment docs retain optional Redis/Valkey coordination and historical restore notes. |
| `docs/deployment/kubernetes.md` | `falkor`, `postgres`, `redis`, `valkey` | 17 | v0.8 deployment docs | Deployment docs retain optional Redis/Valkey coordination and historical restore notes. |
| `docs/deployment/monitoring.md` | `redis` | 1 | v0.8 deployment docs | Deployment docs retain optional Redis/Valkey coordination and historical restore notes. |
| `docs/deployment/tilt-minikube.md` | `falkor`, `postgres`, `redis`, `valkey` | 19 | v0.8 deployment docs | Deployment docs retain optional Redis/Valkey coordination and historical restore notes. |
| `docs/deployment/troubleshooting.md` | `graphiti`, `postgres`, `redis` | 14 | v0.8 deployment docs | Deployment docs retain optional Redis/Valkey coordination and historical restore notes. |
| `docs/guide/capturing-knowledge.md` | `redis` | 10 | v0.8 docs | User guides label legacy services as historical migration or explicit coordination opt-in. |
| `docs/guide/claude-code.md` | `redis` | 2 | v0.8 docs | User guides label legacy services as historical migration or explicit coordination opt-in. |
| `docs/guide/entity-types.md` | `redis` | 1 | v0.8 docs | User guides label legacy services as historical migration or explicit coordination opt-in. |
| `docs/guide/index.md` | `redis` | 2 | v0.8 docs | User guides label legacy services as historical migration or explicit coordination opt-in. |
| `docs/guide/installation.md` | `falkor`, `postgres`, `redis`, `valkey` | 19 | v0.8 docs | User guides label legacy services as historical migration or explicit coordination opt-in. |
| `docs/guide/knowledge-graph.md` | `falkor`, `graphiti`, `redis` | 11 | v0.8 docs | User guides label legacy services as historical migration or explicit coordination opt-in. |
| `docs/guide/mcp-configuration.md` | `postgres`, `redis`, `valkey` | 7 | v0.8 docs | User guides label legacy services as historical migration or explicit coordination opt-in. |
| `docs/guide/memory-loop.md` | `redis` | 1 | v0.8 docs | User guides label legacy services as historical migration or explicit coordination opt-in. |
| `docs/guide/migrating-from-falkor.md` | `falkor`, `postgres` | 16 | v0.8 docs | User guides label legacy services as historical migration or explicit coordination opt-in. |
| `docs/guide/semantic-search.md` | `redis` | 1 | v0.8 docs | User guides label legacy services as historical migration or explicit coordination opt-in. |
| `docs/guide/setting-up-prompts.md` | `redis`, `valkey` | 3 | v0.8 docs | User guides label legacy services as historical migration or explicit coordination opt-in. |
| `docs/guide/sources.md` | `redis` | 2 | v0.8 docs | User guides label legacy services as historical migration or explicit coordination opt-in. |
| `docs/guide/storage-modes.md` | `falkor`, `postgres`, `redis`, `valkey` | 17 | v0.8 docs | User guides label legacy services as historical migration or explicit coordination opt-in. |
| `docs/guide/surrealdb-migration-release-notes.md` | `falkor`, `graphiti`, `postgres` | 29 | v0.8 docs | User guides label legacy services as historical migration or explicit coordination opt-in. |
| `docs/guide/task-management.md` | `redis` | 1 | v0.8 docs | User guides label legacy services as historical migration or explicit coordination opt-in. |
| `docs/guide/why-surreal.md` | `falkor`, `graphiti`, `postgres`, `redis` | 17 | v0.8 docs | User guides label legacy services as historical migration or explicit coordination opt-in. |
| `docs/guide/working-with-agents.md` | `redis` | 6 | v0.8 docs | User guides label legacy services as historical migration or explicit coordination opt-in. |
| `docs/index.md` | `redis` | 2 | v0.8 docs | Top-level docs mention current Surreal default and historical migration context. |
| `docs/testing/ai-memory-landscape.md` | `graphiti` | 5 | benchmark evidence | Competitive landscape docs name Graphiti only as historical comparison context. |
| `docs/testing/benchmark-methodology.md` | `falkor`, `graphiti`, `postgres` | 8 | benchmark evidence | Benchmark comparison flow names historical migration rehearsal mode. |
| `docs/users/sharing-memory.md` | `postgres` | 2 | enterprise readiness | Enterprise docs mention legacy services only as migration context or optional coordination. |
| `infra/local/README.md` | `redis`, `valkey` | 12 | local Kubernetes/Tilt dev | Local Tilt and Helm dev keep Redis/Valkey as explicit coordination while Surreal owns data. |
| `infra/local/secrets.yaml.example` | `redis`, `valkey` | 2 | dev env templates | Environment templates keep legacy ports, migration knobs, and optional Redis/Valkey secrets. |
| `infra/local/sibyl-values.yaml` | `redis`, `valkey` | 4 | local Kubernetes/Tilt dev | Local Tilt and Helm dev keep Redis/Valkey as explicit coordination while Surreal owns data. |
| `infra/local/valkey-values.yaml` | `redis`, `valkey` | 4 | local Kubernetes/Tilt dev | Local Tilt and Helm dev keep Redis/Valkey as explicit coordination while Surreal owns data. |
| `moon.yml` | `graphiti` | 3 | inventory task inputs | Root moon tasks reference the Graphiti exit archive filename as inventory input. |
| `packages/python/sibyl-core/COVERAGE_PLAN.md` | `falkor` | 3 | v0.7 Graphiti exit | Core package docs and tasks preserve compatibility coverage and historical Graphiti context. |
| `packages/python/sibyl-core/README.md` | `graphiti` | 3 | v0.7 Graphiti exit | Core package docs and tasks preserve compatibility coverage and historical Graphiti context. |
| `packages/python/sibyl-core/moon.yml` | `graphiti` | 2 | v0.7 Graphiti exit | Core package docs and tasks preserve compatibility coverage and historical Graphiti context. |
| `setup-dev.sh` | `falkor`, `postgres` | 4 | dev bootstrap | Dev scripts mention legacy migration checks and optional Redis coordination. |
| `skills/agent-activity-audit/EXAMPLES.md` | `falkor` | 1 | v0.8 skill docs | Source skill docs retain examples that mention Redis as historical troubleshooting context. |
| `tools/dev/run-surreal-dev.sh` | `falkor`, `postgres`, `redis` | 33 | dev bootstrap | Dev scripts mention legacy migration checks and optional Redis coordination. |

## Dependency Inventory

### Legacy and transition dependencies
- none

### Graph runtime dependencies
- none

### Target SurrealDB dependencies
| Project | Scope | Dependency |
| ------- | ----- | ---------- |
| `apps/api/pyproject.toml` | `default` | `surrealdb>=2.0.0,<3.0` |
| `packages/python/sibyl-core/pyproject.toml` | `optional:graph` | `surrealdb>=2.0.0,<3.0` |
| `packages/python/sibyl-core/pyproject.toml` | `optional:runtime` | `surrealdb>=2.0.0,<3.0` |
