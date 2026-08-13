# Tech debt audit: apps/web, apps/cli, apps/e2e, infra

Read-only audit at commit `5388d986` (main, clean). Every finding below was verified by
reading the code; hypotheses that did not survive a check are recorded in the
"Checked and clean" sections so nobody re-investigates them.

Severity: **blocker** (ship-stopping or data-integrity), **major** (real user or
maintainer cost, fix soon), **minor** (hygiene, fix opportunistically).
Size: **S** (< half day), **M** (1-3 days), **L** (a week or more).

---

## Top findings, ranked

| # | Finding | Where | Sev | Size |
|---|---|---|---|---|
| 1 | `sibyl auth login` writes the token under a key no command reads; login exits 0 | C1 | blocker | S |
| 2 | CI classifier ignores `uv.lock`, `pyproject.toml`, `charts/`, `VERSION`, Dockerfiles — whole pipeline no-ops | I1 | blocker | S |
| 3 | Stock `helm install` ships production-labelled with MCP auth disabled | I3 | blocker | S |
| 4 | Every browser e2e test asserts the login page; Playwright declared and never imported | E1, E2 | blocker | M |
| 5 | ~32 gate suites run for the first time at RC cut, never in CI | I2 | blocker | S |
| 6 | `sibyl entity delete` destroys data with no confirmation; its `--yes` is dead | C2 | blocker | S |
| 7 | Worker crash-loops under the chart's own defaults; Surreal password regenerates per upgrade | I4, I5 | blocker | S/M |
| 8 | 19 CLI failure paths exit 0; failed writes buffer to disk silently | C3, C4 | major | S |
| 9 | Realtime give-up kills the only freshness signal (focus + reconnect refetch both off) | W1 | major | M |
| 10 | 1 of 185 GitHub Actions is SHA-pinned, incl. a mutable branch ref on the PyPI publish job | I7 | major | M |

