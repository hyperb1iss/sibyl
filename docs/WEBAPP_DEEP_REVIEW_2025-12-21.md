# Sibyl Webapp Deep Review (2025-12-21)

## Scope

Reviewed the Sibyl repository with emphasis on the Next.js web UI in `web/`, and its integration contract with the Python backend in `src/sibyl/`.

Work performed:
- Read core docs (`README.md`, `docs/*`, `web/README.md`)
- Audited routes/components/hooks under `web/src/`
- Verified backend REST surface in `src/sibyl/api/*`
- Ran local validation:
  - `pnpm -C web build` (passes)
  - `pnpm -C web lint` (fails with many format/import-order diagnostics)
  - `uv run pytest -q --tb=no` (21 failed, 328 passed, 1 error; see “Backend findings affecting the web UI”)

## Executive Summary

The web UI has a solid base architecture (Next.js App Router + React Query hooks + a small API client), and the UI/UX direction is cohesive. However, there are multiple **P0 integration gaps** that make some key UI features non-functional against the current backend:

- The web UI calls a **REST `/manage` endpoint that does not exist** in the FastAPI app → “Crawl Now” for sources will 404; any future workflow actions via `manage` will also fail.
- Realtime updates are implemented via WebSocket, but the client connects to **`/ws` on the Next origin** and relies on a rewrite/proxy that **likely won’t work for WebSockets**, and the client has a **disconnect/reconnect bug**.
- UI navigation includes links to **non-existent pages** (e.g. `/sources/[id]`).
- Some “implemented” UI controls are currently **not wired** (graph type filters, “Created Since” search filter).

Most other issues are **interface drift** (types vs OpenAPI, entity type lists missing new types, status taxonomy mismatches) and **maintainability** (dependency drift, lint failures, a few copy/paste artifacts).

## Architecture Review

### Current architecture (as implemented)

**Backend**
- Combined Starlette app mounts:
  - REST API at `/api/*` via FastAPI (`src/sibyl/api/app.py`)
  - MCP transport at `/` (`src/sibyl/main.py`)
- REST routers:
  - `/entities` CRUD (`src/sibyl/api/routes/entities.py`)
  - `/search` and `/search/explore` (`src/sibyl/api/routes/search.py`)
  - `/graph/*` visualization endpoints (`src/sibyl/api/routes/graph.py`)
  - `/admin/*` health/stats/ingest/websocket status (`src/sibyl/api/routes/admin.py`)
- WebSocket endpoint is mounted inside the API app at `/ws`, which becomes `/api/ws` on the combined app (`src/sibyl/api/app.py`, `src/sibyl/main.py`).

**Web UI**
- Next.js 16 App Router app in `web/src/app/*`
- Data fetching:
  - Server-side fetch helpers in `web/src/lib/api-server.ts` (calls backend directly via `SIBYL_API_URL`, defaults `http://localhost:3334/api`)
  - Client-side React Query hooks in `web/src/lib/hooks.ts`, using `fetch` wrapper in `web/src/lib/api.ts` (defaults to relative `/api/*`)
- Realtime:
  - `web/src/lib/websocket.ts` uses browser WebSocket to connect to `ws(s)://<next-origin>/ws` and drives invalidation in `useRealtimeUpdates()`.
- Next dev/prod proxying:
  - `web/next.config.ts` rewrites `/api/:path*` → `http://localhost:3334/api/:path*`
  - and `/ws` → `http://localhost:3334/api/ws`

### What’s working well

- Clear separation between `api.ts` (transport) and `hooks.ts` (query/mutation orchestration).
- Thoughtful UX structure: pages are compositional and consistently use `PageHeader`, “EmptyState/ErrorState”, and skeletons.
- Graph rendering pipeline is reasonably safe (dedupe nodes/edges, clamp sizes) (`web/src/components/graph/knowledge-graph.tsx`).
- Build is currently green for the web app (`pnpm -C web build`).

## P0 / Critical issues (user-visible breakage)

### 1) Web UI calls a REST `/manage` endpoint that does not exist

- Web calls:
  - `web/src/lib/api.ts` → `api.tasks.manage()` posts to `/manage`
  - `web/src/lib/api.ts` → `api.sources.crawl()` posts to `/manage`
  - `web/src/lib/hooks.ts` → `useCrawlSource()` uses `api.sources.crawl()`
