# apps/api tech-debt audit (sibyld server daemon)

Repo: `/Users/bliss/dev/sibyl` @ `5388d986` (main, clean). Lane: `apps/api` only.
Everything below was read in the tree as it stands today. Line numbers are from the
current working copy. Findings the 2026-05-28 audit already fixed are not re-listed.

Scale of the surface: 390 Python files, ~151k lines including tests. 31 routers, 185
REST routes, 204 route handlers and 332 module-private helpers inside
`src/sibyl/api/routes/`.

---

## Severity index

| # | Finding | Severity | Fix |
|---|---|---|---|
| 1 | Broker/scheduler/lock startup failures swallowed; readiness reports ready | blocker | M |
| 2 | `GET /backups/jobs/{job_id}` is cross-org readable | major | S |
| 3 | Idempotency lock scope defaults to the literal string `"unknown"` | major | S |
| 4 | Rate limiting is inert outside 6 auth routes; tests assert the config dict | major | S |
| 5 | `X-Request-ID` validation bypassed by middleware ordering | major | S |
| 6 | Setup-mode auth bypass gates instance-wide settings on data presence | major | M |
| 7 | No test asserts every mounted route carries an auth dependency | major | S |
| 8 | API-key REST scope check depends on unpinned Starlette `Mount` behavior | major | S |
| 9 | 6.5k lines of domain logic living in the route package | major | L |
| 10 | Hand-rolled SurrealQL lexer as a security boundary in a route file | major | M |
| 11 | Config lives in `os.environ`, mutated at startup and by route handlers | major | M |
| 12 | The FastAPI app's `lifespan` never runs in the deployed process | major | S |
| 13 | `get_current_organization` returns an org without checking membership | major | M |
| 14 | Errors swallowed into 200 responses (5 sites) | minor | S |
| 15 | Raw exception strings returned in 200 bodies, bypassing sanitization | minor | S |
| 16 | Three auth-dependency families across two architectural layers | minor | M |
| 17 | Two WebSocket endpoints, two hand-written auth paths, one via query param | minor | M |
| 18 | WebSocket broadcast is serial; one slow client stalls the org | minor | S |
| 19 | `is_setup_mode()` / readiness open an unpooled Surreal client per call | minor | S |
| 20 | Duplicated config validators; dead `fully_surreal`; dead `auth/rls.py` | minor | S |
| 21 | `enqueue_crawl`/`enqueue_sync` take an optional `organization_id` | minor | S |
| 22 | 2.5k-line migration/cutover CLI ships in the daemon wheel | minor | M |
| 23 | `disable_auth` is a half-implemented mode, resolved at import time | minor | S |
| 24 | `SessionMiddleware` reuses the JWT signing secret | minor | S |
| 25 | CORS allows `localhost:3337` in production | minor | S |
| 26 | Six `BaseHTTPMiddleware` layers on every request | minor | M |
| M1 | `api:read`/`api:write` never enforced on the MCP surface | blocker | M |
| M2 | MCP accepts a client-supplied project as an authorization input | major | S |
| M3 | MCP writes carry no org-role gate; VIEWER can write | major | M |
| M4 | MCP re-implements REST handlers, with measured divergence | major | L |
| M5 | Third verbatim copy of the REST scope check in the persistence layer | major | S |
| M6 | mcp 2.0 migration surface (assessment) | n/a | M |
| M7 | Conditional `user_id` overwrite; unpoliced `manage` actions | minor | S |
| J1 | `coordination_backend="auto"` never selects Redis; default queue is in-memory | blocker | M |
| J2 | Delivery semantics differ by backend; no `max_tries`, no local timeout | major | M |
| J3 | A failed local job blocks its own retry for 24 hours | major | S |
| J4 | `run_backup` reports failure as a successful job | major | S |
| J5 | The local scheduler double-fires with more than one process | major | M |
| J6 | Local backend breaks five coordination guarantees across processes | major | M |
| J7 | Five jobs run without an org scope | major | S |
| J8 | Shutdown drains the backlog, then kills running work | minor | S |
| J9 | Failure-looks-like-success is systemic across job bodies | major | M |

---

## 1. Startup failures are swallowed and readiness cannot see them (blocker, M)

`src/sibyl/runtime_services.py:52-114`. Each of `_startup_broker`, `_startup_scheduler`,
`_startup_pubsub`, and `_startup_locks` wraps its work in `try/except Exception` and
downgrades any failure to a `log.warning`, then continues. `startup()` returns normally.

`src/sibyl/api/readiness.py:102-111` probes SurrealDB reachability and schema bootstrap,
nothing else. `src/sibyl/api/app.py:339-354` `/health` returns `"healthy"`
unconditionally.

So a process that failed to connect its job broker, failed to start the scheduler, and
failed to initialize distributed locks passes both probes and takes production traffic.
Every enqueue against that process, every cron-scheduled backup, and every idempotency
lock acquisition then fails or silently no-ops behind a warning line that nobody is
paging on. This is the single highest-leverage finding in the lane: it converts a
recoverable startup fault into silent data loss.

Fix: track which subsystems are required, record their status on the `RuntimeServices`
instance (the flags already exist: `_broker_initialized`, `_scheduler_initialized`,
`_locks_initialized`), and surface them as `DependencyStatus` entries in
`check_readiness()`. Either fail startup outright or report not-ready.

Related: `src/sibyl/api/app.py:161-166` calls `runtime_services.startup()` outside the
`try`, so a partial startup leaks whatever did initialize.

## 2. `GET /backups/jobs/{job_id}` is cross-org readable (major, S)

`src/sibyl/api/routes/backups.py:428-449`.

```python
@router.get("/jobs/{job_id}")
async def get_backup_job_status(job_id: str) -> dict[str, Any]:
    from sibyl.jobs.queue import get_job_status
    info = await get_job_status(job_id)
    return {..., "result": info.result, "error": info.error}
```

It is the only route in the file with no `org: AuthOrganization = Depends(get_current_organization)`
(compare `:184, :194, :225, :283, :318, :345, :370, :407`). The router-level
`Depends(require_org_admin())` at `:76` only proves the caller administers *some* org, and
the codebase itself documents that this is no privilege at all: `persistence/surreal/setup.py:154-156`
notes "every user owns a personal organization with the OWNER role, so an org-scoped check
gates nothing for instance-wide settings."

`info.result` for a backup job is the payload built at `src/sibyl/jobs/backup.py:455-462`,
which carries `organization_id`, `backup_id`, and error text.

The correct pattern already exists one file over: `src/sibyl/api/routes/jobs.py:42-80`
`_job_visible_to_org`, applied at `jobs.py:349-350` and `:373-374`, which 404s on mismatch.
Fix is to reuse it.

## 3. Idempotency lock scope defaults to `"unknown"` (major, S)

`src/sibyl/api/idempotency.py:268-275`:

```python
org = arguments.get("org")
organization_id = str(getattr(org, "id", "unknown"))
actor = arguments.get("auth") or arguments.get("ctx") or arguments.get("user")
principal_id = str(getattr(actor, "user_id", None) or ... or getattr(actor, "id", "unknown"))
```

`serialize_idempotent_request` recovers the tenant by *introspecting the decorated
handler's parameter names*. A handler that names its org parameter anything other than
`org`, or its actor anything other than `auth`/`ctx`/`user`, silently falls back to the
literal string `"unknown"` — and `idempotency_lock` then acquires the lock in a shared
`"unknown"` scope (`idempotency.py:230`, `manager.acquire(organization_id, lock_id)`).

