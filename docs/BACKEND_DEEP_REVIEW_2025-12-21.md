# Sibyl Backend Deep Code Review (2025-12-21)

## Scope
Reviewed the Python backend under `src/sibyl/`:
- Server entrypoint + mounting (`src/sibyl/main.py`, `src/sibyl/server.py`)
- REST API (`src/sibyl/api/*`)
- MCP tool layer (`src/sibyl/tools/*`)
- Graph layer (`src/sibyl/graph/*`)
- Task workflow + analysis (`src/sibyl/tasks/*`)
- Crawling + Postgres RAG (`src/sibyl/crawler/*`, `src/sibyl/db/*`, `src/sibyl/api/routes/crawler.py`, `src/sibyl/api/routes/rag.py`, `src/sibyl/jobs/*`)
- Repo ingestion (`src/sibyl/ingestion/*`)
- Retrieval (`src/sibyl/retrieval/*`)
- Tests + CI configuration (`tests/*`, `.github/workflows/ci.yml`, `pyproject.toml`)

This report intentionally focuses on issues, duplication, architecture/design risks, API contract problems, and missing features/gaps.

## Executive Summary
Sibyl is mid-transition into a “two-systems backend”:
- **Knowledge graph** in **FalkorDB** via **Graphiti** (entities, relationships, tasks/projects).
- **Documentation store** in **Postgres + pgvector** (crawl sources, documents, chunks, embeddings) with optional **Graph-RAG linking**.

The biggest problems are:
1) **Contract drift and duplication** across MCP tools, REST endpoints, CLI, and internal tool implementations.
2) **Security posture is “dev-only”**: no auth, multiple SSRF/file ingestion surfaces, and error details are returned to clients.
3) **Quality/tooling gaps**: tests + CI are currently inconsistent with the codebase direction; lint/typecheck are not consistently runnable; tests leak resources and appear to make live network calls.
4) **Lifecycle/boot order**: the current Starlette “combined app” lifespan likely bypasses FastAPI lifespan (and therefore bypasses any startup DB init/prewarm you might add later).

If you want this to be reliable for both MCP clients and the web UI, the core roadmap is:
**pick a canonical API + canonical domain model**, remove duplicates, lock down the service, and make tests deterministic/offline.

## Architecture Overview (Current)

### Runtime entrypoints
- `sibyl-serve` → `src/sibyl/main.py`: mounts `/api` (FastAPI) and `/` (FastMCP streamable HTTP app).
- MCP server/tool registration in `src/sibyl/server.py` (4-tool surface: `search`, `explore`, `add`, `manage`).

### Data stores
- **FalkorDB (Redis protocol)**: Graphiti nodes/edges; used by graph entities, tasks, projects.
- **Postgres + pgvector**: `CrawlSource`, `CrawledDocument`, `DocumentChunk` for crawler/RAG.
- **Redis jobs DB** (same FalkorDB instance, separate DB number): arq job queue (`src/sibyl/jobs/*`).
- **In-memory background queue**: `src/sibyl/background.py` (asyncio queue) for “enrich_entity” etc.

### Two ingestion pipelines (separate)
- Repo “wisdom ingestion” → graph (entities/relationships): `src/sibyl/ingestion/*` via `src/sibyl/tools/admin.py::sync_wisdom_docs`.
- Web crawl ingestion → Postgres chunks + embeddings (+ optional graph linking): `src/sibyl/crawler/*` invoked by `/api/sources/*` and arq worker.

## REST Surface (as mounted at `/api`)
- Entities CRUD: `GET/POST/PATCH/DELETE /entities` (`src/sibyl/api/routes/entities.py`)
- Search: `POST /search`, `POST /search/explore` (`src/sibyl/api/routes/search.py`)
- Task workflow only (no list/create): `POST /tasks/{id}/start|block|unblock|review|complete|archive`, `PATCH /tasks/{id}` (`src/sibyl/api/routes/tasks.py`)
- Graph visualization: `/graph/*` (`src/sibyl/api/routes/graph.py`)
- Admin health/stats/ingest: `/admin/*` (`src/sibyl/api/routes/admin.py`)
- Crawl sources (Postgres): `/sources/*` (`src/sibyl/api/routes/crawler.py`)
- RAG endpoints: `/rag/*` (`src/sibyl/api/routes/rag.py`)
- Jobs: `/jobs/*` (`src/sibyl/api/routes/jobs.py`)
- WebSocket: `/ws` (mounted as `/api/ws` when combined) (`src/sibyl/api/app.py`, `src/sibyl/api/websocket.py`)

## MCP Surface (FastMCP)
- Registered in `src/sibyl/server.py`:
  - `search(...)` → `src/sibyl/tools/core.py::search`
  - `explore(...)` → `src/sibyl/tools/core.py::explore`
  - `add(...)` → `src/sibyl/tools/core.py::add`
  - `manage(action, entity_id, data)` → `src/sibyl/tools/manage.py::manage`

## P0 / Critical Issues

