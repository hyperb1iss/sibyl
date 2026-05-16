# SurrealDB Phase 1 — Burn-down Findings

Captured during the Sibyl task burn-down session on `feat/surrealdb-driver-phase1` on 2026-04-20. We
pushed the live API through roughly 140 archive and complete operations while SurrealDB was the
active graph backend.

Updated after the server-mode runtime and follow-up hardening work: the original blocker and the
known follow-up tail are now resolved. Keep this document as historical context for why the current
runtime defaults matter, not as the active Surreal roadmap.

## TL;DR

The original primary blocker was `api` and `worker` sharing an embedded `surrealkv://` store from
separate OS processes. Local Surreal development now runs through a SurrealDB server at
`ws://127.0.0.1:8000/rpc`, with local coordination avoiding the old worker split unless Redis
coordination is explicitly enabled.

Additional follow-ups from the burn-down have also landed:

- bulk archive reports per-ID failures and accepts UUID task IDs from stdin
- mixed graph/document search uses rank fusion so documents do not starve graph entities
- Surreal graph runtime adapters fail closed before legacy Cypher fallbacks
- refresh-token malformed org claims return controlled auth failures
- `stats`, missing-entity deletes, debug query dialect checks, skill examples, and graph UI
  stability fixes are no longer active blockers

---

## Resolved Primary Blocker

### Shared embedded `surrealkv://` store across `api` and `worker`

#### What we observed

Server logs during the burn-down repeatedly showed this pattern:

```text
api     | 00:00:01 | Entity updated successfully entity_id=project_05eb5c8c782a
api     | 00:00:01 | Project progress updated total=752 done=443 doing=4
worker  | 00:00:01 | Connecting to SurrealDB url=surrealkv:///Users/bliss/dev/sibyl/.moon/cache/surreal-rehearsal-cli-20260419-234456
worker  | 00:00:03 | Entity created via EntityNode.save entity_id=episode_task_740a4425163d
api     | 00:00:02 | Failed to update entity entity_id=project_05eb5c8c782a error=node project_05eb5c8c782a not found
```

The `api` process writes a node, logs success, then fails to re-read that same node seconds later.
In the gap, the arq `worker` has opened its own embedded connection to the same file and written an
episode.

#### Why this happens

- `surrealkv://` is embedded SurrealDB storage.
- Embedded storage is appropriate for a single process, not a daemon plus a worker.
- Two processes writing to the same file produce silent stale reads rather than clean transactional
  failures.
- The failure mode is especially nasty because the graph looks partially alive: some old nodes still
  resolve while recently touched nodes disappear.

This aligns with SurrealDB's own embedded guidance: embedded mode is single-process, while
multi-process access should go through a server instance over the network transport.

#### Post-session state we captured

After the heavy write batch, entity visibility split by freshness:

| Entity                        | Recently written? | Direct fetch |
| ----------------------------- | ----------------- | ------------ |
| `task_7ac910ccf4b2`           | No                | ✓ 200        |
| `task_740a4425163d`           | Yes               | ✗ 404        |
| `project_05eb5c8c782a`        | Yes               | ✗ 404        |
| `epic_8b4ad0b571c6`           | No                | ✗ 404        |
| `task list` / `explore` scans | n/a               | empty        |

That matches the user-visible behavior we saw during the burn-down:

- `archive_task` reporting 500 after the task status write had already landed
- `task list` and `explore` going empty for a stretch
- `get_by_uuid` falling through its miss chain for entities that had just existed

A restart cleared the bad state, which is exactly what you would expect from a broken embedded
reader snapshot rather than a durable data-loss event.

#### Important clarification about the store path

The earlier draft treated the rehearsal-backed store path as possibly accidental. That no longer
looks accurate. The current dev runtime intentionally picks the newest rehearsal snapshot under
`.moon/cache/` for the Surreal dev runtime.

That means the bug is not "the wrong file got picked." The bug is that both `api` and `worker` are
embedding the same file-backed store at all.

#### Resolution

The local development runtime now starts a SurrealDB server and points the API at the WebSocket RPC
endpoint. The default `moon run dev` flow sets `SIBYL_STORE=surreal`, `SIBYL_AUTH_STORE=surreal`,
`SIBYL_COORDINATION_BACKEND=local`, and `SIBYL_SURREAL_URL=ws://127.0.0.1:8000/rpc`.

Embedded Surreal storage remains useful for tests and single-process tools. It is no longer the
default multi-process dev runtime.

---

## Active Follow-up Bugs

No active bugs remain from the original burn-down list. The next work belongs in the Surreal cutover
roadmap: seam cleanup, rehearsal, backup/restore confidence, and removing the last Postgres safety
rails once a fully-Surreal run is proven.