Two consequences. Cross-tenant contention: two orgs replaying the same `Idempotency-Key`
against the same path serialize against each other. And divergence: the lock is scoped to
`"unknown"` while the idempotency *record* is written with the real `organization_id`
passed separately by the route (`replay_idempotent_response(organization_id=...)`), so the
mutual exclusion the record's replay logic assumes is not the exclusion actually held.

This is the exact "group_id defaulted/confused" shape the multi-tenancy contract forbids.
Fix: raise on a missing org/actor rather than defaulting, and make the resolution explicit
rather than name-based.

Second defect in the same module, `idempotency.py:234-243`: when the lock lease cannot be
extended, `renew_lease` logs `idempotency_lock_lease_lost` and returns. The critical
section keeps executing without a lock, and `:252` then releases with a token that is no
longer valid. Lease loss should abort the operation, not narrate it.

## 4. Rate limiting is inert on 179 of 185 routes (major, S)

`src/sibyl/api/app.py:187-188` sets `app.state.limiter` and registers the 429 handler, but
**`SlowAPIMiddleware` is never added**. In slowapi, `default_limits` are applied by that
middleware; without it, only routes carrying an explicit `@limiter.limit(...)` decorator
are limited. There are exactly six, all in `src/sibyl/api/routes/auth.py` (`:870, :961,
:987, :1413, :1451, :1532`).

So `settings.rate_limit_default` ("100/minute", `config.py:374-377`) is advertised in
config, documented, and enforced nowhere. `RATE_LIMITS` and `get_rate_limit()`
(`src/sibyl/api/rate_limit.py:36-54`) have no caller in `src/` at all.

The test file makes this worse rather than catching it.
`tests/test_rate_limiting.py:9-42` asserts that the `RATE_LIMITS` dict contains the
expected string values and that `get_rate_limit("auth") == RATE_LIMITS["auth"]` — it
tests a dictionary that nothing reads. No test issues N+1 requests and expects a 429.
This is a green suite over an inert feature.

Two smaller issues in the same file. `_get_key` (`rate_limit.py:17-22`) reads
`request.state.jwt_claims`, which `AuthMiddleware` only populates for JWTs, never for
`sk_` API keys — so all API-key traffic from one egress IP shares a single bucket. And
`limiter` is constructed at import time from `settings`, so `reload_settings_from_env()`
cannot affect it.

## 5. `X-Request-ID` validation is bypassed by middleware ordering (major, S)

`src/sibyl/api/errors.py:259-266` `get_request_id` validates a caller-supplied header
against `_REQUEST_ID_RE = ^[A-Za-z0-9_.:-]{1,128}$` — but only *after* checking
`request.state.request_id` first and returning it if already set.

`src/sibyl/api/app.py:130-131` `RequestIdMiddleware` sets
`request.state.request_id = request.headers.get(REQUEST_ID_HEADER) or generate_request_id()`
with no validation at all.

Middleware order (`app.py:267-289`; `add_middleware` prepends, so last-added is outermost)
is VersionHeader → RequestId → AccessLog → Auth → Session → CORS. `RequestIdMiddleware`
therefore always runs first and always wins, and the regex in `errors.py` is dead for
every real request. An arbitrary-length, arbitrary-content client string flows into the
structlog context (`app.py:133`), into every error payload
(`errors.py:281`, `safe_error_payload`), and back out in the response header
(`app.py:136`).

Also note `AccessLogMiddleware` and `RequestIdMiddleware` both set `request.state.request_id`
and both stamp the response header — duplicated responsibility across two layers with
different rules. Collapse them into one.

## 6. Setup mode is an unauthenticated bypass gated on data presence (major, M)

`src/sibyl/persistence/surreal/setup.py:42-44`:

```python
async def is_setup_mode() -> bool:
    return not (await get_setup_status()).setup_complete
```

and `setup_complete` is `_has_records(payload["initialized_memberships"])`
(`setup.py:57-71`) — i.e. "does at least one org_members row exist with role owner or
admin".

Four dependencies return with **zero authentication** when that is true:
`require_setup_mode_or_auth` (`:107-113`), `require_setup_mode_or_admin` (`:116-138`),
`require_settings_admin` (`:141-148`), `require_settings_owner` (`:151-163`).

What sits behind them:

- `src/sibyl/api/routes/settings.py:174` `GET /settings`, `:205` `PATCH /settings` —
  the PATCH writes provider API keys. Note both call `await require_settings_owner(request)`
  *inside the handler body* (`:186`, `:217`) rather than as a `Depends`, so the gate is
  invisible to the dependency graph and to the OpenAPI schema. `DELETE /settings/{key}`
  (`:308`) is the only one that pre-blocks setup mode explicitly (`:319-320`).
- `src/sibyl/ai/llm/routes.py:38` (`/settings/ai`, mounted at `app.py:303`) — all 8 routes
  have no `Depends` whatsoever and call `require_settings_owner(request)` in the body
  (`:116, 133, 158, 182, 191, 203, 230`).
- `src/sibyl/api/routes/setup.py:291` `POST /setup/config` — writes API keys.

The risk is that the gate is derived from data rather than from state: an instance that
loses its `organization_members` rows (bad restore, mid-migration, wrong namespace
selected, a query that returns empty for a transport reason) re-opens the entire settings
surface to anonymous writes. There is no explicit "setup completed" marker, no one-way
latch, and no network restriction on the bypass.

Fix: persist an explicit `setup_completed_at` marker that only ever transitions forward,
and move the in-body `require_*` calls into route `dependencies=[...]` so the gate is
declarative and auditable.

## 7. Nothing tests that routes have auth (major, S)

Finding #2 exists because the test suite cannot see it. `tests/test_wire_scope.py:1-8`
is the closest thing to an end-to-end auth test and its own docstring says it "substitut[es]
only the auth dependencies" — it overrides `get_auth_context`, `get_current_organization`,
`get_current_org_role`, which means a route that *forgot* those dependencies is
indistinguishable from one that has them. `tests/test_route_access_seams.py` calls the
`_verify_*_access` helpers directly with mocks.

There is no test that walks `create_api_app().routes` and asserts each non-allowlisted
route resolves an auth dependency. That test is maybe 30 lines, it would have caught #2,
and it is the cheapest durable guard in this report.

## 8. API-key REST scope enforcement rests on unpinned Starlette behavior (major, S)

`src/sibyl/auth/dependencies.py:48-56`:

```python
def _is_rest_request(request: Request) -> bool:
    return request.url.path.startswith("/api/")
```

This is the *only* thing that makes `api:read` / `api:write` scopes apply to REST
(`dependencies.py:132-135`). The FastAPI app is mounted at `/api`
(`src/sibyl/main.py:166`), so whether this predicate fires depends on whether
`request.url.path` inside a mounted sub-app is the full path or the prefix-stripped one.

I verified it against the installed Starlette 1.6.0: `Mount.matches` builds `child_scope`
with `root_path = root_path + matched_path` and deliberately does **not** rewrite
`scope["path"]`, while `URL.__init__` uses bare `scope["path"]`. So today the predicate is
correct and scopes are enforced.

The debt is that nothing pins it. `tests/test_auth_api_key_scopes_rest.py:15-19`
hand-constructs a scope dict with `"path": "/api/me"` and never drives the real mounted
app. Older Starlette versions *did* strip the mount prefix from `scope["path"]`. If a
future upgrade reverts to that, every API key silently gains unscoped read and write on
the whole REST surface and this test stays green. One `TestClient(create_combined_app())`
test that sends a read-only key at a POST and expects 403 closes it permanently.