- Backend REST routers do **not** include `/manage`:
  - `src/sibyl/api/app.py` includes only `/entities`, `/search`, `/graph`, `/admin`.

Impact:
- “Crawl Now” in Sources UI will 404.
- Any plan to drive workflow via `manage()` from the web will also 404.

Recommendation:
- Decide the integration contract:
  1) Add a REST `/manage` route that dispatches to the canonical manage implementation (preferred for the web UI); or
  2) Remove `/manage` from the web UI and provide explicit REST endpoints (`/tasks/{id}/start`, `/sources/{id}/crawl`, etc.).

### 2) WebSocket path/proxying is likely broken; disconnect triggers reconnect

- Backend WebSocket: `/api/ws`
- Web client connects to `/ws` on Next origin (`web/src/lib/websocket.ts`) and depends on rewrites in `web/next.config.ts`.

Problems:
- Next rewrites are HTTP-layer; WebSocket proxying is not reliably supported in typical Next dev/prod setups.
- `WebSocketClient.disconnect()` closes the socket, but `onclose` always triggers `attemptReconnect()` → you can’t intentionally disconnect (and cleanup during hot reload can cause reconnection churn).
- `connect()` only guards against `OPEN`; repeated calls while `CONNECTING` can create multiple sockets.

Impact:
- Realtime invalidation is likely not working, so the UI depends on manual refresh/polling.
- Connection status shown in the header can be misleading.

Recommendation:
- Connect directly to the backend WebSocket URL (configurable via env), e.g. `ws://localhost:3334/api/ws`, and remove `/ws` rewrite.
- Add a `shouldReconnect` flag to prevent reconnects after manual disconnect.
- Guard against `CONNECTING` as well as `OPEN`.

### 3) Broken navigation: source detail page is linked but not implemented

- `web/src/components/sources/source-card.tsx` links to `/sources/${source.id}`
- There is **no** `web/src/app/sources/[id]/page.tsx` route.

Impact:
- Users hit a 404 page from the Sources list.

Recommendation:
- Either implement `/sources/[id]` (source details + document list) or remove/disable the “View” link.

### 4) UI controls exist but don’t affect behavior

- Graph type filter chips exist (`web/src/app/graph/page.tsx`), but selected filters are never passed into `useGraphData()` (`web/src/components/graph/knowledge-graph.tsx`) → filters are a no-op.
- Entity detail links to “View in Graph” with `?selected=<id>` (`web/src/app/entities/[id]/entity-detail-content.tsx`), but `web/src/app/graph/page.tsx` does not read `searchParams` and therefore never auto-selects/highlights the node.
- Search “Created Since” UI is present (`web/src/app/search/search-content.tsx`) but does not affect the request or filtering.

Impact:
- Users get misleading UX (controls that don’t work).

Recommendation:
- Either wire these features end-to-end or remove them until implemented.

## P1 / High issues (correctness, drift, reproducibility)

### 1) Dependency drift in `web/package.json`

- `lucide-react` is imported widely (layout, header, sidebar, breadcrumb, etc.) but is **not declared** in `web/package.json`. It happens to be present in `node_modules`, but clean installs may fail.
- `nuqs` and `zod` are declared but appear unused in `web/src/`.

Recommendation:
- Add missing runtime deps (`lucide-react`) and remove unused deps or start using them (e.g. `zod` for runtime validation).

### 2) API typing drift: handcrafted TS types vs OpenAPI

- The backend has a well-defined OpenAPI (`/api/openapi.json`).
- The web app still maintains handwritten interfaces in `web/src/lib/api.ts`, and some are already drifted:
  - `ExploreRequest` supports `dependencies`, `project`, `status` in backend (`src/sibyl/api/schemas.py`) but web types don’t fully reflect this.

Recommendation:
- Use the existing `openapi-typescript` script to generate types and replace the handwritten types (or at least make them thin wrappers).
- Consider pairing generated types with runtime validation (e.g. `zod` schemas).

### 3) Hardcoded backend URL in Next rewrites