---

## Already Fixed During Burn-down

These should move out of the active blocker list.

### Shared embedded Surreal dev runtime replaced with server mode

The old `api` plus worker embedded-store failure is resolved by the current server-mode local
runtime.

### Bulk archive reports per-ID failures

`sibyl task archive --stdin` now reports full per-ID results in JSON mode, lists failed IDs in human
output, and accepts UUID task IDs from stdin.

### Mixed search fuses graph and document rankings

`sibyl search` now rank-fuses graph and document results instead of sorting raw scores across
stores, so graph entities can appear alongside ingested docs.

### Surreal graph adapters fail closed before Cypher fallbacks

Runtime graph relationship adapters now raise controlled errors instead of issuing legacy Cypher
when a Surreal driver is active but native edge operations are unavailable. Count paths use direct
SurrealQL `SELECT` statements.

### Refresh-token malformed org claims return controlled auth failures

Malformed `org` claims in browser/API refresh tokens return `401 Invalid token claims`; MCP OAuth
refresh exchange returns `invalid_grant`.

### `/api/admin/stats` is no longer the broken zero-everything path

The earlier draft flagged `sibyl stats` as an active bug. That was true during the rough porting
window, but it is no longer the current state. The stats route now reads from the Surreal-backed
stats path instead of the stale raw-Cypher behavior that produced zeroes.

### `DELETE /api/entities/{id}` now returns 404 for missing entities

The missing-entity delete path was returning a 500 in the earlier draft. That is now fixed and
should not stay in the active list.

### Graph page click spam and layout instability were fixed separately

The graph UI issues we hit while leaning on the system hard were real, but they no longer belong in
the active Surreal Phase 1 blocker pile:

- node clicks now use lighter entity reads instead of hydrating giant related bundles
- the detail panel derives neighbors from the visible graph snapshot
- the force-graph remount path reheats correctly instead of falling into the half-laid-out starfield

Those were important quality fixes, but they are web-layer follow-ups, not Surreal driver blockers.

### Surreal debug queries and Sibyl skill examples now use SurrealQL

The admin debug endpoint now rejects legacy Cypher entrypoints like `MATCH` in Surreal mode before
they hit the database, while keeping read-only Cypher available for the legacy runtime. The CLI and
skill examples now point agents at read-only SurrealQL.

### Sibyl skill docs no longer mention `sibyl logs search`

The current skill docs point agents at `sibyl logs tail` plus normal shell search instead of the
nonexistent `sibyl logs search` command.

### Project progress updates are best-effort

`_update_project_progress` now has regression coverage proving that completion and archive flows
still return the updated task when project rollup reads fail after the primary status write lands.

### Missing entity reads skip impossible fallbacks

Typed graph IDs like `task_*`, `project_*`, and `epic_*` now skip the EpisodicNode fallback after an
EntityNode miss, and the HTTP entity route only checks document chunks for UUID-shaped IDs.

---

## Burn-down Outcome

Even with the storage bug in play, the burn-down made real progress:

- archived: 138 agent-harness tasks
- completed with learnings: 1
- remaining keep-list: 169 tasks, already pre-categorized

The survivors were already clustered enough to make the next pass straightforward once the runtime
is stable again:

- docs
- reasoning module and Memory Architecture work
- epic entity support
- autonomous writeback and workflow hooks
- SurrealDB rewrite waves
- model cleanup
- RBAC, teams, and MCP resilience
- crawler and extraction work
- search and query-generation follow-ups

The session was messy, but not wasted. The remaining work looks tractable.

---

## Recommended Next Steps

1. Run a fully-Surreal rehearsal with Postgres stopped.
2. Verify login/logout/refresh, API keys, org/project flows, crawler writes, RAG/search, backup, and
   restore.
3. Confirm no normal request, startup, health, or worker path opens a SQL session in fully-Surreal
   mode.
4. Use the green rehearsal as the deletion gate for remaining legacy auth/content safety rails.

---

## Minimal Repro for the Main Blocker

To reproduce the embedded-store corruption without repeating the entire burn-down:

1. Start `sibyld serve` and the arq worker against the same `surrealkv://` store.
2. Complete a task through the CLI so the `api` writes first and the `worker` writes an episode
   second.
3. Immediately run another complete or archive operation.
4. Watch `_update_project_progress` fail with `NodeNotFoundError` for a project that was just
   written.
5. Check `task list`, `explore`, or direct entity reads for the recently touched IDs.

A restart clears the view. The same flow against a real SurrealDB server instance should not
reproduce the corruption.