## 9. Domain logic lives in the route package (major, L)

Measured by AST over `src/sibyl/api/routes/`: 204 route handlers totalling 9,096 lines,
plus **332 module-private helper functions totalling 6,559 lines**. The helpers are not
serialization glue; they are the domain.

Worst concentrations:

- `src/sibyl/api/routes/memory.py` — 3,362 lines, of which the first ~1,690 are ~60
  private helpers before a single route: authorization policy composition
  (`_authorize_memory_policy:300`, `_authorize_project_scope_write:246`,
  `_authorize_raw_promotion_api_key_scopes:1476`, `_authorize_share_api_key_scopes:1564`),
  audit-event derivation (`_derived_records_from_audit:1169`, `_correction_history:1207`),
  and promotion/share state machines (`_promotion_state:1237`, `_share_state:1253`).
- `src/sibyl/api/routes/context.py:224-616` — retrieval fusion and refinement:
  `_fuse_context_evidence` (139 lines, `:251`), `_execute_context_refinement_round` (`:393`),
  `_execute_accurate_context_evidence_search` (164 lines, `:449`), `_refinement_frontier`
  (`:238`). This is core retrieval algorithm sitting behind an HTTP decorator.
- `src/sibyl/api/routes/entities.py:568-1190` — the whole entity visibility model
  (`_entity_visible_to_reader`, `_raw_capture_visible_to_reader`,
  `_resolve_entity_list_project_filter`, `_reader_memory_grants`).
- `src/sibyl/api/routes/auth.py:1087-1387` — `_render_device_verify_page`, 300 lines of
  inline HTML and CSS in a route module, in a repo that ships a Next.js frontend.

Individual handlers are correspondingly large: `entities.py:2076 create_entity` 237 lines,
`entities.py:1655 create_entities_bulk` 195, `tasks.py:791 update_task` 194,
`entities.py:1451 get_entity` 190.

Consequence beyond aesthetics: this logic is only reachable through HTTP, so the MCP
surface either duplicates it or silently diverges from it, and the tests that cover it are
route tests with mocked dependencies rather than unit tests over the rule.

Six route modules also each define their own identical `get_entity_graph_runtime(group_id)`
lazy-import wrapper: `entities.py:89`, `graph.py:39`, `rag.py:60`, `context.py:78`,
`tasks.py:60`, plus `graph.py:45` for the query adapter. Same three lines, six copies.

## 10. A hand-rolled SurrealQL lexer is the security boundary on `/admin/debug/query` (major, M)

`src/sibyl/api/routes/admin.py:726-1018` is roughly 290 lines of character-by-character
SurrealQL scanning — `_skip_query_literal`, `_skip_query_comment`, `_skip_query_separators`,
`_has_identifier_boundary`, `_read_query_string_value`, `_query_has_dynamic_content_table`,
`_query_has_additional_statement`, `_query_disallowed_namespace_call` — feeding
`_is_supported_debug_dialect` (`:1009-1017`), which gates `debug_query` (`:1025`).

The code is careful and clearly hard-won (the comment at `:987-988` about walking nested
`::` chains shows real iteration). The debt is structural:

- It is a **deny-list** (`_GRAPH_DEBUG_FORBIDDEN_TOKENS`, `:893-921`). Any SurrealQL
  keyword added by a future server version defaults to permitted. `LET`, `RETURN`, `FOR`,
  and `IF` are not in the list; today they are blocked only incidentally by the
  "first token must be SELECT" rule.
- It is a security boundary implemented as string parsing, in a route file, with no
  fuzzing and no property tests.
- It is the wrong layer. SurrealDB can express read-only access directly; a
  scoped read-only session or a DEFINE-level permission would make the parser unnecessary.

Blast radius is bounded — the endpoint is `_OWNER_ONLY` and executes against
`str(org.id)`'s own namespace, with client-supplied `group_id`/`organization_id`/`org_id`
params correctly rejected at `_debug_params_for_org` (`:881-890`). But a boundary this
fragile should not live in a route module.

Same endpoint, second issue: `admin.py:1057-1067` catches every exception and returns
**HTTP 200** with `error=str(e)` in the body. That path never touches
`http_exception_payload`, so `sanitize_error_text` (`errors.py:350-358`) never runs and
raw SurrealDB exception text — which can echo query fragments and internal identifiers —
goes straight to the client. `_validate_content_debug_query`'s `ValueError` (`:868-878`)
lands here too, so a validation failure returns 200 instead of 400.

## 11. Configuration is process environment, mutated at startup and by routes (major, M)

Three separate mechanisms fight over the same state.

`src/sibyl/config.py:805` instantiates a module-global `settings` at import. Modules
capture values from it at import time (e.g. the security warning at
`auth/dependencies.py:40`, the `limiter` at `rate_limit.py:26-31`, and the
`require_org_role`/`require_org_admin` branch selection at `dependencies.py:206` and `:269`).
`reload_settings_from_env()` (`config.py:808-812`) mutates `settings.__dict__` in place and
none of those captured values move.

`src/sibyl/services/settings.py:394-433` `load_runtime_settings_from_db` reads eleven
settings out of SurrealDB at startup and writes them into `os.environ` via
`os.environ.setdefault`. Provider keys, embedding models, and embedding dimensions are
therefore transported through the process environment.

`src/sibyl/api/routes/settings.py:170-171` does the same thing at request time:
`os.environ[env_var] = str(value)` inside a route handler.

Net effect: config is global mutable process state written from three places, one of which
is an HTTP handler. `os.environ.setdefault` also makes it a one-way door — a value loaded
at startup can never be cleared without a restart, which is why
`DELETE /settings/{key}` has to hand-clear env vars (`settings.py:329-331`).

Separately, `Settings` carries ~85 fields with three overlapping `model_validator(mode="after")`
passes, two of which duplicate the same defaulting logic verbatim: `config.py:232-235` and
`config.py:519-522` are identical four-line blocks setting `auth_store` and
`local_auth_enabled`.

## 12. The FastAPI app's `lifespan` never runs in the deployed process (major, S)

`src/sibyl/api/app.py:158-166` defines a `lifespan` that constructs `RuntimeServices` and
calls `startup()`/`shutdown()`, and passes it to `FastAPI(lifespan=lifespan)` at `:183`.

In the deployed process the API app is a **mounted sub-app** (`src/sibyl/main.py:166`,
`Mount("/api", app=api_app)`). Starlette delivers `lifespan` scope only to the top-level
router; mounted ASGI apps never receive it. The outer Starlette lifespan
(`main.py:106-159`) does the real startup.

So `api/app.py`'s lifespan is dead in production and live only under
`TestClient(create_api_app())`. Two failure modes follow: tests exercise a startup path
production does not, and if the mounting ever changes to propagate lifespan,
`RuntimeServices.startup()` runs twice with no guard against it. Delete the sub-app
lifespan, or make `RuntimeServices` idempotent and say which one owns the lifecycle.

Adjacent dead branch in the same file: `main.py:142-159`. `worker_task` is initialized to
`None` and never assigned — with `coordination_backend == "local"` it logs "runs
in-process", otherwise it logs "Embedded worker disabled in surreal mode". So
`SIBYL_RUN_WORKER=true` (`main.py:249`) does nothing on the redis backend while logging as
though the decision were deliberate, and the cancellation block at `:156-159` is
unreachable.

## 13. `get_current_organization` does not verify membership (major, M)