The three themes worth naming: **a green check that proves nothing** (2, 4, 5, plus I11's
nonexistent lockfile path and I12's cacheable gate), **silent failure** (1, 6, 8, 9, plus
W4's swallowed SSR errors), and **defaults that only work by accident** (3, 7, plus I14
and I15's unreachable config).

---

## apps/web (Next.js 16)

222 non-test TypeScript files, 124 of them client components. Biome config is strict
and genuinely enforced (8 suppressions repo-wide, exactly 1 `any`).

### W1. Realtime give-up silently breaks the only freshness signal — major, M

`components/providers.tsx:51-52` disables both `refetchOnWindowFocus` and
`refetchOnReconnect`, with the comment at lines 47-50 stating plainly that
"Realtime/websocket invalidation is the primary freshness signal."

`lib/websocket.ts:344-347` gives up permanently after `maxReconnectAttempts = 10`
(`websocket.ts:192`), returning early without calling `setStatus('disconnected')`. The
last status written was `'reconnecting'` at line 351, so the client parks in
`'reconnecting'` forever. `reconnectAttempts` resets only on a successful connect
(line 273) or `destroy()` (line 340), and no UI offers a manual reconnect.

The two decisions are each defensible alone and destructive together. After roughly
17 minutes of cumulative backoff on a flaky connection, the app has no websocket, no
focus refetch, and no reconnect refetch. Hooks with a polling fallback survive
(`useMemorySourceImport` at `hooks.ts:680-686`, `useBackups` at `hooks.ts:2099`,
`useBackupJobStatus` at `hooks.ts:2101-2110` all poll when status is not `'connected'`),
but the primary data hooks have no `refetchInterval` at all: `useTasks`
(`hooks.ts:1293-1299`), `useEntities` (`hooks.ts:568-574`), `useProjects`, `useEpics`.
Those views then serve stale cache indefinitely until a hard reload or a mutation, with
no indication anything is wrong.

Confirmed at `hooks.ts:1050-1135`: `useRealtimeUpdates` is the sole invalidation driver
for `entity_created`, `entity_pending`, `entity_updated`, and `entity_deleted`, all of
which route through `invalidateByEntityType` to reach `tasks.all`, `projects.all`,
`entities.all`, and `sources.all`. None of those has a polling fallback, so all four
primary list views are affected.

Fix: transition to `'disconnected'` on give-up, and either re-enable
`refetchOnReconnect` or add a reconnect affordance. The status transition alone is S;
the full freshness-contract repair is M.

### W2. Entity detail queries with params are never invalidated — major, S

`queryKeys.entities.detail` (`hooks.ts:55-56`) takes an optional trailing `params`
object that becomes part of the key. `EntityDetailPanel` uses it: `entity-detail-panel.tsx:92-96`
calls `useEntity(entityId, undefined, { include_summary: false, related_limit: 0 })`
when `queryMode === 'graph'`, producing key
`['entities','detail',id,{include_summary:false,related_limit:0}]`.

Every invalidation site omits the params. `invalidateByEntityType` at `hooks.ts:224`
calls `invalidateQueries({ queryKey: queryKeys.entities.detail(entityId) })`, which
builds `['entities','detail',id,undefined]`. React Query's `partialMatchKey` compares
index 3 as `partialMatchKey({include_summary:false,...}, undefined)`, and the `typeof`
guard rejects it — so the graph panel's cached entity is never invalidated after an
update or delete.

The same key shape breaks `useDeleteEntity` at `hooks.ts:843`:
`getQueryData(queryKeys.entities.detail(id))` misses the params-bearing entry, so
`entityType` resolves `undefined` and the mutation falls through to the `default`
branch of the switch (`hooks.ts:219-226`) — wrong invalidation set for tasks, projects,
and sources deleted from the graph panel.

Fix: split the registry into an exact key and a prefix key (`detail(id)` for
invalidation, `detailWith(id, params)` for the query), the standard TanStack pattern.

### W3. No route-level error boundaries; three routes have none at all — major, S

`find app -name 'error.tsx'` returns nothing: there are zero Next.js error boundary
files. The only boundary is a single `AsyncBoundary` in `components/layout/main-shell.tsx:50`,
which `app/(main)/layout.tsx:10` applies to the `(main)` group.

That leaves `app/login/page.tsx` (523 lines of client code), `app/setup/page.tsx`, and
`app/reset-password/page.tsx` with no boundary whatsoever — a render throw on the login
or first-run setup page produces Next's default error screen. Inside `(main)`, the
single page-level boundary means any error in any segment blanks the entire shell
rather than the failing panel, despite `loading.tsx` files existing for 15 routes.

Fix: an `error.tsx` per route group, plus segment boundaries around the heavy panels.

### W4. Server-side prefetch failures are silent — major, S

Four server components swallow fetch errors with no logging and no user signal:
`app/(main)/entities/page.tsx:53-54`, `app/(main)/search/page.tsx:31-33`,
`app/(main)/projects/page.tsx:15-21`. Each is a bare `.catch(() => undefined)` (or
`.catch(() => ({...}))`), so a backend outage or a timeout degrades to a client-side
refetch with nothing recorded.

`lib/api-server.ts` is otherwise well built — a 5s `AbortSignal.timeout`
(`api-server.ts:25-32`), cookie forwarding (`api-server.ts:61-72`), and a distinct
timeout error message (`api-server.ts:78-80`). All of that diagnostic value is
discarded at the call site. `lib/logger.ts` exists and is not used here.

Fix: log the swallowed error server-side; keep the UI degradation.

### W5. Two architectures for the same job — major, M

Nine pages use the server-component shell that prefetches in parallel and hands
`initialData` to a client child — `app/(main)/entities/page.tsx:44-68` is the
reference implementation, joined by `page.tsx`, `settings/page.tsx`, `memory/page.tsx`,
`archive/page.tsx`, `projects/page.tsx`, `search/page.tsx`, `memory/captures/page.tsx`,
`entities/[id]/page.tsx`.

Twenty-four pages are wholesale `'use client'` with no SSR prefetch, including the
heaviest ones: `tasks/page.tsx:1`, `graph/page.tsx`, `sources/page.tsx`,
`epics/page.tsx`, and all seven `settings/*` pages. `tasks/page.tsx:48-52` fires four
client queries on mount with nothing server-rendered.

Neither pattern is wrong, but which one a route uses is unpredictable, so every new
page is a coin flip and the SSR path's `initialData` plumbing rots on the routes that
skipped it. Fix: pick one, document it, migrate the high-traffic routes.

### W6. `lib/api.ts` is a 3,040-line god-module — major, M

One file holds 169 exported interfaces (the entire API type surface) plus the `api`
object with 26 namespaces (`api.ts:2065-3000`: `entities`, `rawCaptures`, `memory`,
`sourceImports`, `synthesis`, `search`, `graph`, `admin`, `telemetry`, `jobs`,
`backups`, `auth`, `security`, `preferences`, `profile`, `session`, `orgs`, `tasks`,
`projects`, `epics`, `sources`, `rag`, `metrics`, `setup`, `settings`).

Nearly every component imports from it, so any type edit invalidates the module for the
whole graph on every typecheck and rebuild. `lib/hooks.ts` at 2,135 lines has the same
shape one layer up. Fix: split into `lib/api/<domain>.ts` with a barrel, mirroring the
existing `lib/constants/` split which already does this well.

### W7. `graph/page.tsx` is 1,764 lines holding seven components — major, M

`app/(main)/graph/page.tsx` defines `MobileEntitySheet` (:94), `ClusterLegend` (:169),
`StatsOverlay` (:271), `GraphToolbar` (:317), and `GraphPageContent` (:786) — the last
alone runs ~970 lines with 11 `useState`, 9 `useEffect`, and 14 `useCallback`/`useMemo`
hooks. The canvas paint path (`paintNode`, :1206-1390) is 185 lines inline.

Nothing here is broken; it is the single hardest file in the app to modify safely, and
`components/graph/` already exists as the natural home (it currently holds only
`entity-detail-panel.tsx`).

### W8. Priority constants duplicated five times, with a latent divergence — minor, S

`lib/constants/tasks.ts:55-57` already defines `TASK_PRIORITIES` (including `someday`)
and `TASK_PRIORITY_CONFIG` as the canonical source. Five local redefinitions ignore it:

- `PRIORITY_ORDER` at `app/(main)/epics/page.tsx:29`, `components/tasks/task-list-mobile.tsx:23`,
  `components/tasks/kanban-board.tsx:30`
- `PRIORITY_LABELS` at `components/tasks/task-card.tsx:76` and `components/epics/epic-card.tsx:53`
  (byte-identical)
- `PRIORITY_STYLES` at `components/tasks/task-card.tsx:40` and `components/epics/epic-card.tsx:17`

The epics copy omits `someday`, so at `epics/page.tsx:137-138` a `someday` epic takes
the `?? 99` fallback. To be precise: with today's five priorities that still sorts it
last, identical to the `someday: 4` the task copies use — **this is a latent
divergence, not a live bug**. It becomes one the moment a sixth priority is added to
`TASK_PRIORITIES` and only three of the five copies are updated.

### W9. Six dead query-key registry entries — minor, S

Defined in `hooks.ts` and referenced nowhere: `projects.members` (:136),
`epics.tasks` (:152), `epics.progress` (:153), `metrics.projectsSummary` (:165),
`session.all` (:74), `rag.all` (:83). They imply cache surfaces that do not exist.

Three keys also bypass the registry entirely with raw literals: `['metrics']`
(`hooks.ts:198,206`, where the registry has `metrics.org`/`metrics.project` but no
`metrics.all` prefix to use), and `['crawl_progress', source_id]`
(`hooks.ts:1133,1184`) — the only snake_case key in a kebab-case registry. A registry
that the code routes around in three places is one rename away from a silent cache miss.

### W10. Five exported Suspense wrappers with zero consumers — minor, S

`components/suspense-boundary.tsx` exports `SuspenseBoundary` (:97), `PageSuspense`
(:117), `SectionSuspense` (:126), `CardGridSuspense` (:135), and `ListSuspense` (:144).
None is imported anywhere in the app; only the 15 `*Skeleton` exports below them are
used, and those go through `loading.tsx` files directly. `StatusIndicator`
(`components/ui/icons.tsx:294`) is likewise unused and is visibly the component built
for W1's missing offline state — its `status` union even omits `'reconnecting'`.

### W11. Stale nav entry for a redirect stub — minor, S

`lib/constants/navigation.ts:43` registers `archive: { label: 'Memory Captures',
href: '/archive' }`. `app/(main)/archive/page.tsx:24` is a pure redirect to
`/memory/captures`, and every real link in the app already points at the new route
(`dashboard-content.tsx:894`, `memory-home.tsx:457,710,745,764`,
`search-result.tsx:72`). The stub also carries a `loading.tsx` rendering `ArchiveSkeleton`
for a page that never renders, and `archive/page.test.tsx`.

Related naming collision: `/sources/[id]` (docs-crawl RAG sources,
`app/(main)/sources/[id]/page.tsx`) and `/memory/sources/[sourceId]` (memory provenance,
`app/(main)/memory/sources/[sourceId]/page.tsx`) are unrelated concepts sharing a noun.

### W12. Query-key factory type understates its own key — minor, S

`queryKeys.graph.hierarchical` (`hooks.ts:93-94`) is typed
`(params?: { max_nodes?, max_edges?, refresh? })`, but `useHierarchicalGraph`
(`hooks.ts:968-980`) passes seven fields including `projects`, `types`, `resolution`,
and `cluster_id`. Because the argument is a variable rather than a literal, excess
property checking does not fire, so it compiles. Caching is correct at runtime (the
whole object lands in the key); the type is simply a lie that would mislead the next
person reasoning about invalidation.

### W13. Minor a11y residuals — minor, S

Biome disables `noStaticElementInteractions`, `useSemanticElements`, and
`useKeyWithClickEvents` globally (`apps/web/biome.json:19-21`), so clickable-div
correctness is unenforced. In practice the code is disciplined — all five
`<div onClick>` sites carry `role`, `tabIndex`, and `onKeyDown`. Two residuals:

- `components/tasks/task-card.tsx:150-171` has `role="button"` and a key handler but no
  `aria-label` and no `focus-visible` ring, while its sibling `epic-card.tsx:118-136`
  has both. Keyboard users get no visible focus on the kanban board.
- `components/layout/project-selector.tsx:159-170` handles `Enter` but not `Space`
  (ARIA requires both for `role="button"`), and nests a real `<button>` (:174) inside
  the `role="button"` div — nested interactive controls.

### Checked and clean (do not re-investigate)

- **Charts are theme-aware.** `components/metrics/charts.tsx:82-97` reads SilkCircuit
  CSS custom properties off `document.documentElement` and re-reads on every theme
  flip. The hex block at :52-62 is an explicitly labelled SSR fallback, not a dark-only
  palette.
- **Canvas colors are theme-aware.** `lib/constants/graph.ts:45-51` provides
  `canvasNodeColor`/`canvasClusterColor` with a documented dawn-theme darkening pass,
  because canvas cannot read CSS variables.
- **The graph starfield redesign landed.** The historical finding is resolved on the web
  side: `GRAPH_DEFAULTS.MAX_NODES` is now 500 with a comment explaining the legibility
  tradeoff (`constants/graph.ts:56-59`), overview/detail resolution modes exist, the
  server sends `recommended_resolution` and the client honours it
  (`graph/page.tsx:884-902`), clusters are labelled and colored, and `StatsOverlay`
  (`graph/page.tsx:271-306`) surfaces displayed-vs-total counts. The 1000/5000
  truncation still lives in `apps/api/src/sibyl/persistence/graph_runtime.py:1140-1141`
  and `graph_communities.py` (not this lane), but the client no longer requests it.
- **No polling freeze from W1.** Every `useWebSocketStatus` gate is
  `status === 'connected' ? false : poll`, so a stuck `'reconnecting'` still polls. W1's
  impact is confined to hooks with no polling fallback.
- **Both `dangerouslySetInnerHTML` sites are sound** — a static theme FOUC script
  (`app/layout.tsx:53`) and Shiki's own escaped output (`components/ui/markdown.tsx:67`).
- **`AsyncBoundary` is not dead** (used at `main-shell.tsx:50`); the gap is coverage, not
  existence.
- **Lint hygiene is excellent**: 8 `biome-ignore` suppressions and 1 `any` across 222
  files, `noExplicitAny`/`noNonNullAssertion`/`noFloatingPromises` all at `error`.

One config note: `apps/web/biome.json:73-76` puts `noFloatingPromises` and
`noMisusedPromises` in `nursery`. Nursery rules are renamed or promoted between Biome
minors, so a routine bump can silently drop both checks with no lint error. Worth
pinning awareness to the dependency-bump checklist. (minor, S)

---

## apps/e2e

27 tests across five files. The auth and CLI-runner fixtures are well built, bounded
waits are used correctly everywhere, and two tests (archive round-trip, context recall)
are genuine end-to-end proofs. The problems are coverage and vacuity, not flakiness.

### E1. The entire browser suite cannot fail — blocker, M

`tests/browser/test_smoke.py:28-57`: all five tests issue
`httpx.get(path, follow_redirects=True)` and assert the status is in
`(200, 301, 302, 307, 308)`. `apps/web/src/proxy.ts:35-43` redirects every
unauthenticated request to `/login`, and the matcher at `proxy.ts:52` covers `/`,
`/tasks`, `/graph`, `/settings`. The tests send no cookies, so all four
protected-route tests follow the redirect and assert that the login page returned 200.
They are five copies of `test_login_page`.

Deleting `apps/web/src/app/(main)/tasks/page.tsx` would not fail `test_tasks_page`. When
the frontend is down the autouse fixture at :22-26 skips the class, so the suite has
exactly two outcomes: skip or pass. CI spends minutes building and booting the frontend
(`ci.yml:649-651`, `:678-690`) to prove nothing.

### E2. Playwright is declared, installed, documented, and never imported — blocker, S

`apps/e2e/pyproject.toml:22-24,29` declares `playwright>=1.40` as both an optional extra
and a dev dependency, `apps/e2e/moon.yml:36-38` defines `playwright-install`, and
`README.md:79-85` documents the setup. Zero playwright imports exist under `tests/`. The
`browser` marker (`pyproject.toml:59`, described as "requires playwright") is attached to
a pure-httpx suite. This is what makes E1 look acceptable at a glance.

### E3. Zero coverage of org/tenant isolation — blocker, M

`CLAUDE.md` names namespace-per-org as the load-bearing invariant. No e2e test creates
two users in two orgs and asserts that user A cannot read user B's entities, tasks, or
captures. Every existing test shares one session-scoped user (`conftest.py:359`), so
none could detect a leak. A regression here is cross-tenant data disclosure.

### E4. e2e is excluded from CI's task graph and re-declared by hand — major, S

Every task in `apps/e2e/moon.yml` (lines 12, 19, 28, 35, 38, 51) carries `preset: server`.
Verified against the installed moon 2.2.6: `moon task e2e:test` reports
`Mode: persistent / Runs in CI: No`, while `api:test`, `core:test`, and `cli:test` all
report `Runs in CI: Yes`. Root `moon.yml:212-254` omits `e2e:` from `:check`.

CI works around this by bypassing moon entirely — `ci.yml:696-703` runs
`cd apps/e2e && uv run pytest -v` with `SIBYL_E2E_CLI_COMMAND` re-declared inline. So the
moon task definitions and the CI invocation are two configurations that drift silently:
editing `apps/e2e/moon.yml` has no CI effect, and the CI run ignores the `-m` marker
filters.

### E5. Assertions that cannot fail — major, S each

- `tests/api/test_health.py:31,37` — `assert status_code in (200, 401, 403)`; only a 500 fails.
- `tests/api/test_health.py:41` — `assert "total_entities" in data or "entities" in data
  or isinstance(data, dict)`; the third disjunct is unconditionally true, making the
  first two decorative.
- `tests/api/test_endpoints.py:81` — `isinstance(data, dict)` is the whole test body.
- `tests/api/test_endpoints.py:108,116` — `isinstance(data, (dict, list))`.
- `tests/api/test_endpoints.py:70-73` — loops over returned entities to check a type
  filter; an empty result passes having verified nothing.
- `tests/cli/test_tasks.py:27-38` — creates a task, lists by status, then asserts only
  `isinstance(tasks, list)`. The creation is decorative and the named behavior (status
  filtering) is untested. Same shape at `tests/cli/test_entities.py:50-56,74-82`.

### E6. A test that passes when the command is broken — major, S

`tests/cli/test_projects.py:52-64` runs `project show --json`, and on failure falls
through to re-verifying `project list` (comment: "may not be implemented"). If
`sibyl project show` regresses or is deleted, the test still passes.

### E7. Mock-only unit tests gated behind a live server — major, S

All five tests in `tests/test_cli_runner.py` are pure `unittest.mock` tests of argument
construction and touch no network, but `conftest.py:468-470` makes `require_services`
`autouse=True`, pulling in a session-scoped `wait_for_services` that raises `TimeoutError`
after 30s without a backend. They also carry no marker, so both `e2e:test-api` and
`e2e:test-browser` skip them.

### E8. The perf gate is off by default and never runs — major, S

`tools/perf/multi_user.py:26` sets `DEFAULT_ERROR_RATE = 0.0` (correct), but `max_p95_ms`
defaults to `_env_optional_float("SIBYL_PERF_MAX_P95_MS")` with no fallback (:324-326),
and `check_thresholds` short-circuits on `is not None` (:196). `apps/e2e/moon.yml:20-28`
does not set it, so the suite measures p95, prints it, and asserts nothing. Load is
4 users x 3 iterations x 5 operations = 60 requests
(`tests/perf/test_multi_user_performance.py:47-49`).

It also never runs at any cadence: `skipif SIBYL_E2E_PERF != 1` (:28-32), no `test-perf`
in any workflow, and `nightly-regression.yml` has only `baseline-parity`, `live-graph`,
and `restore-to-scratch`. Root `:check` runs `multi-user-perf-test` (`moon.yml:770`), but
that targets `tools/tests/test_multi_user_perf.py` — unit tests of the harness, not a
live run.

Spot-checked independently and confirmed: `apps/web/src/proxy.ts:36-43` redirects every
unauthenticated request to `/login` and the matcher at :50-55 covers all four tested
paths; `grep -rn playwright apps/e2e/tests/` returns nothing; all six `apps/e2e/moon.yml`
tasks carry `preset: server`.

While confirming E4 I also found `apps/e2e/moon.yml:47-51`: the `format` task carries
`preset: server`, marking a one-shot `ruff format` as a persistent server task. Its
sibling `lint` (:44-46) correctly does not. Copy-paste slip. (minor, S)

### E9. Coverage gaps, ranked

- **Web: 33 routes, 0 meaningfully covered.** Highest value missing: login → dashboard
  (nothing proves a user can sign in through the UI), `/setup` first-run (a broken
  wizard bricks every new install), `/search`.
- **CLI: 26 registered sub-apps, 8 covered.** No coverage for `login`/`logout`/`whoami`/`auth`
  (`apps/cli/src/sibyl_cli/main.py:134,157-158,164`), `epic`, `team`, `org`, `synthesis`,
  `explore`, `export`, `ingest`, `crawl`, `session`, `pending-writes`, `admin`, `skill`,
  `docs`, `debug`, `logs`, `doctor`, `update`, `config`, `note`, `brief`, `init`.
  Highest value: the auth commands, the first thing every user touches.
- **API: 31 route modules, 5 endpoints touched.** Untouched and high value: `backups`
  (a silently broken restore is unrecoverable), `setup`, `context`/`memory` (the core
  read path, currently exercised only indirectly through the CLI).
- **Auth flows:** login is a fixture (`conftest.py:359-394`), so a failure surfaces as a
  collection error rather than a named test. No test for logout, refresh, invalid
  credentials, expired-token rejection, or password reset.

### E10. Shared session user with no cleanup — minor, M

`e2e_auth_token` is session-scoped (`conftest.py:359`); no test deletes what it creates.
`test_projects.py` creates four projects per run, `test_tasks.py` five tasks plus four
projects. Names are uniquified (`conftest.py:478-495`) so runs do not collide, but the
org accretes permanently. Ephemeral in CI; unbounded growth when run locally against the
dev DB per `README.md:9-11`. Defaults at `conftest.py:22-23` (`localhost:3334`,
`localhost:3337`) collide with a running `moon run dev` stack, which is what makes the
accidental-pollution path easy to hit.

### E11. Undeclared dependency on a pre-seeded corpus — minor, S

`ci.yml:698-700` authenticates as `baseline-corpus@sibyl.dev`, whose org is populated by
`moon run baseline-seed` + `baseline-replay-runtime` (`ci.yml:677-680`). Tests like
`test_entities_with_type_filter` read whatever that seed produced, but nothing in
`conftest.py` or `README.md` mentions the dependency, so a local run against a fresh org
exercises different paths than CI.

Boundary worth naming: `ci.yml:589` sets `SIBYL_MOCK_LLM: true`, so no e2e test covers
any LLM-dependent extraction, synthesis, or reflection path.

### Checked and clean

- **No committed junk.** `git ls-files apps/e2e` returns 20 files, all source.
  `.pytest_cache/`, `.ruff_cache/`, `.venv/`, `__pycache__/` are covered by root
  `.gitignore` (lines 2, 24, 46, 57) and are untracked local artifacts.
- **No unbounded waits.** All three `time.sleep` sites sit inside bounded deadline loops
  (`conftest.py:280-296`, `:337-351`, `:460-465`) that raise on expiry.
- **No order dependence.** Every test builds its own fixtures with UUID-suffixed names.
- **No suppressed failures.** The two `skip` sites are environment gates; no `xfail`
  anywhere. `--strict-markers` and `--strict-config` are both on
  (`pyproject.toml:50`).

---

## apps/cli

44 source modules, 38 test files. Findings below were produced with live probes against
a sandboxed `HOME`; I independently re-verified the two blockers and C6 by reading the
cited lines.

### C1. `sibyl auth login` writes the token under a key no other command reads — blocker, S

`auth.py:43-55` and `client.py:54-94` resolve the API base URL with inverted priority.
The client ranks the active context above `SIBYL_API_URL` (`client.py:78-83`, priorities
2 and 3); auth checks `SIBYL_API_URL` first (`auth.py:51-53`) and only falls back to
`SibylClient().base_url` (`auth.py:55`) when the env var is empty.

The precondition is precise: an active context **and** `SIBYL_API_URL` set to a
different host. With context `prod → https://sibyl.example.com` and
`SIBYL_API_URL=http://localhost:3334/api`, login stores the token under the localhost
key while every command reads the context key. The credential *scope* matches
(`context:prod:org:default` both sides); only the server key diverges.

Login prints `✓ Login complete` and exits 0. Every later command sees no token, takes a
401, and per C4 silently buffers its writes. The failure presents as a server-side auth
problem. This is the mechanism behind the known "sibyl auth still needs login +
pending-writes flush" symptom.

Fix: have `_compute_api_url` delegate to the client resolver for the no-explicit-server
case instead of reading the env var itself.

### C2. `sibyl entity delete` destroys data with no confirmation and a dead `--yes` — blocker, S

`entity.py:270` declares `yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip
confirmation")] = False`. The body (`entity.py:276-299`) never references it and
contains no prompt — it goes straight to `await client.delete_entity(entity_id)` at
:285. Verified by reading the full command.

The flag actively misleads: anyone reading `--help` concludes a guard exists. An AST
sweep found exactly two dead Typer parameters in the package, both named `yes` — this
one and `epic.py:583` (`archive_epic`).

### C3. Nineteen command failure paths exit 0 — major, S

The `else: error("Failed to ...")` tail sits at the end of a `@run_async` coroutine with
no `raise typer.Exit(1)`, so the process exits 0 on a refused write. Probed:
`task start`, `task create`, `task complete`, and `epic start` all print `✗ Failed to
...` and exit 0.

Sites: `task.py:589,631,672,726,815,1044,1162,1246`; `epic.py:492,531,574,614,685`;
`entity.py:261`; `project.py:275`; `crawl.py:299`; `crawl_shared.py:50`; `local.py:431`;
`main.py:2085`. Any CI job, hook, or agent checking `$?` reads a refused write as
success — and Sibyl's own session hooks are exactly such callers.

### C4. Failed writes are buffered to disk with nothing telling the user — major, S

`client.py:631-641` files every POST/PATCH/DELETE into `~/.config/sibyl/pending_writes/`
before sending. On connection failure `client.py:739-744` raises with no remediation
string. Probed against a dead port: three writes queued, user saw only "Is the server
running?", queue depth 3.

`sibyl doctor` never checks the queue (`doctor.py:509-531` covers config, health, port,
embedded-lock, write-probe, and agent checks only). Depth is exposed solely by
`sibyl debug status` (`debug.py:345`), which requires OWNER role. No age or size bound
on the queue either.

The buffer mechanism is sound — `_should_keep_pending_write` (`client.py:282-286`) errs
in the safe direction and the metric-sum invariant is documented
(`pending_writes.py:14-19`). The gap is purely surfacing. Combined with C1, this is how
memories go missing: login silently no-ops, writes silently queue, nothing reports it.

### C5. Error output goes to stdout and corrupts `--json` — major, S

`common.py:38` builds one console on stdout; `error()` (:183) and `warn()` (:188) both
print to it, while `pagination_hint()` (:148-170) writes to stderr. The streams are
backwards: machine-readable output shares stdout with human errors, and a purely
informational hint gets stderr. `sibyl <cmd> --json | jq` breaks on any error — the same
hazard `print_json`'s own docstring warns about (`common.py:52-61`).

### C6. The 401 handler recommends a command that does not exist — major, S

`main.py:1348-1350` prints `sibyl auth signup` as the "Create account" remediation.
Verified: `auth.py` registers `status` (:653), `set-token` (:701), `clear-token` (:717),
`login` (:744), `local-signup` (:833), and the `api-key` group. There is no `signup`.
This sits on the most frequently hit error path in the CLI.

### C7. The refresh-revoked recovery path is permanently dead — major, S to delete

`client.py:416-420` (`_silent_local_relogin`) reads `local_login_email` and
`local_login_password` from stored credentials. A repo-wide grep finds
`local_login_password` at exactly one location: that read. Nothing writes it.

So a revoked refresh token always takes the failure branch at `client.py:549-555` and
appends `Silent re-login failed: No stored local login credentials are available.` The
user is told a credential store is empty for a feature that was never wired up.

### C8. Login swallows the real failure behind bare `except Exception` — major, S

`auth.py:594-596` and `auth.py:633-636` catch bare `Exception` during the device-flow and
OAuth fallbacks and report only `type(e).__name__`. A TLS failure, a DNS error, and a bug
in our own code all degrade to `Device login unavailable (SSLError); trying OAuth login`,
ending at the generic `No supported login methods detected for this server`
(`auth.py:638-641`). Field login failures become undiagnosable from CLI output.

Adjacent dead branch: `auth.py:627-632` has an `if e.payload is not None:` / `else:`
where both branches emit the identical string and `e.payload` (the server's diagnostic)
is discarded either way.

### C9. `--password` accepted on the command line — major, S

`auth.py:769-771` (`login`) and `auth.py:836` (`local-signup`) accept `--password` as a
Typer option, putting passwords in shell history and in `ps` output for every other user
on the box. Typer supports `prompt=True, hide_input=True`.

### C10. `main.py` is a 3,945-line god-module with four clean seams — major, M

| Range | Contents | Extract to |
|---|---|---|
| 590-1330 | 23 `_print_*` renderers, zero command definitions | `memory_views.py` (~740 lines) |
| 2611-3354 | memory admin (audit, cite, inspect, blame, correct, promote, review, share, space) | `memory_admin.py` (744) |
| 2127-2420 | four synthesis commands | `synthesis.py` (294) |
| 3355-3514 | seven team commands | `team.py` (160) |

Those four cuts remove ~1,900 lines. The 590-1330 renderer block is the highest-value
one: pure presentation, no Typer coupling, no interleaved commands.

### C11. Thirteen near-identical mutation bodies, and the helper for them is dead — major, M

Every state-changing command in `task.py` and `epic.py` repeats the same six-beat body.
Mechanical proof: `task.py:611-636` (block) vs `task.py:652-677` (unblock) is 26 lines
each with 5 differing; `epic.py:510-536` vs `epic.py:593-619` is 27 each with 8
differing. Roughly 515 lines carrying ~182 lines of invariant scaffolding across
`task.py:562-594,611-636,652-677,700-731,771-823,992-1049,1097-1167,1213-1254` and
`epic.py:456-497,510-536,551-579,593-619,642-690`.

`task.py:51-58` defines `_output_response` — the exact helper this pattern wants — with
zero call sites. The abstraction was written and then every site was copy-pasted anyway.
The copies have already drifted: some echo the unresolved argument
(`task.py:579,628,669,723,803`; `epic.py:529,570`), others the resolved ID
(`task.py:912,1158`; `epic.py:612,682`), so an ID prefix yields inconsistent output.

Also dead: `_validate_task_id` (`task.py:108-138`) and `_validate_epic_id`
(`epic.py:60-84`) are referenced only by their own tests; live commands use the
`_resolve_*` variants.

### C12. Two implementations of `show`, and they disagree — major, M

`main.py:1408-1457` and `entity.py:175-210` both dispatch raw-memory-versus-entity,
sharing renderers but not dispatch. `main.py:1412-1428` tries `resolve_id_prefix` →
`get_entity`, catches 404, retries as raw memory. `entity.py:189-198` gates the raw path
solely on `is_raw_memory_reference`, a bare `raw_memory_` prefix test
(`memory_display.py:15-17`). A raw memory addressed by bare UUID resolves under
`sibyl show <uuid>` and 404s under `sibyl entity show <uuid>`.

Same shape in `sibyl note` vs `sibyl task note`: `main.py:2068-2086` re-implements
`task.py:1230-1246`, and only the latter calls `print_mutation_receipt`, so `sibyl note`
silently drops the revision receipt its sibling prints.

### C13. `epic tasks` is a verbatim copy of the task table that lost a feature — major, S

`epic.py:1003-1022` reproduces `task.py:272-296` character-for-character modulo
indentation, including a trailing `# Full title, no truncation` comment, but omits
`task.py:273-281`, which widens columns on wide terminals and non-TTY pipes. So
`sibyl task list` reflows titles and `sibyl epic tasks` never does. A function-local
`from sibyl_cli.common import format_status` at `epic.py:1001`, while `format_priority`
is imported at module top (`epic.py:21`), marks this as a later paste.

### C14. `org.py` opts out of the shared error handler in all seven commands — major, S

`org.py:15` imports only `error, print_json, run_async, success`, and every command ends
with a hand-rolled `except SibylClientError` at `org.py:51-53,85-87,114-116,164-166,
186-188,212-214,234-236`. Bypassing `common.py:301-330` means `sibyl org *` never
surfaces `request_id`, `remediation`, `error_code`, or the 404/400/409 labels that every
other lane module gets. `org.py:21` also shadows the shared console with a bare
`Console()`, losing the `width=160` non-TTY setting.

### C15. Four modules with zero test references — major, M

| Module | Lines | Untested surface |
|---|---|---|
| `explore.py` | 305 | all four commands: `related`, `traverse`, `dependencies`, `path` |
| `config_cmd.py` | 157 | seven commands including `set`, `reset`, `edit` |
| `project_refs.py` | 102 | `resolve_project_reference`, used by `main.py:78` |
| `view_shared.py` | 43 | shared render helpers |

`explore.py` and `config_cmd.py` are the real holes — an entire user-facing sub-app
each, one of which mutates config and includes a destructive `reset`.
(`memory_display.py` has no direct test module but is covered indirectly via
`test_main_capture.py` and `test_entity.py`.)

### C16. Formatting and palette drift — minor, S each

- **Palette bypasses** of `sibyl_core.logging.colors`: `epic.py:52-56` mixes imported
  constants with the same values inlined as hex (`"#ff6363"`, `"#50fa7b"`), plus a
  `"#888888"` fallback that is not in the palette at all; `logs.py:57-60` hardcodes all
  four hexes in comments naming the constants it declined to import; `org.py:149-152`
  and `pending.py:92` use generic Rich names.
- **Table factory bypassed** by `config_cmd.py:55`, `debug.py:138`, `dev.py:215`,
  `local.py:460`, `org.py:148`, `update.py:407` instead of `common.create_table`
  (:209-222). `org.py:148` sets no box or header style at all.
- **`pagination_hint` has one call site** (`task.py:451`) while ten sites hand-roll the
  same footer (`task.py:304,307`; `project.py:125`; `entity.py:167,345,461`;
  `epic.py:169,1022`; `document.py:353,488`).
- **`--json` coverage is uneven**: `org.py` 1 of 7 (and `list`/`create`/`switch` dump raw
  JSON unconditionally, the inverse of every other list command), `pending.py` 1 of 3,
  `project.py` 4 of 8, `context.py` 6 of 10, `epic.py` 8 of 9; `auth.py` has none.
- **Duplicated helpers**: `_parse_csv_ids` byte-identical at `task.py:161-164` and
  `main.py:345-348`; the CSV row emitter at `entity.py:137-152` and `project.py:92-108`;
  `document.py:109-113` vs `main.py:876-880`, which differ by one token
  (`or "project"` vs `or "private"`) — a semantic split hiding inside a copy.

### C17. `search` and `add` are hidden from help but documented as the core workflow — minor, S

`main.py:2607-2608` registers `recall` and `search` with `hidden=True`, `main.py:3774`
hides `add`, and `graph-search` (:1607) and `graph-add` (:1737) are hidden too. Rendered
`sibyl --help` shows no search command. The project's own `CLAUDE.md` documents
`/sibyl search "topic"` and `sibyl add "Pattern Title" "..."` as the standard cycle.

### C18. `epic` is deprecated with no deprecation path — minor, S

The sub-app help carries `DEPRECATED: an epic is just a task with subtasks...`, but no
epic command emits a runtime warning and no removal version is named. The only runtime
deprecation warning in the package is `main.py:201` for entity-type aliases.

### C19. Client lifecycle inconsistency — minor, M

Thirty-five sites use bare `client = get_client()` and never close, while
`document.py:230,295,331`, `session.py:119` and `main.py` use `async with`. Not a
correctness bug (`client.py:458` recreates the httpx client lazily), but every command
leaves an open `AsyncClient` at `asyncio.run()` teardown. Separately, `pending.py:193`
and `doctor.py:315` reach through to the private `client._request(...)` to pass
`_buffer_pending`/`_pending_write_id`; both uses are legitimate and want a public
replay/probe API.

### Checked and clean

`auth_store.py` is genuinely well built: 0600 file mode via `os.fchmod` before any
content is written (:83-114), 0700 directory enforcement with umask (:54-81), atomic
rename, and `fcntl.flock` exclusion (:34-51). No token is logged anywhere in the
package. `read_content_file` (`common.py:83-122`) defends against symlinks with
`O_NOFOLLOW`, plus size limits and binary detection.

---

## infra / CI

Nine workflows (5,195 lines), two Helm charts, ten `moon.yml` files. I independently
re-verified I1, I7, and I11 against the repo; all three reproduced exactly.

### I1. The CI change classifier ignores `uv.lock`, `pyproject.toml`, `charts/`, `VERSION`, and Dockerfiles — blocker, S

`.github/workflows/ci.yml:125`. The `runtime_changed` case list is
`apps/api/*|apps/cli/*|apps/e2e/*|packages/python/sibyl-core/*|tools/*|benchmarks/*|
baselines/*|moon.yml|.moon/*|.prototools|pnpm-lock.yaml|compose.e2e.yml|docker-compose*.yml`.

Verified by grepping the whole classify step (lines 46-165): `uv.lock` 0 hits,
`pyproject` 0, `charts` 0, `VERSION` 0, `Dockerfile` 0. Only `pnpm-lock` appears.

Every classifier flag stays false, so `run_static`, `run_build`, `run_tests`, and
`run_e2e` are all false and the pipeline no-ops. The concrete exposure: Dependabot runs a
weekly `uv` update (`dependabot.yml:4-7`), and any bump confined to `uv.lock` plus the
root `pyproject.toml` merges green with zero test coverage. That root `pyproject.toml` is
where the security pins live — the lxml CVE-2026-41066 override at :9-12 and
`cryptography>=48.0.1` at :30. Chart, Dockerfile, and `publish.yml` edits likewise
trigger nothing.

### I2. CI never runs the root gate suite; ~32 gate tests are release-time-only — blocker, S

`ci.yml` runs `:lint`, `:typecheck`, the four `test-cov` tasks, `bench-gate`,
`api:test-live`, the baseline replay, e2e pytest, and the storybook build. It never runs
`moon run :check`. That target appears only in `release.yml:289`, `publish.yml:68`, and
`publish-dogfood-images.yml:113`.

Root `check` (`moon.yml:212-254`) carries 41 deps, so roughly 32 gate suites run for the
first time at RC cut: `inventory-check`, `sync-versions-check`, `dev-script-test`,
`chaos-test`, `storage-access-check`, `baseline-test`, and the whole `*-gate-test` family
(memory-trust, usage-loop, trust-control, context-quality, workspace-trust, autonomy,
reflection-quality, forgetting, write-path-integrity, team-scope, auth-session,
overview-perf, synthesis, adapter-ingest, large-corpus-rehearsal, okf-export, doc-claim,
backup-restore, enterprise-readiness-evidence), plus `release-workflow-test`.

Combined with I1 this closes a loop: `release-workflow-test` declares `release.yml` and
`publish.yml` as inputs (`moon.yml:568-575`), but editing `publish.yml` neither trips the
classifier nor reaches a workflow that runs the test. The gate guarding the release
workflow cannot see edits to the release workflow.

### I3. Stock `helm install` yields a production-labelled deploy with MCP auth disabled — blocker, S

`charts/sibyl/values.yaml:168` (`existingSecret: ""`) with `:155`
(`SIBYL_ENVIRONMENT: "production"`). The `{{- if .Values.backend.existingSecret }}` guard
at `charts/sibyl/templates/backend-deployment.yaml:71` skips the secret block, so
`SIBYL_JWT_SECRET` is never set, and the dev auto-generation branch at
`apps/api/src/sibyl/config.py:557` is `elif self.environment != "production"`, so it does
not fire. The process runs with `jwt_secret == ""`.

MCP auth then disables itself: `auth_enabled = auth_mode == "on" or (auth_mode == "auto"
and jwt_secret_set)` (`server.py:1818`) with `mcp_auth_mode` defaulting to `"auto"`
(`config.py:364-367`). Startup logs it at INFO and continues (`main.py:133-137`), and
`SessionMiddleware` is built with `secret_key=""` (`api/app.py:276`). The production
validator (`config.py:229-276`) checks `disable_auth`, memory URLs, `cookie_secure`, and
default Surreal credentials, but never a JWT secret. The values comment at :166-167
states the requirement; a comment is not a guard.

Unverified: whether tokens signed with an empty HMAC key also verify, which would make
sessions forgeable rather than merely unauthenticated.

### I4. The worker crash-loops under the chart's own defaults — blocker, S

`charts/sibyl/values.yaml:361` (`worker.enabled: true`) with `:15`
(`coordinationBackend: "auto"`). `resolved_coordination_backend` returns `"redis"` only
on a literal `"redis"` (`config.py:799-801`), so `auto` resolves to `local`, and the
worker CLI prints a message and returns immediately (`cli/main.py:258-263`). The
container exits 0, the Deployment restarts it, and the pod settles into CrashLoopBackOff
with a Completed container. The chart guards only the opposite direction
(`redis-secret.yaml:4`).

### I5. The SurrealDB root password regenerates on every `helm upgrade` — blocker, M

`charts/sibyl/templates/surreal-secret.yaml:14` renders `randAlphaNum 24` with no
`lookup` guard, so each upgrade writes a new password. The Deployments checksum only the
ConfigMap (`backend-deployment.yaml:17`, `worker-deployment.yaml:18`), never the Secret,
so running pods do not roll and keep serving on the old value until an unrelated restart
picks up a password the database has never seen. Separately the generated value never
reaches SurrealDB at all: the wrapper resolves its credentials secret to
`<upstream-fullname>-root` (`charts/surrealdb/templates/_helpers.tpl:71-73`), a different
object from `<release>-surreal`. A delayed fuse — the upgrade reports success and auth
breaks later, far from the cause.

### I6. SurrealDB mounts a 100Gi PVC with no `fsGroup` against a nonroot image — blocker, S

`charts/surrealdb/values.yaml:21-26` enables persistence at 100Gi with
`path: rocksdb:/data/db` (:14), and the `surrealdb:` block (:7-31) sets no
`podSecurityContext`. Upstream defaults it to `{}` and warns twice that the official
image runs as `USER nonroot` (65532) and that provisioners creating the volume
root-owned fail with Permission denied without `fsGroup: 65532` (vendored
`surrealdb-0.5.0.tgz` → `values.yaml:120-127`, :274-278). The asymmetry is the tell: the
same file sets `fsGroup: 1000` for the wrapper's own jobs at :59-63. Blocker on affected
storage classes, no impact on ones that chown the mount.

### I7. One of 185 external action references is SHA-pinned — major, M

Verified by counting across all nine workflows plus `.github/actions/`: **185 external
`uses:` references, exactly 1 SHA-pinned** — `KSXGitHub/github-actions-deploy-aur@084b0d9b...`
at `publish.yml:354`, which handles `AUR_SSH_KEY`. The security reasoning was applied
once and never generalized.

The two that matter most, both confirmed:

- `publish.yml:162` uses `pypa/gh-action-pypi-publish@release/v1` — a **mutable branch
  ref**, not even a tag, in the job holding `id-token: write` for PyPI trusted publishing
  (:126-127).
- `release.yml:247` uses `hyperb1iss/git-iris@v2`, a mutable major tag, handed
  `secrets.ANTHROPIC_API_KEY` (:256) inside a workflow whose top-level permissions are
  `actions: write` and `contents: write` (:29-31). Own org, so lower risk, but that token
  can write to the repo and dispatch `publish.yml`.

Remaining third-party tag pins: `moonrepo/setup-toolchain@v0.6` (23x),
`docker/login-action@v4` (10x), `docker/setup-buildx-action@v4` (5x),
`docker/build-push-action@v7` (2x), `aquasecurity/trivy-action@v0.36.0` (2x),
`azure/setup-helm@v5.0.1` (2x), `softprops/action-gh-release@v3` (2x), plus
`codecov/codecov-action@v7`, `pnpm/action-setup@v6.0.10`, `astral-sh/setup-uv@v9.0.0`,
`sigstore/cosign-installer@v4.1.2`. First-party `actions/*` accounts for 132 of the rest.

Dependabot already has a `github-actions` entry (`dependabot.yml:76-90`) and updates SHA
pins while preserving the trailing version comment, so pinning adds no maintenance cost.

### I8. Dependabot has no `docker` ecosystem — major, S

`dependabot.yml` covers `uv`, `npm`, and `github-actions` only. Four Dockerfiles carry
floating base tags: `apps/api/Dockerfile:7,30` (`python:3.13-slim-bookworm`),
`apps/web/Dockerfile:8,24,53` (`node:24-alpine`), `.devcontainer/Dockerfile:1`, and
`infra/ansible/roles/sibyl/files/caddy/Dockerfile:4,7` (`caddy:2`). Base image CVEs
surface only when Trivy scans the built image during publish (`publish.yml:592`, :624).

The repo already knows this shape of bug: the comment above `dependency-audit` names it
for the npm side — "The image scan in publish only speaks once a release is already under
way, which is how a HIGH postcss advisory reached two tagged releases"
(`ci.yml:186-190`). Same gap, still open for base images.

### I9. 102 duplicated setup step instances across 727 YAML lines — major, M

Counted by parsing every workflow: `Setup moonrepo toolchain` 23, `Cache moon outputs`
18, `Cache uv` 18, `Install system dependencies` 17, `Verify toolchain` 12, `Install Node
dependencies` 8, `Cache pnpm store` 6. The pattern is already proven here —
`.github/actions/start-surrealdb/action.yml` is a composite action used at 10 call sites.
The setup block is the obvious second extraction and was never done.

### I10. Caches fragmented into 13 uv and 12 moon namespaces on identical hash inputs — major, S

Every `Cache uv` step keys on `hashFiles('**/uv.lock')` but under 13 distinct prefixes
(`uv-`, `uv-eval-`, `uv-live-`, `uv-longmemeval-`, `uv-longmemeval-compare-`,
`uv-longmemeval-local-`, `uv-longmemeval-v2-`, `uv-longmemeval-v2-official-`,
`uv-longmemeval-v2-package-`, `uv-nightly-`, `uv-okf-memory-`, `uv-release-`,
`uv-restore-`). The `restore-keys` fall back to their own prefix only (e.g.
`eval.yml:218-220`), so no namespace can read another's entry. `Cache moon outputs` has
the same shape across 12 prefixes. Thirteen near-identical copies compete for the 10GB
repository cache budget and evict each other.

### I11. Six cache keys hash a lockfile path that does not exist — major, S

`ci.yml:292,397,469,626,745` and `release.yml:83` read
`hashFiles('.prototools', 'uv.lock', 'apps/web/pnpm-lock.yaml')`. Verified:
`find . -name pnpm-lock.yaml` returns exactly one file, the root `./pnpm-lock.yaml`, and
6 workflow lines reference the `apps/web/` path. This is a pnpm workspace, so a member
lockfile has never existed.

`hashFiles` silently omits patterns matching nothing, so the key resolves from
`.prototools` and `uv.lock` alone. The cached paths include `apps/web/.next/cache`
(`ci.yml:288-290`), meaning the Next build cache is invalidated by no JavaScript
dependency change at all.

### I12. `bench-gate` is the only cacheable gate, and it gates files outside its inputs — major, S

18 of 19 `*-gate` and rehearsal tasks set `options: cache: false`; `bench-gate`
(`moon.yml:666-673`) alone does not. It is also the only one invoked repeatedly within a
single CI job against runtime files outside its declared inputs — `eval.yml:295,465,665,778`
each pass a different report under `.moon/cache/evals/`, while the declared inputs are
`tools/**/*.py`, `benchmarks/**/*.py`, `benchmarks/context_pack_cases.json`,
`benchmarks/results/ai-memory/**/*.json`, and `pyproject.toml`. The gated file is not an
input, so its content cannot influence the cache decision. Whether that produces a false
PASS depends on whether moon folds `--` passthrough args into the task hash, which was
not verified; the asymmetry against 18 siblings is verified and is reason enough.

### I13. The RC gate skips `apps/e2e` entirely — major, S

`apps/e2e/moon.yml` has no `check` and no `typecheck`, the only project with a test suite
lacking one. Root `lint` (`moon.yml:181-192`) and root `check` (:212-254) both list core,
api, cli, web, hooks, docs, skills, infra — never e2e. So `moon run :check`, the RC gate
at `release.yml:289`, `publish.yml:68`, and `publish-dogfood-images.yml:113`, never lints
or tests e2e. `ci.yml:328` runs `moon run :lint`, whose colon-prefix form does reach
`e2e:lint`, so PR lint is covered; the release path is the gap. This compounds E4 above.

### I14. No chart value reaches `SIBYL_PUBLIC_URL` — major, S

The variable appears nowhere under `charts/`. Without it `public_url` keeps its default
`http://localhost:3337` (`config.py:310-313`), and because the chart sets
`SIBYL_SERVER_HOST: "0.0.0.0"` (`values.yaml:153`) the `server_url` derivation
(`config.py:332-335`) rewrites the bind host to `localhost` and yields
`http://localhost:3334`.

Those two feed the MCP OAuth issuer and resource-server URLs (`server.py:1826-1829`), the
OIDC redirect base (`auth/oidc.py:76`), the CORS allowlist (`api/app.py:260-266`),
organization invitation links (`organization_runtime.py:166`), and password reset links
(`auth_runtime/password_reset.py:116`). The repo's own reference values file sets it by
hand and comments it as the single source of truth
(`infra/local/sibyl-values.yaml:36-38`); the shipped chart omits it. Settable through the
free-form `backend.env` map, which is the only reason this is not a blocker.

### I15. Graph embedding config is unreachable, and the exposed key looks like it covers it — major, S

The chart exposes `SIBYL_EMBEDDING_MODEL` and `SIBYL_EMBEDDING_DIMENSIONS`
(`values.yaml:158-159`), which map to the **document chunk** pair (`config.py:592-601`).
The graph pair is separate, defaults to 1024 rather than 1536 (`config.py:602-615`), and
`graph_embedding_dimensions` sizes the Surreal vector field and HNSW index at schema
definition time (`sibyl_core/backends/surreal/schema.py:52`). No chart value reaches it.

The provider side compounds it: `embedding_provider` and `graph_embedding_provider` both
default to `openai` while the chart defaults `SIBYL_LLM_PROVIDER: "anthropic"`
(`values.yaml:156`) and wires the OpenAI key `optional: true`
(`backend-deployment.yaml:82-87`). A deployment given only an Anthropic key extracts
entities and fails every embedding call.

### I16. SurrealDB runs BestEffort while every application pod has limits — major, S

`charts/surrealdb/values.yaml:7-31` sets no `surrealdb.resources` and upstream defaults to
`{}` (vendored tgz `values.yaml:201`). Backend (`charts/sibyl/values.yaml:124-130`),
frontend (:294-300), and worker (:404-410) all set limits and requests. The only stateful
component gets BestEffort QoS, making it the first eviction target under node memory
pressure.

### I17. `podSecurity.enforceRestricted` would reject the chart's own pods — major, S

`charts/sibyl/values.yaml:513` (default off). The three security contexts set
`allowPrivilegeEscalation`, `readOnlyRootFilesystem`, and `capabilities.drop` but no
`seccompProfile` (:212-224, :335-347, :417-429), which the restricted profile requires as
`RuntimeDefault` or `Localhost`; `enforce-version: "latest"` (:514) applies the newest
semantics. Separately, `charts/sibyl/templates/podsecurity.yaml:3-12` renders a
`Namespace` as a release resource, so installing into an existing namespace fails Helm
ownership validation and `helm uninstall` deletes the namespace with everything in it,
including a co-located SurrealDB PVC.

### I18. Local Tilt runs SurrealDB 2.6.5 while all 14 other pins are v3.2.3 — major, S

`Tiltfile:331-341` installs `surrealdb-helm/surrealdb` at `--version=0.5.0` with
`infra/local/surrealdb-values.yaml`, which sets no `image.tag`. The upstream chart's
`appVersion` is **2.6.5**. Every other surface pins v3.2.3:
`charts/surrealdb/values.yaml:10`, `docker-compose.yml:12`, `compose.e2e.yml:12`,
`docker-compose.prod.yml:163`, `docker-compose.quickstart.yml:186`,
`infra/ansible/roles/sibyl/defaults/main.yml:12`,
`infra/ansible/roles/sibyl/files/docker-compose.yml:22`, `apps/cli/.../docker.py:83`,
`apps/cli/.../local.py:138`.

This lands directly on a known trap: the recorded 2.x-versus-3.x divergence class where
code passes on the lenient 2.x engine and 500s live on 3.x. Validating local Kubernetes
against 2.6.5 is a hole in exactly that net.

### I19. `sync-versions-check` inputs are a hand-maintained duplicate, already missing two — major, S

`moon.yml:297-312` declares 11 of the 13 targets in `_targets()`
(`tools/release/sync_versions.py:55-109`), missing `apps/api/pyproject.toml` and
`apps/cli/pyproject.toml`. The task is cacheable with no outputs (two consecutive runs:
`9s 528ms, 3dc51e09` then `cached, 3dc51e09`), and `release.yml:76-85` restores
`.moon/cache` across runs, so a change confined to an undeclared input leaves the hash
unchanged and the gate reports a green it did not compute.

Nothing ships stale today — `release-workflow-test` covers both missing files
(`moon.yml:576-577`, `test_release_workflow.py:439-451`). The structural hole is that no
test asserts `set(_targets()) == inputs`, so the next pin added inherits a blind gate.

### I20. Hardcoded fallback image tag in the CLI, two minor releases stale — major, S

`apps/cli/src/sibyl_cli/local.py:60-70` returns `"1.0.0-rc.8"` when package metadata is
unavailable. `DEFAULT_IMAGE_TAG` feeds the generated compose files (`local.py:77`, :115)
and the `--tag` default for `sibyl docker init` (`docker.py:230`). The happy path resolves
correctly, so this fires only when the CLI runs without installed metadata — at which
point a 1.2.0 CLI silently writes a compose file pulling `sibyl-api:1.0.0-rc.8`. No test
exercises the branch and `sync_versions.py` does not target the file.

### I21. Minor infra findings

- **`publish.yml` and `publish-dogfood-images.yml` have no top-level `permissions:`.**
  Every job declares its own (12 of 12 and 4 of 4, all tightly scoped), but a newly added
  job silently inherits the repository default token instead of default-deny. Every other
  workflow sets a top-level `contents: read`.
- **`longmemeval-v2.yml` duplicates a 33-item path list verbatim** for `push` (:6-38) and
  `pull_request` (:42-74), byte-identical after strip. YAML anchors collapse it.
- **Five checks are cacheable while all 18 sibling gates are not**: `bench-gate`
  (`moon.yml:666`), `sync-versions-check` (:297), `inventory-check` (:323),
  `storage-access-check` (:793), `fmt-check` (:172). `sync-versions` (:291) is a pure
  side-effect writer with no declared outputs and caching on.
- **`infra:lint` and `skills:lint` shell out to `npx prettier`** (`infra/moon.yml:7`,
  `skills/moon.yml:22`) inside the RC gate. Prettier is a root devDependency at `^3.9.6`,
  so `npx` resolves it locally when present and silently downloads an arbitrary version
  from the network when not. `pnpm exec prettier` is the deterministic form.
- **`skills/moon.yml:24-28` declares `**/*.yaml` in `sources`** but the lint command checks
  only `'**/*.md' '**/*.yml'`. Latent — no `.yaml` exists under `skills/` today.
- **The worker has no probes** while backend (`backend-deployment.yaml:118-121`) and
  frontend (`frontend-deployment.yaml:81-84`) both do. A wedged arq worker stays Ready
  forever, and its HPA scales on CPU and memory (`worker-hpa.yaml:15-31`), so stuck reads
  as underutilized and scales down.
- **SurrealDB chart jobs inherit empty resources** (`charts/surrealdb/values.yaml:70`,
  consumed by `bootstrap-job.yaml:68-69`, `export-cronjob.yaml:86-87`,
  `snapshot-cronjob.yaml:86-87`, `restore-drill-cronjob.yaml:159-160`). The export and
  restore-drill jobs stream a full database export with no memory ceiling on a node also
  running the database.
- **Wrapper `appVersion` lacks the `v` prefix its own fallback needs.**
  `charts/surrealdb/Chart.yaml:6` is `"3.2.3"` and `_helpers.tpl:115` falls back to
  `.Chart.AppVersion`, producing `surrealdb/surrealdb:3.2.3`; upstream publishes
  v-prefixed tags, so that path is an ImagePullBackOff. Latent only because
  `values.yaml:10` sets `v3.2.3` explicitly.
- **`okf-memory-changelog.yml` guards one artifact upload and not its sibling** (:94 gated
  on `hashFiles(...) != ''`, :101 has `if-no-files-found: error` and no guard). Whether
  the weekly cron fails depends on an unreadable repo secret; the intra-workflow
  inconsistency is verified.
- **`publish-dogfood-images.yml` is `workflow_dispatch`-only** and nothing dispatches it.
  A legitimate manual tool worth confirming is still wanted.
- **Nothing tests `sync_versions.py` itself.** `"moon run sync-versions"` and
  `"moon run sync-versions-check"` are absent from `RELEASE_WORKFLOW_REQUIRED_FRAGMENTS`
  (`test_release_workflow.py:32-68`), so deleting `release.yml:171-172` still passes the
  allowlist proof. No test asserts every target exists, and the docstring at
  `sync_versions.py:36-39` claims a `_pep440` parity check that no test performs.
- **The SurrealDB version is duplicated across 14 sites with no sync tool** (I18's list
  plus `charts/surrealdb/Chart.yaml:6`, `docs/deployment/ansible.md:12`,
  `docs/deployment/docker-compose.md:62,90,214`). Only the two CLI sites have tests
  pinning the literal. Extending `sync_versions.py` with a second source of truth folds it
  into the existing gate. (M)
- **`infra/local/tidb-cluster.yaml:17` pins `alpine:3.16.0`** (2022-era) as the TiKV helper
  image, in a path documented as the local backing store.

### Checked and clean

No `pull_request_target` anywhere. No secret is echoed, written to `$GITHUB_OUTPUT`, or
used in an `if:` expression. Every top-level `permissions:` block is `contents: read`
except `docs.yml` (pages deploy, correctly scoped) and `release.yml` (`actions: write` for
the publish dispatch, `contents: write` for the tag). All 16 `workflow_dispatch` inputs in
`eval.yml` are defined and referenced. `infra/local/secrets.yaml` is gitignored.

Version pins are currently in sync: `sync_versions.py --check` exits 0 with "All
deployment pins match VERSION (1.2.0)", all 13 targets exist, all 17 patterns match. The
three published Python packages carry no version literal (`dynamic = ["version"]` reading
the root `VERSION`), the web version is injected at build time
(`apps/web/next.config.ts:13,43,61`), and no Dockerfile holds a literal. The chart
subchart chain has no drift: `Chart.yaml` 0.5.0, `Chart.lock` 0.5.0, vendored tgz 0.5.0.
Every chart env var maps to a live setting. No image in either chart uses `:latest`.
`.prototools` (moon 2.2.6, python 3.13, node 24, pnpm 11.5.2, uv 0.9) matches what CI
installs.

Not run: no `helm template`, `helm lint`, or cluster operation, so every chart rendering
claim comes from reading templates rather than rendered output. No workflow was executed.
Moon's passthrough-argument hashing was not tested, which is why I12 is scoped to the
asymmetry rather than a claimed false PASS.
