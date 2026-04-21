# SurrealDB Phase 1 — Burn-down Findings

Captured during the Sibyl task burn-down session on `feat/surrealdb-driver-phase1` on 2026-04-20.
We pushed the live API through roughly 140 archive and complete operations while SurrealDB was the
active graph backend. The result is encouraging: there appears to be one real Phase 1 blocker, a
small follow-up tail, and a couple of items that are already fixed.

## TL;DR

The primary blocker is still the same: `api` and `worker` are both opening the same embedded
`surrealkv://` store from separate OS processes. Under concurrent writes, readers drift into stale
views of the graph and recently written entities start returning 404 or empty scans.

The rest of the tail is smaller than the first draft made it look:

- One hardening fix should still happen even after the storage fix: `_update_project_progress`
  should not be able to turn a successful archive into a 500.
- A few remaining tools and UX paths still need cleanup.
- `stats` and missing-entity delete handling are already fixed and should no longer be tracked as
  active blockers.

---

## Confirmed Active Blocker

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

| Entity | Recently written? | Direct fetch |
| --- | --- | --- |
| `task_7ac910ccf4b2` | No | ✓ 200 |
| `task_740a4425163d` | Yes | ✗ 404 |
| `project_05eb5c8c782a` | Yes | ✗ 404 |
| `epic_8b4ad0b571c6` | No | ✗ 404 |
| `task list` / `explore` scans | n/a | empty |

That matches the user-visible behavior we saw during the burn-down:

- `archive_task` reporting 500 after the task status write had already landed
- `task list` and `explore` going empty for a stretch
- `get_by_uuid` falling through its miss chain for entities that had just existed

A restart cleared the bad state, which is exactly what you would expect from a broken embedded
reader snapshot rather than a durable data-loss event.

#### Important clarification about the store path

The earlier draft treated the rehearsal-backed store path as possibly accidental. That no longer
looks accurate. The current dev runtime intentionally picks the newest rehearsal snapshot under
`.moon/cache/` for `dev-surreal`.

That means the bug is not "the wrong file got picked." The bug is that both `api` and `worker` are
embedding the same file-backed store at all.

#### Recommended fix

Move SurrealDB to server mode and have both `api` and `worker` connect over `ws://` or `http://`.
This is the highest-leverage remaining change in Phase 1.

Rough shape:

```yaml
surrealdb:
  image: surrealdb/surrealdb:latest
  command: start --user root --pass root rocksdb:///data/sibyl.db
  volumes:
    - surreal-data:/data
  ports:
    - "8000:8000"
```

The driver work is already in good shape for remote mode. What remains is runtime and infra wiring:

- add a SurrealDB service to local orchestration
- point `api` and `worker` at the server URL
- update the `moon` and dev-shell entrypoints accordingly

This is bigger than an env var flip, but it is still a contained Phase 1 change.

---

## Active Follow-up Bugs

These still matter, but they are not all the same class of blocker.

### 1. `archive_task` can still report failure after a successful archive

**Where:** `apps/api/src/sibyl/api/routes/tasks.py` →
`packages/python/sibyl-core/src/sibyl_core/tasks/workflow.py` →
`_update_project_progress`

The root cause bug made this show up constantly, but the hardening gap is independent: rollup
cache updates should not be able to turn the primary archive operation into a 500.

What still needs to happen:

1. make `_update_project_progress` best-effort
2. log failures clearly
3. preserve success for the underlying archive when the status write already committed

This keeps future regressions from creating the same partial-success lie even after server mode.

### 2. `get_by_uuid` misses are too expensive and too fuzzy

**Where:** `packages/python/sibyl-core/src/sibyl_core/graph/surreal/ops/entity_node_ops.py`

Under pressure, the miss path becomes noisy:

- try `EntityNode`
- fall back to `EpisodicNode`
- fall back to document-chunk lookup

For real misses, that is a lot of wasted work. For flaky embedded-store reads, it also turns a
stale read into a slow stale read.

Recommended follow-up:

- short-circuit obvious non-document IDs like task, project, and epic IDs
- keep the fallback chain for the cases where it is actually useful

### 3. `sibyl debug schema` and `sibyl debug query` still send raw Cypher

**Where:** `packages/python/sibyl-core/src/sibyl_core/graph/client.py` and the Surreal driver path

These debug paths are still forwarding `MATCH ...` queries that SurrealDB rejects at parse time.
This is now the main remaining raw-Cypher island.

Recommended follow-up:

- port schema/debug helpers to SurrealQL
- or route them through the graph manager seams instead of raw query passthrough

### 4. Sibyl skill docs still mention `sibyl logs search`

The skill docs reference a command that does not exist. The actual supported commands are
`sibyl logs tail` and `sibyl logs stats`.

Recommended follow-up:

- remove the bad doc reference
- or add a real `logs search` command if we want that affordance

### 5. Bulk archive mode still lacks per-ID failure detail

**Where:** CLI archive bulk mode with `--stdin`

The archive summary reports success and failure counts, but not which task IDs failed. That makes
retries and postmortems annoying, especially when the underlying failure mode is partial success.

Recommended follow-up:

- add `--json` output for per-ID results
- or print the failed IDs inline in human output

---

## Already Fixed During Burn-down

These should move out of the active blocker list.

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

1. Move local SurrealDB to server mode and wire `api` plus `worker` to it.
2. Make `_update_project_progress` best-effort so cache updates cannot poison primary task writes.
3. Port the remaining raw-Cypher debug helpers to SurrealQL.
4. Tighten the `get_by_uuid` miss path so obvious misses do less work.
5. Fix Sibyl doc drift around `sibyl logs search`.
6. Add per-ID output for bulk archive results.
7. Re-run the burn-down after server mode and verify list, explore, graph, and archive behavior in
   one pass.

If we do that in order, Phase 1 stops looking like "death by a thousand bugs" and starts looking
like what it probably is: one infra/runtime blocker plus a small cleanup tail.

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