`src/sibyl/persistence/surreal/auth.py:702-774` `SurrealAuthContextResolver.resolve` runs
one query returning `user`, `organization`, and `membership` independently (`:725-741`).
`AuthContext.organization` is populated from the org row **whenever the `org` claim names a
real organization**, regardless of whether the membership row exists — only `org_role`
goes `None` (`:764-769`).

`src/sibyl/auth/dependencies.py:180-186` `get_current_organization` then checks only
`ctx.organization is None`. So the dependency that every org-scoped route uses to obtain
`group_id` — and that `get_entity_manager`/`get_graph_store`
(`src/sibyl/api/dependencies.py:67-100`) use to open a client on that org's Surreal
namespace — never asserts the caller is a member.

The tenancy invariant holds today only because the *separate* `require_org_role`
dependency is present on the routers that matter, and because token issuance does check
membership: `persistence/surreal/organization_runtime.py:760-768` (`switch_org`) 404s when
`membership is None`. I checked the refresh path too — `api/routes/auth.py:1571-1584` takes
the org from the signed refresh token's own claims, not from client input.

Two residual gaps. Membership is never re-verified on refresh
(`persistence/surreal/auth_runtime/sessions.py:241-256` mints a new access token from the
caller-supplied `organization_id` without re-reading the session's org or the membership
row), so a user removed from an org keeps refreshing into it until the session is revoked;
they are stopped by `org_role is None` at the role check, not by the org resolution. And
the WebSocket path (`src/sibyl/api/websocket.py:398-399`) scopes broadcasts purely on the
`org` claim with no membership check at all.

Fix: make `resolve` refuse to populate `organization` without a membership row, so the
invariant is enforced once at the chokepoint instead of 31 times at the routers.

## 14. Errors swallowed into 200 responses (minor, S)

- `src/sibyl/api/routes/crawler.py:291-350` `preview_url`: the handler raises
  `HTTPException(400, "Invalid URL")` at `:299` **inside** a `try` whose `except Exception`
  at `:347` catches it and returns HTTP 200 with `{"error": "Failed to preview URL"}`.
  `HTTPException` is an `Exception`. So an invalid URL, an SSRF block, a timeout, and a
  successful fetch are all 200s.
- `src/sibyl/api/routes/jobs.py:191-193` `list_jobs`: queue unreachable returns
  `{"jobs": [], "total": 0, "error": ...}` with status 200 — a caller cannot distinguish
  "no jobs" from "the queue is down".
- `src/sibyl/api/routes/jobs.py:146-152` `jobs_health`: returns `status: "unhealthy"` at 200.
- `src/sibyl/api/routes/rag.py:756-763`: graph search failure returns an empty related-entity
  list at 200.
- `src/sibyl/api/routes/admin.py:277-286` `health`: returns `HealthResponse(status="unhealthy",
  errors=[str(e)])` at 200.

## 15. Raw exception text returned in 200 bodies (minor, S)

`admin.py:1063-1066` (`error=str(e)`) and `admin.py:279-286` (`errors=[str(e)]`) return
exception strings in successful responses, which bypasses the entire sanitization pipeline
in `src/sibyl/api/errors.py`. That pipeline only runs for `HTTPException` and unhandled
exceptions via the handlers at `app.py:209-258`.

Worth noting the sanitizer is also aggressive in the other direction:
`sanitize_error_text` (`errors.py:350-358`) replaces any message longer than 200 chars, or
containing two or more path-like segments, or matching
`token|secret|password|credential|api_key`, with the generic "Invalid request data." A
legitimate message like "Password reset token has expired" is destroyed. And `_safe_details`
(`errors.py:376-385`) drops every detail key outside a 7-item allowlist, so
`ProjectAuthorizationError`'s `project_id` (`auth/authorization.py:113`) never reaches the
client.

## 16. Three auth-dependency families across two layers (minor, M)

- `src/sibyl/auth/dependencies.py` — `require_org_role`, `require_org_admin`,
  `get_current_org_role`.
- `src/sibyl/auth/authorization.py` — `require_project_role/read/write/admin`,
  `verify_entity_project_access`.
- `src/sibyl/persistence/surreal/setup.py:107-171` — `require_global_admin`,
  `require_settings_admin`, `require_settings_owner`, `require_setup_mode_or_admin`,
  `require_setup_mode_or_auth`.

The third family is FastAPI dependency code living inside the **persistence** package,
imported directly by routes (`api/routes/logs.py:18`, `api/routes/setup.py:19-20`). It
imports back up into `sibyl.auth.dependencies` (`setup.py:12`), so the layering is circular
in intent if not in module graph. `auth/authorization.py:97-119` also still carries a class
explicitly marked `DEPRECATED` in its own docstring.

## 17. Two WebSocket endpoints, two hand-written auth paths (minor, M)

`src/sibyl/api/websocket.py:371-399` `_extract_org_from_token` and
`src/sibyl/api/routes/logs.py:71-99` `_validate_owner_token` independently reimplement
token extraction, JWT verification, and session validation. Neither reuses
`resolve_claims`. Neither supports `sk_` API keys.

`logs.py:110-114` takes the token as a **query parameter** (`ws://host/api/logs/stream?token=<jwt>`),
which puts a live access token into reverse-proxy access logs and browser history.

Both validate once at connect and never again, so revoking a session does not close an open
socket — a revoked admin keeps streaming server logs until they disconnect.

## 18. WebSocket broadcast is serial (minor, S)

`src/sibyl/api/websocket.py:122-130` awaits `conn.websocket.send_json(message)` in a loop
over every connection in the org. One slow or backpressured client delays delivery for
every other client in that organization and blocks the calling request handler that
triggered the broadcast. `asyncio.gather` with per-connection exception capture is the
straightforward fix.

## 19. Unpooled Surreal clients on hot paths (minor, S)

`src/sibyl/persistence/surreal/setup.py:47-74` `get_setup_status` calls
`build_surreal_auth_client()` and `client.close()` on every invocation — and `is_setup_mode()`
is called on **every request** to `/settings`, `/settings/ai`, and `/setup`. Same shape in
`src/sibyl/api/readiness.py:63-78`, which connects and closes a fresh client per readiness
probe (every few seconds under a k8s `readinessProbe`). Both bypass the pooling described
in `CLAUDE.md`'s "Surreal Connection Pooling" section.

## 20. Dead and duplicated code (minor, S)

- `src/sibyl/config.py:793-796` `fully_surreal` is `return True` with **zero callers** in
  the repo.
- `src/sibyl/auth/rls.py` (71 lines) — the whole module is a retired shim.
  `get_rls_session`/`require_rls_session` raise 501 and nothing in `src/` imports any of it.
- `src/sibyl/config.py:232-235` and `:519-522` — the same four-line defaulting block
  duplicated across two validators.
- `src/sibyl/api/rate_limit.py:36-54` — `RATE_LIMITS` and `get_rate_limit`, referenced only
  by the test that asserts their contents.
- `src/sibyl/api/routes/logs.py`, `websocket.py`, `setup.py` all carry their own
  `disable_auth` special case (`logs.py:74`, `websocket.py:379`, `websocket.py:420`).

## 21. `enqueue_crawl` / `enqueue_sync` take an optional org (minor, S)

`src/sibyl/jobs/queue.py:76-98` are the only two enqueue functions with
`organization_id: str | None = None`; all fifteen others require it
(`:105, 126, 134, 148, 170, 188, 204, 218, 233, 256, 274, 290, 317, 338, 352, 366, 380`).