### P0.1 No authentication/authorization anywhere
There is no auth on the REST surface or WebSocket. Destructive operations exist:
- Delete entities (`DELETE /api/entities/{id}`)
- Delete crawl sources + data (`DELETE /api/sources/{id}`)
- Trigger background ingestion of local paths (`POST /api/admin/ingest`)

If the service is reachable on a network, it is effectively “open admin”.

**Recommendation**
- Add authN/authZ (even a single shared token header is better than none).
- Add environment-based “unsafe admin endpoints disabled in production” gates.

### P0.2 SSRF + internal-network access via URL preview and crawling
`GET /api/sources/preview?url=...` fetches arbitrary URLs with redirects (`src/sibyl/api/routes/crawler.py`), and the crawl ingestion can fetch arbitrary external sites.

**Risks**
- SSRF to internal services.
- Accidental crawling of private endpoints.

**Recommendation**
- Enforce allowlists (domain allowlist and/or CIDR blocklist for private/reserved ranges).
- Restrict schemes to `https` (and maybe `http` only in explicit dev mode).
- Add request timeouts and max download sizes everywhere.

### P0.3 Arbitrary filesystem ingestion surface
`POST /api/admin/ingest` accepts a free-form `path` and forwards to `sync_wisdom_docs(path=...)`.

**Risk**
- Remote user can cause the server to read and ingest arbitrary files (data exfiltration and/or CPU burn).

**Recommendation**
- Remove `path` from the public API, or restrict it to a safe subdirectory rooted at `settings.conventions_repo_path`.
- Consider disabling `/admin/ingest` entirely in non-dev deployments.

### P0.4 Cypher injection surface (LLM-controlled string interpolation)
`src/sibyl/crawler/graph_integration.py` builds Cypher with:
`query += f" AND n.entity_type = '{entity_type}'"` where `entity_type` comes from the LLM extractor output.

**Risk**
- A malicious/unexpected model output can alter the query.

**Recommendation**
- Never interpolate untrusted strings into Cypher; use parameters and validate against a strict enum.

### P0.5 Core contract drift and duplicated implementations
There are parallel/overlapping implementations for the same concepts:
- `manage()` exists as:
  - “canonical” dispatcher: `src/sibyl/tools/manage.py` (action + `data` dict; includes crawl/admin/analysis)
  - a separate typed/task-only manage: `src/sibyl/tools/core.py::manage` (task workflow only)
- Sources exist as:
  - graph entity model: `src/sibyl/models/sources.py::Source` (used by `tools/manage.py` crawl stubs)
  - Postgres DB model: `src/sibyl/db/models.py::CrawlSource` (used by `/api/sources/*` and jobs worker)

**Impact**
- MCP/REST/CLI/web integrations will “call the wrong thing” and silently lose features.
- The system appears to have two “source systems” with different IDs and behavior.

**Recommendation**
- Choose one canonical “manage” and delete/deprecate the other.
- Choose one canonical “Source/Document” model; if Postgres is the canonical doc store, remove the graph `Source/Document` CRUD stubs or make them a projection of Postgres state.

## P1 / High Issues

### P1.1 Combined app lifespan likely bypasses FastAPI lifespan
`src/sibyl/main.py` creates a Starlette app with its own lifespan and mounts the FastAPI app at `/api`.
In Starlette, mounted apps generally do **not** receive lifespan events from the parent.

**Impact**
- `src/sibyl/api/app.py::lifespan` (graph pre-warm) likely never runs under the combined app.
- If you later add Postgres init/migrations to the FastAPI lifespan, it may also never run.

**Recommendation**
- Move required startup/shutdown work into the combined app lifespan, or explicitly manage sub-app startup.

### P1.2 REST API gaps vs UI needs (and schema mismatches)
Notable gaps/mismatches:
- No `GET /tasks` or `POST /tasks` (list/create), so clients are pushed into `/search` for task listing.
- `SearchRequest.query` requires `min_length=1` (`src/sibyl/api/schemas.py`), but task listing often needs an empty query with filters.
- `POST /entities` defaults content to empty (schema), but `tools.core.add` rejects empty content → 400s for naive create flows.

**Recommendation**
- Add first-class tasks/projects REST endpoints or relax search schema to allow empty query when filters are present.
- Make entity/task creation schemas explicit (don’t overload “task fields inside metadata”).

### P1.3 Graph query correctness smells
Several Cypher queries use `RELATIONSHIP` labels/properties and/or reference variables that aren’t defined.
Example: `src/sibyl/tasks/dependencies.py` uses `relationships(path)` without binding `path`.

**Impact**
- “Dependencies mode” and transitive dependency traversal are likely broken beyond the simplest case.

**Recommendation**
- Centralize graph query helpers and add integration tests that assert these endpoints work against a seeded graph.

### P1.4 Logging and error handling are “developer leaking”
Patterns observed:
- Many endpoints return `detail=str(e)` on 500 errors (leaks internal error messages).
- `structlog` is configured with `format_exc_info` + Console renderer; lots of `log.exception(...)` calls exist (and pytest treats warnings as errors).