- `web/next.config.ts` routes `/api/*` to `http://localhost:3334/api/*` unconditionally.
- `web/README.md` suggests `NEXT_PUBLIC_API_URL`, but code uses `SIBYL_API_URL` (server-side only) and hardcoded rewrites (client-side).

Recommendation:
- Make backend base URL environment-driven for both server and client (and avoid hardcoding localhost).
- If you need same-origin in production, deploy behind a reverse proxy that routes `/api` and `/ws` to the backend.

### 4) Archived tasks handling is inconsistent

- TS type `TaskStatus` includes `'archived'` (`web/src/lib/api.ts`).
- UI status columns omit archived (`TASK_STATUSES` in `web/src/lib/constants.ts`), so archived tasks can become invisible (depending on backend behavior).

Recommendation:
- Either include an “Archived” column/filter or ensure archived tasks are excluded at the query level.

### 5) Copy/paste artifacts / minor correctness smells

- Duplicate `onSuccess` key in `useTaskUpdateStatus` (`web/src/lib/hooks.ts`) is a merge artifact (harmless but indicates quality drift).
- `CommandPalette` selection-reset effect comment doesn’t match implementation: selection is not reset when query changes (`web/src/components/ui/command-palette.tsx`).
- Several components perform `await mutateAsync(...)` without `try/catch`, which can throw and break UI state (e.g. `EntityDetailContent.handleSave()` in `web/src/app/entities/[id]/entity-detail-content.tsx`).

## P2 / Medium issues (security, UX polish, performance)

### Security / access control (system-level)

There is no auth on the backend REST surface. The web UI exposes destructive actions (delete entity, ingest arbitrary paths, create crawl sources).

Risk:
- Anyone who can reach the service can mutate/delete data or trigger ingestion/crawling.

Recommendation:
- Add authN/authZ or network-layer protection at minimum.
- Add SSRF protections for crawling sources (allowlist domains, block private IP ranges) and constrain ingestion paths.

### Performance

- Graph view fetches up to 500 nodes / 1000 edges by default; for real graphs, this will become heavy. Consider:
  - Server-side aggregation or sampling
  - Progressive loading (expand from a selected node)
  - Caching / memoization on the backend for graph payloads

### Maintainability

- `pnpm -C web lint` reports many diagnostics (format/import sorting and a `noImportantStyles` warning for the reduced-motion accessibility block in CSS).

Recommendation:
- Run `biome check --write` and enforce lint/format in CI for `web/`.
- If `!important` is intentional for reduced-motion overrides, disable that rule for that file/section.

## Backend findings affecting the web UI

The web UI is relatively thin and depends on the backend data model being stable. A fresh test run in this environment surfaced systemic backend issues that will impact the UI once deeper task/project/source behaviors are relied upon:

- `uv run pytest -q --tb=no` → **21 failed, 328 passed, 1 error**.
- Major causes observed:
  - Task model requires `project_id` (`src/sibyl/models/tasks.py`), but multiple tests (and the web UI task creation flow) assume tasks can exist without a project.
  - Community detection/summarization tests assume optional deps (`networkx`, etc.) are installed; in this environment they were not.
  - Integration tests show schema/storage mismatch for Pattern `category` (`tests/test_graph_integration.py` expects it in `metadata`, but fetched entity returned empty `metadata`).

Recommendation:
- Clarify and enforce the canonical task/project/source schema (which fields are first-class vs in `metadata`), and align:
  - Pydantic models
  - Graph storage
  - REST schemas/OpenAPI
  - Web UI assumptions

## Recommended roadmap (high-level)

**Immediate (P0)**
1) Decide and implement the REST contract for `manage` actions and update the web UI accordingly.
2) Fix WebSocket connectivity and reconnection semantics (or disable realtime until it is reliable).
3) Fix/implement `/sources/[id]` route and graph “selected node” deep-linking.

**Short term (P1)**
1) Switch web typings to OpenAPI-generated types; remove drift.
2) Fix `web/package.json` dependency declarations; remove unused deps.
3) Add archived-task UX or query-level filtering.

**Medium term (P2)**
1) Add auth and harden ingestion/crawl endpoints.
2) Improve graph scaling strategy (subgraph-first, progressive expansion).