Every real caller passes it (`api/routes/crawler.py:546-553`,
`core_runtime_ports.py:126-141` — the port even types it as required), so the optionality
buys nothing and costs real complexity: `api/routes/jobs.py:59-79` has a whole fallback
branch that re-derives the org for `crawl_source`/`sync_source` jobs by loading the source
record, purely because those two jobs might not carry one. Tighten the signature and the
fallback deletes itself.

## 22. Migration/cutover tooling ships in the daemon (minor, M)

`src/sibyl/cli/migrate.py` is 2,548 lines — the second-largest file in `apps/api` — with 13
commands including `rehearse` (`:1907`), `cutover` (`:2090`), `auth-flow-compare` (`:1833`),
and `consolidate` (`:1362`). It shells out to `ssh`/`subprocess` (`:189, :228`), runs moon
tasks (`:530`), and drives a full pre-cutover acceptance suite (`:599`).

This is one-time Neo4j-era migration rehearsal machinery. `settings.store` is now
`Literal["surreal"]` (`config.py:169-172`) and `fully_surreal` is hardcoded `True`
(`config.py:794-796`), so the alternative the cutover moves *from* no longer exists in the
codebase. It ships in every `sibyld` wheel.

## 23. `disable_auth` is a half mode, resolved at import (minor, S)

`require_org_role` and `require_org_admin` (`auth/dependencies.py:198-208`, `:258-271`)
choose between the real check and a `_noop` **when the decorator is constructed**, i.e. at
router-module import. `reload_settings_from_env()` cannot change it afterwards.

The mode is also incomplete: with `disable_auth=True` the role checks vanish, but
`get_current_organization` still raises 401 because `resolve_claims` returns nothing, so
most routes still fail. Production is protected (`config.py:237-241`), but as a dev mode it
half-works, and each of `logs.py:74`, `websocket.py:379`, `websocket.py:420` handles it
differently.

## 24-26. Middleware and transport hygiene (minor)

- `src/sibyl/api/app.py:274-279`: `SessionMiddleware(secret_key=settings.jwt_secret.get_secret_value())`
  reuses the JWT signing key for itsdangerous cookie signing. Two cryptographic purposes,
  one key; rotating either forces rotating both.
- `src/sibyl/api/app.py:261-273`: `cors_origins` hardcodes `http://localhost:3337` and
  `http://127.0.0.1:3337` alongside `public_url`, with `allow_credentials=True`, in every
  environment including production.
- Six middleware layers on every request, four of them `BaseHTTPMiddleware`
  (`VersionHeader`, `RequestId`, `AccessLog`, `Auth`) plus `SessionMiddleware` and CORS.
  `BaseHTTPMiddleware` wraps each request in an anyio task group and is the known-costly
  option; `AuthMiddleware` (`auth/middleware.py:22-35`) exists only to pre-decode a JWT that
  `resolve_claims` re-verifies anyway (`auth/dependencies.py:102-115`), and its output feeds
  exactly one other consumer, the rate-limit key function that finding #4 shows is inert on
  almost every route. Pure ASGI middleware, or folding these into one layer, is the fix.

---

# MCP surface

`src/sibyl/server.py` (2,844 lines) registers 13 tools and 2 resources on
`mcp.server.fastmcp.FastMCP`, mounted at `/` (`src/sibyl/main.py:167`) alongside REST at
`/api`.

## M1. `api:read` / `api:write` are not enforced on MCP at all (blocker, M)

I grepped `server.py`, `auth/mcp_auth.py`, and `auth/mcp_oauth.py` for `api:read` and
`api:write`. **Zero hits.** Those scopes exist only in the two REST copies
(`auth/dependencies.py:35-36` and `persistence/surreal/auth_runtime/_common.py:79-80`).