**Recommendation**
- Standardize error responses (safe public message + internal log).
- Decide whether you want `log.exception` everywhere or “log.error without traceback” in hot paths; align with the `pytest` warning policy.

## P2 / Medium Issues

### P2.1 Performance: entity listing filters in Python after over-fetching
`GET /api/entities` pulls up to ~500 per entity type and filters in Python (`src/sibyl/api/routes/entities.py`).
This will not scale as the graph grows.

**Recommendation**
- Push filters down into the graph query (type/language/category/project/status).
- Add pagination at the data-store level instead of list-then-slice.

### P2.2 Postgres schema rigidity and drift risks
`DocumentChunk.embedding` is declared as `Vector(1536)` (`src/sibyl/db/models.py`) while embeddings are configurable via settings.

**Recommendation**
- Make embedding dims a hard invariant (remove config), or migrate schema per model change.

### P2.3 “Enhanced retrieval” produces dict-shaped entities
Hybrid traversal (`src/sibyl/retrieval/hybrid.py`) returns dict records for graph traversal results, while formatting paths expect rich entity objects.

**Recommendation**
- Normalize to one “entity shape” at the retrieval boundary (object model or standardized dict).

## P3 / Low Issues / Cleanup
- Duplicate/unused imports and style issues (`uv run ruff check src` reports errors; see “Quality Signals”).
- Debug endpoints (`/api/graph/debug`) should be gated/removed in production.
- Mixed “Conventions” naming persists in docstrings/errors (`ConventionsMCPError` etc.), which can confuse new contributors.

## Duplicated Code Inventory (High value refactors)
- **Manage**: `src/sibyl/tools/manage.py` vs `src/sibyl/tools/core.py::manage`
- **Health/stats**: `src/sibyl/tools/admin.py` vs `src/sibyl/tools/core.py::{get_health,get_stats}` vs `/api/admin/*`
- **Sources/documents**: `src/sibyl/models/sources.py` vs `src/sibyl/db/models.py` vs `/api/sources/*` vs `tools/manage.py` crawl stubs
- **Ingestion**: `src/sibyl/ingestion/*` vs `src/sibyl/crawler/*` (two pipelines, different outputs)
- **Serialization**: multiple “dataclass to dict” conversions (server `_to_dict`, REST route local conversions)

## Quality Signals (Tests, Lint, CI)

### Pytest
- Full suite currently **fails collection** due to an outdated import expectation:
  - `tests/test_tasks_workflow.py` imports `VALID_TRANSITIONS` from `sibyl.tasks.workflow` but the module no longer defines it.
- Running with `--ignore=tests/test_tasks_workflow.py`:
  - Results observed: **4 failed, 406 passed, 2 errors** (the “errors” appear tied to unraisable resource warnings and are not stable when isolated).
- Entity extraction tests are out of sync with implementation:
  - `EntityExtractor` in `src/sibyl/crawler/graph_integration.py` now uses **Anthropic** (`client.messages.create`) but tests mock **OpenAI** (`chat.completions.create`).
- Test runs emit multiple **unclosed socket/transport/asyncpg connection** warnings at shutdown.
  - This suggests engine/pool and/or HTTP clients are not being properly closed in tests.

### Ruff
`uv run ruff check src` reports multiple issues (imports, error-handling style, ambiguous unicode chars in regex, unused vars/imports).

### CI workflow issue
`.github/workflows/ci.yml` runs `uv run mypy src`, but `mypy` is not present in the declared dependency groups (only `pyright` is listed under `[dependency-groups].dev`).

**Recommendation**
- Either add `mypy` to dependencies, or switch CI to `pyright`, or adjust `uv sync` to include the correct group/tooling.

## Recommended Roadmap (Practical)

### Phase 0 (Security + Contract Lockdown)
1) Add auth (even a simple shared token).
2) Add SSRF protections for `/sources/preview` and crawling.
3) Restrict or remove `/admin/ingest` path parameter.
4) Stop returning raw exception strings in HTTP 500 responses.

### Phase 1 (Coherence: one model, one interface)
1) Decide the canonical “source/document” system (Postgres is already the functional path). Remove/bridge the graph `Source` stubs.
2) Make one canonical `manage()` API and route all callers through it.
3) Align MCP tool wrappers (`src/sibyl/server.py`) with `tools.core` capabilities (filters/modes) and align REST schemas accordingly.

### Phase 2 (Reliability and Scale)
1) Fix lifespan handling so startup tasks run deterministically (especially DB init/migrations).
2) Fix graph query correctness for dependency traversal and hybrid traversal.
3) Push entity listing filters/pagination into the store; avoid over-fetch.

### Phase 3 (Tooling and Developer Experience)
1) Make tests deterministic/offline (mock LLM calls and crawling; close engines/clients).
2) Repair CI typecheck step (either ship mypy or remove it).
3) Enforce ruff formatting/lint in CI and keep the codebase clean during refactors.