The REST gate fires only for paths under `/api/` (`auth/dependencies.py:48-49`, finding #8),
and MCP is mounted at `/`, so MCP requests never reach it. The result:

- The default scope set for a newly created API key is `["mcp"]`
  (`src/sibyl/api/routes/auth.py:117`; the allowlist is
  `{"api:read", "api:write", "mcp"}` at `auth.py:111`).
- That default key is correctly **refused** every mutating REST call
  (`dependencies.py:132-135` → 403 `insufficient_api_scope`).
- The same key is **fully accepted** by MCP `add` (`server.py:2436`), `remember`
  (`server.py:2544`), and `manage` (`server.py:2677`) — including
  `manage("complete_task")`, `manage("correct_memory")`, and `manage("crawl")`.

So `mcp` is a single all-or-nothing capability with no read/write distinction, and the
least-privilege story the REST scopes tell is undone by the surface that most agents
actually connect through. This is the most consequential finding in the MCP lane.

Two scope fallbacks make it worse:

- `auth/mcp_oauth.py:393`: `scopes = list(auth.scopes or []) or [OAUTH_SCOPE]` — an API key
  with **zero** scopes is upgraded to `["mcp"]` and admitted.
- `auth/mcp_oauth.py:98`: `_parse_scopes_from_claims` falls through to `return [OAUTH_SCOPE]`,
  so a JWT carrying no `scope`/`scopes` claim is granted `mcp`.

`auth/mcp_auth.py` gets this right — `SibylMcpTokenVerifier` hard-rejects a missing `mcp`
scope on both branches (`:43-44`, `:70-71`) with no fallback. It is dead code:
`server.py:1822` leaves `token_verifier = None` and passes it as `None` at `server.py:1851`.
81 lines of correct, unreachable enforcement.

## M2. MCP takes a client-supplied project as authorization input (major, S)

`src/sibyl/server.py:319-323`:

```python
accessible_projects = await _get_accessible_projects(ctx)
if accessible_projects is None:
    if project:
        return {project}
    return None
```

`_get_accessible_projects` returns `None` when `ctx.user_id` is falsy and
`api_key_project_ids is None` (`server.py:296-301`), and also whenever
`resolve_accessible_project_graph_ids` returns `None`. On that branch the caller's raw
`project` string becomes the authoritative scope set with **no membership check and no
existence check**, and is then passed as `accessible_projects` into the memory policy
(`server.py:1104, 1256, 1269`) and into `authorize_memory_write`. Client input becomes an
authorization input.

The REST twins never do this: `api/routes/context.py:632-639` and
`api/routes/search.py:391-399` always route a named project through
`verify_entity_project_access(..., require_existing_project=True)` first.

## M3. MCP writes carry no org-role gate (major, M)

REST write routes gate on `require_org_role(*_WRITE_ROLES)`
(`api/routes/memory.py:1878`, `api/routes/entities.py:2072`) or an explicit role check
(`api/routes/synthesis.py:121-122`). On MCP, `McpContext.org_role` defaults to `None`
(`server.py:126`) and the API-key branch of `_get_mcp_context` (`server.py:184-201`) never
populates it. It is forwarded into `MemoryPolicyContext.organization_role`
(`server.py:143`) but no MCP tool ever compares it against a write-role set. The only role
check on the entire MCP surface is `_require_owner_mcp_context` for the `logs` tool
(`server.py:1796-1799`).

Concretely: `_resolve_mcp_project_scope` does a plain set-membership test
(`server.py:325-327`), so a project **VIEWER** can drive `remember` and `add` writes through
MCP where REST demands CONTRIBUTOR.

`_add_mcp_entity` (`server.py:1068-1158`) also skips `_validate_related_to_targets_for_write`
(`api/routes/entities.py:808`, applied at `:2112`), so `related_to` targets go from tool
argument (`server.py:2443`) into core `add` (`server.py:1126`) unvalidated.

## M4. MCP re-implements REST handlers rather than sharing them (major, L)

Not stylistic duplication — the copies have measurably diverged:

- **API-key memory-scope gate.** `server.py:437` vs `api/routes/memory.py:274`:
  `effective = ctx.user_id if memory_scope == MemoryScope.PRIVATE.value else scope_key`
  versus `... if memory_scope == "private" and not scope_key else scope_key`. MCP discards
  an explicitly supplied `scope_key` on a private write; REST honours it. Same intent, two
  answers.
- **Active-task link resolution.** `_resolve_mcp_capture_links` (`server.py:532-571`) vs
  `_resolve_reflection_links` (`api/routes/context.py:644-683`) — same probe, but MCP passes
  `allowed_memory_scope_keys=ctx.api_key_memory_scope_keys` (`server.py:557`) and REST omits
  the argument entirely (`context.py:661-670`).
- **Context pack.** `_compile_mcp_context_pack` (`server.py:333-411`) vs `context_pack`
  (`context.py:796-879`) — REST normalizes the query via `normalize_retrieval_question`
  (`context.py:816`) and MCP never calls it, so an identical goal retrieves differently on
  the two surfaces. REST also supports `record_exposure` and `knn_type_overfetch`
  (`context.py:834-835`); MCP has neither.
- **Deny auditing.** REST writes `memory.policy_deny` audit rows on both denial paths
  (`memory.py:347-358`, `:371-382`); MCP raises a bare `ValueError(decision.reason)`
  (`server.py:510`, `:485`) and writes nothing. Policy denials on the MCP surface are
  invisible to the audit log.
- **Idempotency.** MCP hand-rolls reserve/replay/complete twice — `server.py:632-663` and
  `server.py:1688-1728` — including a duplicated "interrupted takeover" comment block
  (`server.py:646-655` ≡ `:1707-1717`). REST gets the same behavior from
  `@serialize_idempotent_request` (`memory.py:1880`) and helpers.
- **Byte-identical duplicate:** `_append_unique_ids` at `server.py:522-529` and
  `context.py:616-623`.
- Also `_log_mcp_policy_decision` (`server.py:414-430`) ≡ `_log_policy_decision`
  (`memory.py:167-183`); `_deny_mcp_api_key_memory_scope` (`server.py:465-485`) ≡
  `_api_key_memory_scope_denial` (`memory.py:279-297`); `_authorize_mcp_memory_write`
  (`server.py:488-519`) ≡ `_authorize_memory_policy` (`memory.py:300-387`);
  `_reflect_mcp_memory` (`server.py:784-889`) ≡ `reflect_context` (`context.py:925-1014`).

`_manage_workflow_transition` (`server.py:1332-1491`) re-derives transition responses,
message strings, revision validation, citation recording, and learning-job enqueue on top
of the shared `transition_work_item` service. The comment at `server.py:1274-1277`
acknowledges it was written to catch MCP up to REST — which is the diagnosis, not the fix.

Root cause is finding #9: the shared rule lives in the route package, so the only way to
reach it from MCP is to copy it.

Separately, `logs` uses `has_owner_membership` on MCP (`server.py:1796-1799`) while REST
`/logs` uses `require_global_admin` (`api/routes/logs.py:31, 58`) — two different admin
notions guarding the same data.

## M5. A third copy of the REST scope check lives in the persistence layer (major, S)

`persistence/surreal/auth_runtime/_common.py:78-100` contains verbatim copies of
`_SAFE_HTTP_METHODS`, `_REST_READ_SCOPES`, `_REST_WRITE_SCOPE`, `_is_rest_request`,
`_api_key_allows_rest`, and `_insufficient_api_scope` from `auth/dependencies.py:34-73`.
Both are live: the `_common` copy runs inside `resolve_request_claims`
(`persistence/surreal/auth_runtime/sessions.py:200-207`), reached from `logout`
(`api/routes/auth.py:1613`) and `resolve_request_user`.

A security check duplicated across the auth layer and the persistence layer will drift, and
when it does one request path will enforce and the other will not.

## M6. mcp 2.0 migration surface (assessment, not a defect)

Every `mcp.` import in `src`:

| file:line | symbols |
|---|---|
| `server.py:17` | `mcp.server.auth.middleware.auth_context.get_access_token` |
| `server.py:18` | `mcp.server.fastmcp.FastMCP` |
| `server.py:1824` | `mcp.server.auth.settings.AuthSettings`, `ClientRegistrationOptions` |
| `auth/mcp_auth.py:16` | `mcp.server.auth.provider.AccessToken` |
| `auth/mcp_oauth.py:29-36` | `AccessToken`, `AuthorizationCode`, `AuthorizationParams`, `OAuthAuthorizationServerProvider`, `RefreshToken`, `TokenError` |
| `auth/mcp_oauth.py:37` | `mcp.shared.auth.OAuthClientInformationFull`, `OAuthToken` |

Plus tests (`test_mcp_oauth_multi_org_selection.py:10-11`,
`test_mcp_oauth_session_refresh.py:9-10`, `test_server_accessible_projects.py:1799`) and
client-side `mcp.ClientSession` in `tools/baselines/replay.py:10-11`.

FastMCP surface actually used: constructor kwargs `host`/`port`/`stateless_http`/`auth`/
`auth_server_provider`/`token_verifier` (`server.py:1844-1852`); `@mcp.tool()` ×13;
`@mcp.resource(uri)` ×2; `@mcp.custom_route(...)` ×4 (`server.py:1856, 1860, 1864, 1868`);
`streamable_http_app()` (`main.py:104`); `session_manager.run()` (`main.py:152`);
`run(transport="stdio")` (`main.py:212`). `sse_app` is unused.

**The good news: the 13 tool bodies are decoupled.** No tool takes a `Context` parameter and
no tool imports from `mcp`; signatures are plain Python types and returns are
`dict[str, Any]` via `_to_dict` (`server.py:1877`). A 2.0 bump would not touch them.

What it does touch, in cost order:

1. **`SibylMcpOAuthProvider`** (`auth/mcp_oauth.py:166-425`) — subclasses
   `OAuthAuthorizationServerProvider[...]` and implements 9 abstract methods (`get_client:177`,
   `register_client:193`, `authorize:203`, `load_authorization_code:215`,
   `exchange_authorization_code:225`, `load_refresh_token:276`, `exchange_refresh_token:322`,
   `load_access_token:388`, `revoke_token:423`) plus an `AuthorizationCode` subclass
   (`:132-134`). A full protocol implementation pinned to the 1.x provider ABC. This is the
   dominant cost.
2. **`get_access_token()` ambient lookup** (`server.py:168`) — the entire MCP auth model
   hangs off this contextvar. `_get_mcp_context` is the single identity source for every
   tool via `_require_mcp_context` (`server.py:240-252`). If 2.0 changes ambient-token
   retrieval, all 13 tools lose their identity source simultaneously.
3. **`create_mcp_server`** (`server.py:1802-1874`) — constructor kwargs, the
   `AuthSettings`/`ClientRegistrationOptions` shape (`:1827-1836`), and the
   `auth_server_provider` vs `token_verifier` mutual exclusion documented at `:1840-1842`.
4. **ASGI wiring** (`main.py:104, 152, 167`).
5. **`mcp.shared.auth` models** (`mcp_oauth.py:37`) used for dynamic-client-registration
   round-trip (`model_validate` at `:185`, `model_dump` at `:200`).
6. **`auth/mcp_auth.py`** — dead (see M1), but it still needs porting or deleting.

Roughly five files, concentrated in `mcp_oauth.py`. Deleting the dead `mcp_auth.py` and
folding its (correct) scope logic into the live path would shrink the migration and close
M1 at the same time.

## M7. Smaller MCP items (minor)

- `_get_mcp_context`'s JWT branch (`server.py:207-227`) calls `verify_access_token` but not
  `validate_access_session`, unlike both `dependencies.resolve_claims` (`:117`) and
  `mcp_oauth.load_access_token` (`:403`). Currently covered because the transport already
  validated, but the duplicated path itself does not re-check revocation.
- `manage` overwrites `organization_id` unconditionally (`server.py:1731`) — correct — but
  overwrites `user_id` only conditionally (`:1732-1733`, `if ctx.user_id:`). Same pattern
  for `created_by` at `:681-682` and `:1116-1117`. When `ctx.user_id` is falsy, a
  client-supplied `data["user_id"]` / `metadata["created_by"]` survives into core `manage`
  (`:1758-1766`).
- `_authorize_mcp_manage_action` returns `None` (`server.py:1259-1260`) for any action
  outside `MCP_ENTITY_PROJECT_POLICY_ACTIONS` (`:95-110`) and `MCP_PROJECT_ID_POLICY_ACTIONS`
  (`:111`), so `crawl`, `sync`, `refresh`, `link_graph`, and `link_graph_status` get **no
  policy decision at all** and reach core `manage` with client-controlled `data`, protected
  only by org isolation.

---

# Jobs and worker reliability

## J1. `"auto"` never selects Redis, so the default is an in-memory queue (blocker, M)

`src/sibyl/config.py:177-180` defaults `coordination_backend="auto"`. `config.py:799-801`:

```python
def resolved_coordination_backend(self) -> Literal["local", "redis"]:
    return "redis" if self.coordination_backend == "redis" else "local"
```

`"auto"` maps to `"local"`. Redis is **never** auto-selected regardless of whether Redis is
configured and reachable. Everything routes through this one value:
`coordination/broker.py:414-434`, `scheduler.py:22-42`, `locks.py:58-78`, `pending.py:47-65`.

That makes an in-process `asyncio.PriorityQueue` (`coordination/_local/broker.py:117`) the
default job queue. It persists nothing. Every queued job dies with the process, with no
record and no recovery. Combined with finding #1 (broker startup failures swallowed), a
deployment can look entirely healthy while losing every background job.

A setting named `auto` that ignores available infrastructure and always picks the weaker
option is the wrong default, and the log line at `main.py:115-120` reports it as though a
decision were made.

## J2. Delivery semantics differ by backend and neither is documented (major, M)

**Redis/arq — at-least-once, but only on crash.** The ack happens after the work
(`arq/worker.py:588-660`), so a crash mid-job leaves the queue entry and another worker
re-runs it once the `in_progress` TTL expires. But arq only retries on
`Retry`/`RetryJob`/`CancelledError` (`arq/worker.py:613-625`); a plain `Exception` sets
`finish=True` and fails permanently with zero retries (`arq/worker.py:629-635`). Nothing in
`src/sibyl/jobs/` ever raises `arq.Retry`. `WorkerSettings` (`jobs/worker.py:337-341`) sets
`max_jobs`, `job_timeout=3600`, `keep_result`, and `poll_delay` — never `max_tries`.

**Local — at-most-once, with no retry and no timeout.** `_worker_loop` pops the id
(`_local/broker.py:758`) and flips the record to `IN_PROGRESS` (`:764`) before running.
Death between those points is unrecoverable. `_run_job` awaits the function unbounded
(`:785`) — `job_timeout` is read only by arq; the local broker copies `max_jobs` and
`keep_result` (`:95-96`) but not the timeout.

## J3. A failed local job blocks its own retry for 24 hours (major, S)

`coordination/_local/broker.py:799-811` — on exception, `_run_job` sets
`record.status = JobStatus.COMPLETE` and stores the error string. `_enqueue_unique`
(`:730-733`) then treats `COMPLETE` as already-done and returns `created=False` for any
later enqueue with the same `job_id`, unless `clear_result=True`.

`enqueue_create_entity` (`:273-283`) and `enqueue_update_entity` (`:296-303`) do not pass
`clear_result`. So a failed `create_entity:{entity_id}` silently swallows every retry for
`keep_result` = 86,400 seconds (`jobs/worker.py:340`, consumed at `_local/broker.py:96`).
The caller receives the old job id and a success-shaped response.

The Redis path has the same 24-hour window from the other direction: arq's
`enqueue_job(_job_id=...)` returns `None` when either the job key or the result key exists
(`arq/connections.py:156-159`), so `_redis/broker.py:231-239` (`enqueue_update_entity`),
`:408-424`, and `:442-458` silently drop a re-enqueue within 24h and hand back a stale id.

## J4. `run_backup` reports failure as a successful job (major, S)

`src/sibyl/jobs/backup.py:430-462` catches every exception and **returns** a payload with
`"success": False` instead of re-raising. Both brokers therefore record the job as
succeeded (`_local/broker.py:813-823`; arq sets its own `success=True`), so it is never
retried, and `job_end` telemetry records `status="ok"` because `worker.py:136-140` reads
arq's flag rather than the payload's.

A nightly backup can fail every night and every signal in the system says it worked.

## J5. The local scheduler double-fires with more than one process (major, M)

`coordination/_local/scheduler.py` keeps its entire dedup state in per-instance dicts:
`self._last_fired` (`:35`, checked at `:80-81`) and `self._active_jobs` (`:34`, checked at
`:83-87`). `RuntimeServices._startup_scheduler` (`runtime_services.py:72-84`) starts one per
process.

Firing goes `_run_spec` → `enqueue_scheduled_job(spec.name)` (`_local/scheduler.py:135-138`)
→ `_enqueue_unique(job_id=f"scheduled:{function}", clear_result=True)`
(`_local/broker.py:553-559`), and the broker's `self._jobs` dict is also per-process
(`:106`). **N API processes run every cron job N times, every tick** — including
`consolidate_all_orgs`, `run_reflection_dream_cycle_all_orgs`, and `run_scheduled_backups`
(`jobs/worker.py:195-275`). `clear_result=True` additionally disables even the intra-process
dedup.

Redis mode is safe: `build_cron_jobs()` (`jobs/worker.py:82-95`) uses `arq.cron(unique=True)`,
which derives the job id from the scheduled timestamp so exactly one worker wins, and
`RedisScheduler` is a deliberate no-op (`_redis/scheduler.py:9-16`).

Redis-mode caveat: `WorkerSettings.cron_jobs = build_cron_jobs()` is evaluated in the class
body (`jobs/worker.py:329`), so the schedule is frozen at import and a runtime settings
reload (`runtime_services.py:29`) cannot change it. `get_cron_jobs` (`worker.py:328`) is
unused.

## J6. Multi-process on the local backend breaks five coordination guarantees (major, M)

Beyond the scheduler:

1. **Dedup is per-process.** `self._jobs` (`_local/broker.py:106`) — `create_entity:{id}`
   runs once per process, producing duplicate graph writes.
2. **Locks do not cross processes.** `LocalLockManager` is a plain `asyncio.Lock`
   (`_local/locks.py:17`) and `extend()` is a no-op (`:74-77`), so `entity_lock` in
   `jobs/entities.py:1252` and `jobs/operational_distillation.py:78` provides no mutual
   exclusion. This is also what backs `idempotency_lock` (finding #3).
3. **The pending registry does not cross processes.** `_local/pending.py:20-23` — the
   queue-until-materialized guarantee documented at `jobs/pending.py:6-13` is void.
4. **Source-import run state does not cross processes.** `_SOURCE_IMPORT_RUNS` is a module
   dict consulted before the Surreal fallback (`jobs/source_imports.py:138`, `:761-763`).
5. **Job listing and status are per-process.** `list_jobs` reads `self._recent_job_ids`
   (`_local/broker.py:571-575`), so the admin API shows only whichever process served the
   request.

## J7. Jobs missing org scope (major, S)

Graph access is namespace-isolated per org, so a query without an org predicate is still
contained *provided `group_id` is correct*. These are the ones where it is not:

- **`sync_all_sources`** (`jobs/crawl.py:270-284`) — `list_sources()` at `:274` takes no org
  argument and `sync_source(ctx, str(source.id))` at `:279` is called **without**
  `organization_id`. One job iterates every source in every org.
- **`poll_raw_capture_changefeed`** — org-scoped by argument, but the underlying read is
  not: `SHOW CHANGES FOR TABLE raw_captures SINCE ... LIMIT $limit`
  (`jobs/raw_changefeed.py:141-145`) runs against the **shared** content client
  (`persistence/surreal/content.py:212-213`) with no org predicate. Every org's change
  payload is read into the process and filtered in Python at `:299`. The `LIMIT` is shared
  too, so a busy org consumes another org's poll budget.
- **`_raw_capture_organization_ids`** (`jobs/raw_changefeed.py:222-237`) — cross-org
  `SELECT ... GROUP BY organization_id`.
- **`cleanup_old_backups`** (`jobs/backup.py:465-506`) — org-less by design; globs
  `settings.backup_dir` across all orgs (`:144`) and unlinks by mtime.
- **`purge_due_deleted_personal_memories`** (`jobs/privacy.py:15-34`) — purges globally,
  then reads the org id back off each returned row (`:22`) for the audit log.

Related visibility gap: `_JOB_ORG_ARGUMENT_INDEX` (`coordination/broker.py:21-31`) covers
only 9 functions. `run_backup`, `promote_raw_captures`, `poll_raw_capture_changefeed`,
`replay_memory_probes`, `create_learning_episode`, `create_learning_procedure`,
`update_task`, and `drain_source_import` are absent, so `_job_visible_to_org`
(`api/routes/jobs.py:40-79`) falls through to `return False` — those jobs are invisible to
their own org in the jobs API. It fails closed, which is the right direction, but on the
local backend it is total: `JobInfo.organization_id` is always `None`
(`_local/broker.py:64-76`), so visibility depends entirely on that 9-entry map.

## J8. Shutdown drains the backlog, then kills running work (minor, S)

`LocalQueueBroker.shutdown` (`_local/broker.py:128-178`) sets `self._queue = None` (`:134`),
then `await asyncio.wait_for(queue.join(), timeout=self._shutdown_grace_seconds)` (`:139`).

`local_queue_shutdown_grace_seconds` **is** honored (`config.py:661-666`, read at
`_local/broker.py:98-102`, applied at `:139`). Three problems around it:

- `queue.join()` waits for the whole **backlog**, not just in-flight work. A deep queue means
  the grace expires and running jobs are cancelled at `:164-165` even though they would have
  finished in time.
- Jobs still queued at timeout are lost. `_mark_queued_jobs_cancelled` (`:843-859`) only
  mutates in-memory records; there is no drain-to-disk and nothing re-enqueues them.
- Setting `self._queue = None` **before** draining (`:134`) poisons in-flight jobs that
  enqueue follow-up work: `_require_queue()` (`:866-869`) raises
  `RuntimeError("Local job broker is not running")`. `jobs/entities.py:716` and `:748`
  swallow exactly that and return success.

The arq shutdown hook is a bare log line (`jobs/worker.py:122-125`).

## J9. Failure-looks-like-success is systemic across job bodies (major, M)

Beyond J4, the pattern where an exception is logged and the job returns a success shape
appears throughout. The highest-consequence instances:

| Location | What is silently lost |
|---|---|
| `jobs/entities.py:716-721` | embedding-backfill job never enqueued; `create_entity` returns OK |
| `jobs/entities.py:748-753` | memory-extraction job never enqueued; `create_entity` returns OK |
| `jobs/source_imports.py:974-980` | raw promotion never enqueued; import reports COMPLETED |
| `jobs/source_imports.py:341-346` | import run state never persisted; run continues in memory only |
| `jobs/entities.py:533-534, 561-566, 619-620` | dedup link, explicit relationships, auto-links all dropped |
| `jobs/entities.py:245-251` | inherited task-knowledge edges dropped |
| `jobs/entities.py:274-284` | relationship writes dropped inside `_persist_job_relationships`; only the count differs |
| `jobs/raw_promotion.py:466-476` | lineage edges lost, replaced with a metadata note |
| `jobs/raw_promotion.py:668-673` | extraction enqueue failure recorded as metadata; job succeeds |
| `jobs/memory_extraction.py:635-651` | projection failure returns `projection_state: "partial"` with 0 entities; job succeeds |
| `jobs/pending.py:194-200, :203` | failed pending ops are recorded, then `clear_pending_operations` runs **unconditionally** and discards them |
| `jobs/worker.py:148-151` | `_job_result_info` returning `None` makes `job_end` telemetry record `status="ok"` |
| `jobs/backup.py:533, :585` | `except Exception: # noqa: S110` with bare `pass` on archive metadata reads |

`jobs/probes.py:173-182` is the one place that deliberately guards against this — a `None`
return from `update` is counted as a failure. It is the right model for the rest.

Counted differently: `_safe_broadcast` swallows every WebSocket and pub/sub event at debug
level in four separate modules (`jobs/entities.py:189-190`, `jobs/crawl.py:32-33`,
`jobs/backup.py:164-166`, `jobs/source_imports.py:189-190`), so realtime UI updates can stop
entirely with no signal above debug.

---

# Suggested order of attack

1. **#1** (readiness blind to broker/scheduler/lock failure) and **J1** (`auto` never picks
   Redis) — together these are the difference between a healthy-looking pod and one silently
   dropping every background job. Neither fix is large.
2. **M1** (no `api:read`/`api:write` on MCP) — the dead `auth/mcp_auth.py` already contains
   the correct check; wiring it in and deleting the fallbacks at `mcp_oauth.py:98` and `:393`
   closes it.
3. **#2** (backups IDOR) and **#7** (route-auth coverage test) — the second prevents the
   next instance of the first. Both are small.
4. **#3** (`"unknown"` idempotency scope), **M2** (client project as authz input), **J3**
   (failed job blocks its own retry), **J4** (`run_backup` fakes success) — four small,
   independent correctness fixes.
5. **#4** (inert rate limiting), **#5** (request-id bypass), **#12** (dead lifespan) — small,
   and each currently has a green test or a passing probe telling you otherwise.
6. **#6** (setup-mode bypass) and **#13** (membership check at the resolver) — medium,
   security-structural.
7. **#9** / **M4** — the large one. Moving the shared rules out of `api/routes/` into core is
   what stops MCP and REST from drifting, and it is the root cause behind M2, M3, and most of
   M4. Best done incrementally, starting with the memory policy helpers that already exist in
   two diverged copies.

