# Tech-debt audit: sibyl-core graph / persistence / models

Scope: `packages/python/sibyl-core/src/sibyl_core/` — `models/`, `backends/surreal/`,
`services/graph*.py`, `storage/`, `migrate/`, and multi-tenancy plumbing. Excluded by lane
assignment: retrieval/ranking/ai/extraction, `apps/api`, `apps/web`. Every claim below was read in
the source at the cited line. Commit `5388d986`, v1.2.0.

Two historical facts named in the brief were re-checked and one has changed:

- `entity.updated_at` is **no longer** `option<string>`. Graph schema migration v5
  (`backends/surreal/schema.py:264-293`, `entity_updated_at_datetime`) converted it to
  `option<datetime>`, and the current field definition at `schema.py:87` is `option<datetime>`. The
  lexicographic-ORDER-BY hazard is retired. A runtime fallback for un-migrated namespaces still
  exists (finding G7).
- Dead Graphiti tables confirmed: `REMOVED_GRAPH_TABLES = ("community", "saga")` and
  `REMOVED_GRAPH_EDGES = ("has_episode", "next_episode", "has_member")` at `schema.py:575-577`,
  dropped by migration v4 and asserted empty before any migration (`schema.py:897-910`). That
  cleanup is done. The `episode` table and `mentions` edge, however, are still fully alive in the
  schema (finding S4).
- Namespace-per-org isolation confirmed at `services/graph_client.py:117-122`.

---

## A. Findings that would fight a memory-representation redesign

### A1. Every entity stores its metadata three times, and one copy goes stale by design

`services/graph.py:3017-3025`, `graph.py:1982-2006`, `graph.py:3071-3081`

`_entity_record` writes the same facts into three places on every full write:

```python
attributes: dict[str, object] = {
    **metadata,                      # (1) flattened bag
    "description": entity.description or "",
    "updated_at": updated_at,
    "_direct_insert": True,
    "metadata": json.dumps(metadata),   # (2) JSON snapshot of the same bag
    "entity_type": entity.entity_type.value,
}
```

plus (3) ~20 promoted columns (`project_id`, `memory_scope`, `status`, `tags`, `retrieval_count`, …)
on the record itself.

The partial-update path (`_entity_update_patch`, `graph.py:3071`) builds an `attributes` patch that
never contains a `metadata` key, and the write is `UPDATE entity MERGE $patch` (`graph.py:562`). A
deep merge leaves `attributes.metadata` untouched, so from the first partial update onward the JSON
snapshot is a stale copy of pre-update state. The read path (`entity_from_surreal_row`,
`graph.py:2021`) layers `snapshot | flattened`, so the flattened copy wins for any key present in
both — but a key an update **removed** resurrects from the snapshot. The code documents this and
mitigates it with a hand-maintained allowlist of five keys (`_SNAPSHOT_SHADOWED_METADATA_KEYS`,
`graph.py:1990-1998`).

Why it matters: this is the single largest structural obstacle to a 1.3 memory representation. Any
new metadata key with delete-semantics silently inherits the resurrection bug unless someone
remembers to add it to a frozenset in a 3,475-line module. Storage is roughly tripled for metadata,
and there is no single authority for what a row means.

**Size L · Severity blocker (for the redesign; major as-is)**

### A2. The write path is a blind full replace with per-field absence exceptions bolted on

`services/graph.py:229-279` (`_ENTITY_BULK_UPSERT_QUERY`)

`INSERT INTO entity $rows ON DUPLICATE KEY UPDATE ... attributes = $input.attributes` replaces the
whole attributes bag with no revision guard and no `WHERE`. Two fields got rescued from that after
it caused real data loss, each with its own inline special case and a paragraph of comment:

- `memory_scope` (`graph.py:239-240`, `263-264`) — preserved on absence, cleared only by a sentinel
  `CLEAR_MEMORY_SCOPE` string.
- `retrieval_keys` / `retrieval_keys_normalized` (`graph.py:268-269`) — same rule.
- `created_by` (`graph.py:249`) — `created_by ?? $input.created_by`, first-writer-wins.

Every other metadata key still gets wiped by a reprojection or restore that rebuilds an `Entity`
without carrying it forward. `EntityManager.create` / `create_direct` (`graph.py:450`, `331`) are
therefore destructive upserts, not creates.

Why it matters: the absence-vs-null distinction is being encoded ad hoc, one field at a time, in a
SurrealQL string. A redesign needs it as a property of the model.

**Size M · Severity major**

### A3. Typed Entity subclasses are write-only; only two of twelve rehydrate

`models/entities.py:147-267`, `services/graph.py:2213-2218`

`Pattern`, `Rule`, `Template`, `Tool`, `Language`, `Topic`, `Episode`, `KnowledgeSource`,
`ConfigFile`, `SlashCommand`, `Procedure` all declare typed fields. On write, `_entity_metadata`
(`graph.py:3424-3444`) `model_dump`s every field not in its exclude-set into the untyped `metadata`
bag. On read, `_coerce_native_entity` reconstructs only `Task` and `Procedure`:

```python
def _coerce_native_entity(entity: Entity) -> Entity:
    if entity.entity_type == EntityType.TASK:
        return _entity_to_task(entity)
    if entity.entity_type == EntityType.PROCEDURE:
        return _entity_to_procedure(entity)
    return entity
```

So reading a `Pattern` yields a bare `Entity` whose `category` / `languages` / `confidence` live as
untyped dict entries. The class hierarchy is decoration.

**Size M · Severity major**

### A4. The relationship model cannot express the temporal validity the schema already stores

`models/entities.py:269-278` vs `backends/surreal/schema.py:190-193`

The `relates_to` table defines `created_at`, `expired_at`, `valid_at`, `invalid_at`. The
`Relationship` pydantic model has only `id`, `relationship_type`, `source_id`, `target_id`,
`weight`, `metadata`, `created_at`. `_relationship_record` (`graph.py:3346-3348`) digs the three
temporal columns out of the untyped metadata bag, accepting `valid_from`/`valid_to` aliases.

The model also has no `organization_id`, no `revision`, and no `updated_at` — so edges have no
optimistic-concurrency control at all, while entities do (`graph.py:565`, `models/entities.py:139`).

Why it matters: "temporal validity" is named as a 1.3 goal. The storage layer already supports it;
the typed model is the thing blocking it, and it is a small fix.

**Size S · Severity major**

### A5. Relation type is an open string in the database

`backends/surreal/schema.py:182` vs `schema.py:385-388`

`entity_type` got a DB-level enum assertion in migration v8, rendered from `EntityType`
(`GRAPH_ENUM_ASSERTION_DEFINITIONS`). `relates_to.name` is `TYPE string` with no `ASSERT`, so any
relation name can be written regardless of `RelationshipType`'s 40 members. Note also the comment at
`schema.py:677-678`: widening the entity enum requires minting a new migration version to replay the
assertion, so a typed-relations effort inherits a per-release migration tax.

**Size S · Severity minor (correctness) / major (as a redesign constraint)**

### A6. Timestamps are fabricated when absent

`services/graph.py:2100-2103`

```python
created_at=_row_datetime(normalized_row.get("created_at") or metadata.get("created_at"))
or datetime.now(UTC),
updated_at=_row_datetime(normalized_row.get("updated_at") or metadata.get("updated_at"))
or datetime.now(UTC),
```

A row with a missing or unparseable timestamp reads back as "just now". For a system whose ranking
and recency logic run on these fields, and whose next release wants temporal validity, a silently
invented timestamp is worse than a null.

**Size S · Severity major**

### A7. Three incompatible datetime conventions in one lane

`backends/surreal/records.py:19-26` (naive UTC), `services/graph.py:555, 2896, 2981`
(`datetime.now(UTC)`, aware), `services/graph.py:3410-3418` (`_metadata_datetime` returns whatever
`fromisoformat` produces, naive or aware, unnormalized).

`records.utcnow()` deliberately strips tzinfo with a docstring explaining that mixing aware and
naive raises on comparison — and then `graph.py` writes aware datetimes into the same columns.
`_metadata_datetime` passes both through untouched.

**Size M · Severity major**

---

## B. Schema and migration integrity

### S1. The graph plane is the one plane with no schema-invariant safety net

`backends/surreal/schema_helpers.py:44-64`, `backends/surreal/schema_invariants.py:1-9`,
`apps/api/src/sibyl/cli/db.py:706-756`

`execute_schema_statement` swallows a failed `DEFINE INDEX ... UNIQUE` when the table already holds
duplicates, logging only fingerprints:

```python
except Exception as exc:
    if not is_duplicate_unique_index_error(statement, exc):
        raise
    fields = {"schema_scope": scope, "statement_hash": fingerprint_text(statement),
              "error_hash": fingerprint_text(str(exc)), ...}
    log.warning("surreal_schema_unique_index_skipped", **fields)
```

`apply_schema_migrations` then records the migration as applied. `schema_invariants.py` exists
specifically to catch this, and says so in its module docstring. But:

- `auth_schema.py:684` builds `auth_schema_invariant_plan()`.
- `content_schema.py:737` builds `content_schema_invariant_plan()`.
- **`backends/surreal/schema.py` builds no graph plan at all.** No `graph_schema_invariant_plan`,
  and `bootstrap_schema` never calls `ensure_schema_invariants`.
- `sibyld db init` (`cli/db.py:713`, "Apply pending auth and content schema migrations") runs
  `_init_plane` for auth and content only. The remediation loop — `db duplicates` → `--collapse` →
  re-`init` — is likewise unreachable for graph.

The unique indexes at stake are `idx_entity_uuid` (`schema.py:110`) and `idx_relates_uuid`
(`schema.py:195`), i.e. the identity of every memory and every edge. A namespace that once held
duplicate uuids loses entity uniqueness permanently, the log line carries no readable detail, and
nothing reports it.

**Size M · Severity blocker**

### S2. Graph migrations run lazily, per-org, at request time, with no cross-process lock

`services/graph_client.py:47-49, 102-115`, `backends/surreal/schema_version.py:137-173`

`prepare_graph_schema` guards with a **process-local** `asyncio.Lock` and an in-memory
`_prepared_groups` set. There is no advisory lock in the database. Two sibyld replicas (or api +
worker) touching a cold org namespace concurrently both run `apply_schema_migrations`, which
includes `DEFINE INDEX ... CONCURRENTLY` statements (`schema.py:281-292`, `346-381`, `537`) and
whole-table `UPDATE` backfills (`schema.py:245-261`, `442-491`).

Related: migrations are not transactional per version. A failure mid-migration leaves statements
partially applied and the version unrecorded, so the next attempt replays from the first statement.
Most statements are idempotent by construction (`IF NOT EXISTS` / `OVERWRITE` / `UPDATE ... WHERE`),
but nothing enforces that property for new migrations.

Also: eviction from the client LRU calls `mark_graph_schema_dirty` (`graph_client.py:84`), so the
next touch of that org re-runs the entire bootstrap path. Past 64 active orgs
(`surreal_graph_client_cache_size`), schema bootstrap thrashes.

**Size M · Severity major**

### S3. The SQL under test is not the SQL that ships

`backends/surreal/schema.py:970-985`, `packages/python/sibyl-core/pyproject.toml:54`

```python
def render_surreal_compatible_sql(sql: str, *, url: str) -> str:
    rendered = render_fulltext_compatible_sql(sql, url=url)   # FULLTEXT -> SEARCH for embedded
    if not url.startswith(_EMBEDDED_SURREAL_SCHEMES):
        rendered = (rendered.replace("type::is::string", "type::is_string")
                    .replace("type::is::number", "type::is_number")
                    .replace("type::is::datetime", "type::is_datetime")
                    .replace("string::is::datetime", "string::is_datetime"))
    return rendered
```

The SDK is pinned `surrealdb>=2.0.0,<3.0`, so `memory://` in tests runs the bundled 2.x engine;
every deployment pins server `v3.2.3` (`docker-compose.yml:12`,
`infra/ansible/roles/sibyl/defaults/main.yml:12`). The bridge is four hardcoded string replacements
plus one for `FULLTEXT`/`SEARCH`. Any new schema statement using a namespaced builtin outside that
list ships the wrong dialect to production and passes every unit test. `tests/test_graph.py:1535`
acknowledges the class of gap in a comment.

**Size M · Severity major**

### S4. The `episode` table and `mentions` edge are restore-only but fully maintained

`backends/surreal/schema.py:142-158, 208-221, 232-261, 340-344, 374-381`;
`services/graph.py:462-465`; `migrate/legacy_graph_archive.py:1-16`

The only writer of the `episode` table is the archive-restore path (`tools/admin.py:890`,
`_save_native_episode`), and the only writer of `mentions` is the same restore
(`tools/admin.py:942-945`). Nothing in the live memory loop touches either. Note
`EntityType.EPISODE` rows live in the **`entity`** table and are unrelated
(`tasks/distillation.py:20-76`).

Meanwhile every org namespace pays for: 12 field definitions, 3 `episode` indexes and 5 `mentions`
indexes, `RELATION_ENDPOINT_BACKFILL_DEFINITIONS` running a full `UPDATE mentions` on migrations v3
and v6, four `mentions` index rebuilds `CONCURRENTLY` in v7, and a `DELETE FROM mentions` statement
inside every single entity delete transaction (`graph.py:462-465`).

`migrate/legacy_graph_archive.py` (335 lines) exists solely to shape Graphiti-era episode payloads
for that restore path.

**Size M · Severity major** (removing it is the cheapest large win before a redesign)

### S5. Schema definitions and data migrations are interleaved in the bootstrap blocks

`backends/surreal/schema.py:132-135`, `162-175`, `418-439`

`NODE_DEFINITIONS` — nominally the DDL block — ends with a whole-table
`UPDATE entity SET description = description ?? attributes.description ...`, and
`RELATION_EDGE_CLEANUP_DEFINITIONS` runs
`DELETE FROM relates_to WHERE in NOT IN (SELECT VALUE id FROM entity)` — an anti-join over the whole
graph — on every fresh bootstrap. Fresh namespaces run data repair against zero rows; the mixing
makes it impossible to tell definition from repair when reading the file.

**Size S · Severity minor**

### S6. Field types ping-pong between strict and optional across migrations

`backends/surreal/schema.py:407-412, 414-440, 493-497, 499-507, 540-561, 563-567`

`ENTITY_REQUIRED_FIELD_OPTIONAL_DEFINITIONS` loosens `revision`, `retrieval_count`,
`citation_count`, `misled_count` back to `option<int>`, and is prepended to migrations v11, v12, v13
and embedded in v14 and v15, each of which re-tightens them. Two further migrations
(`entity_required_field_repair` v14, `entity_schema_drift_repair` v15) exist purely to repair what
earlier migrations left inconsistent. The migration list is carrying its own bug history as
permanent replayable state.

**Size M · Severity minor** (works; costs every fresh namespace and every reader)

### S7. Statement splitting is line-oriented and would break on multi-line string literals

`backends/surreal/schema_helpers.py:14-30`

`split_statements` splits on any line ending in `;`, ignoring quoting. Every current statement is
safe, but the function is the sole gateway for all three schema planes and a future
`DEFINE ... COMMENT 'text; with semicolon'` would silently split into two broken statements. Same
class in `backends/surreal/connection.py:62, 72` (`query.split(";")` for retry classification).

**Size S · Severity minor**

---

## C. Duplication and dead code

### D1. Three divergent SurrealDB result normalizers, one claiming to be canonical

`backends/surreal/records.py:1-7, 29-54`; `services/graph.py:2501-2541`;
`services/surreal_content.py:643, 653, 671`

`records.py` opens with:

> "These were historically copy-pasted into every persistence module. Keeping a single source of
> truth closes a real drift hazard… Every persistence module imports from here."

That is no longer true. `records.normalize_record` **drops** `id` (`records.py:35`).
`graph.py:_normalize_record` **preserves** it as `record_id` and synthesizes a `uuid` from it
(`graph.py:2507-2517`). `surreal_content.py` carries a third pair plus a fourth variant
`_normalize_records_preserving_id`. The envelope detection differs between them
(`graph.py:2526-2533` handles a shape `records.py:44` does not). The exact drift hazard the module
was written to close is back, across three copies.

Smaller instances of the same: `_metadata_str` is defined four times (`graph.py:3359`,
`services/memory.py:2398`, `services/reflection.py:912`, `services/memory_autonomy.py:274`);
`_optional_int` twice (`graph.py:2332`, `schema_version.py:254`); datetime coercion in
`records.py:110`, `surreal_content.py:822`, and `graph.py:2349`/`3410`.

**Size M · Severity major**

### D2. `sibyl_core/storage/` is an unimplemented abstraction with zero production consumers

`storage/__init__.py`, `storage/contracts.py:11-78`, `storage/models.py`

`EntityStore`, `RelationshipStore`, `SearchIndex`, `GraphStore`, `EntityPatch`, `EntityBundle`,
`GraphStats`, `SearchHit`, `Page`, `SearchFilters`, `RelationshipPatch` — 177 lines of
"backend-agnostic graph storage contracts". The only importer anywhere in the repo is
`tests/test_storage_contracts.py`, which constructs `FakeEntityStore` / `FakeGraphStore` and asserts
the Protocols are satisfiable. No production class implements any of them; the real seam is
`EntityManager` in a 3,475-line concrete module.

Why it matters beyond the dead lines: this is a decoy. Anyone planning 1.3 who greps for the storage
abstraction finds a clean Protocol package and reasonably concludes that is the seam to build
against.

**Size S to delete · Severity major** (as a redesign trap; minor as code)

### D3. `graph_runtime.py` carries a duplicate runtime dataclass, an unreachable branch, and a Cypher-era guard

`services/graph_runtime.py:36-42, 49-51, 101-132`

- `ActiveGraphRuntime` (36-42) duplicates `GraphRuntime` (`graph.py:310-313`) field-for-field;
  `get_graph_runtime` builds one from the other.
- `count_entities_by_type` (93-154) has three paths. Path 1 uses
  `getattr(entity_manager, "count_by_type")`, which `EntityManager` always has (`graph.py:1323`), so
  it always wins. Path 2 reads `getattr(entity_manager, "_driver")` — `EntityManager.__init__`
  (`graph.py:320-329`) sets `_client`, `_group_id`, `_embedding_provider`, never `_driver`, so that
  branch is unreachable. Path 3 paginates the whole org graph into memory to count it.
- `_assert_surreal_query_dialect` (49-51) rejects queries containing `CALL`, `MATCH`, or `UNWIND` as
  "not SurrealQL" — a Neo4j/Cypher-era guard on a Surreal-only system, and a token match that would
  reject a legitimate query with a field named `match`.

**Size S · Severity minor**

### D4. Vestigial per-row fields written on every entity

`services/graph.py:3022, 3033, 3076`

- `"_direct_insert": True` — a Graphiti-era marker written into `attributes` on every create and
  every update. Nothing reads it as a signal; the only other production reference strips it out
  again (`apps/api/src/sibyl/api/routes/entities.py:1990`). It also rides inside the
  `attributes.metadata` JSON snapshot.
- `"labels": [entity.entity_type.value, "Entity"]` — the literal `"Entity"` is a Graphiti node
  label, and `entity_type` is duplicated from its own column. Both are indexed by
  `idx_entity_labels` on `labels.*` (`schema.py:112`, rebuilt `CONCURRENTLY` in migration v19), and
  `_entity_type_from_row` reads labels as a fallback type source (`graph.py:2149-2151`).

**Size S · Severity minor**

### D5. Deleted graph contract suite left only stale bytecode

`packages/python/sibyl-core/tests/graph/surreal/__pycache__/`

The directory contains no tracked `.py` files (`git ls-files` returns nothing) — only `__pycache__`
from commit `13f29fee` "delete the Graphiti compat layer and legacy graph". Among the
deleted-but-cached names are dead-table suites (`test_saga_node_ops`, `test_community_edge_ops`,
`test_next_episode_edge_ops`, `test_has_episode_edge_ops`, `test_episodic_edge_ops`) and
live-surface suites (`test_entity_node_ops`, `test_search_interface`,
`test_native_memory_contract`). The live-surface coverage went with them; whether it was re-homed
into `tests/test_graph.py` was not verified.

**Size S · Severity minor**

### D6. `EntityType._missing_` handles exactly one type case-insensitively

`models/entities.py:64-68`

```python
@classmethod
def _missing_(cls, value: object) -> "EntityType | None":
    if isinstance(value, str) and value.lower() == "guide":
        return cls.GUIDE
    return None
```

`EntityType("guide")` already resolves via StrEnum without `_missing_`, so the hook only fires for
`"Guide"` / `"GUIDE"` — and does nothing for the other 34 members. Either all types should be
case-insensitive or none.

**Size S · Severity minor**

---

## D. Error handling that swallows or masks

### G1. Bulk relationship write reports failure as "skipped"

`services/graph.py:1465-1474`

```python
async def create_bulk(self, relationships) -> tuple[int, int]:
    try:
        created_ids = await self.create_direct_bulk(prepared, generate_embeddings=True)
    except Exception:
        return 0, len(prepared)
```

A bare `except Exception` with no logging turns a dead connection, a schema rejection, or a
transaction conflict into `(0 created, N skipped)`. The caller cannot distinguish a write failure
from a legitimate no-op. Edges vanish silently.

**Size S · Severity major**

### G2. Vector search failure degrades retrieval to zero candidates, silently

`services/graph.py:840-845`

```python
except Exception as exc:
    log.warning("entity_vector_search_failed", error_type=type(exc).__name__)
    return []
```

Only the exception class name is logged — not the message — so an HNSW index that is missing,
mid-rebuild, or dimension-mismatched produces `entity_vector_search_failed: SurrealQueryError` and
an empty result the caller reads as "nothing matched". Given the embedding-dimension rebuild
machinery (`schema.py:763-806`) can leave vectors cleared, this is a live failure mode.

**Size S · Severity major**

### G3. A broken graph renders as an empty graph

`services/graph_communities.py:1948-1950, 1971-1973, 1993-1995`

`count_graph_totals` → `return 0, 0`; `_fetch_graph_nodes` → `return [], set()`;
`_fetch_graph_edges` → `return []`, each behind `except Exception as e: log.warning(...)`. Three
independent failure paths all render as "your graph is empty" rather than "the query failed."

**Size S · Severity major**

### G4. Embedding failures are absorbed and the entity is written unembedded

`services/graph.py:2700-2725`

`_embed_texts_for_write` catches everything, logs `graph_embedding_failed`, returns `None`, and the
write proceeds without a vector. There is a repair path (`backfill_embeddings_if_current`,
`graph.py:377`), but the create call returns success and no caller can tell an embedded memory from
an unembedded one.

**Size M · Severity major**

### G5. Unique-index skip logs only fingerprints

`backends/surreal/schema_helpers.py:56-64` — see S1. The warning carries `statement_hash` and
`error_hash`, not the statement or message, so the operator cannot tell which index was skipped or
on what without re-deriving it.

**Size S · Severity minor** (rolls up into S1)

### G6. Bootstrap error is recorded then execution continues

`apps/api/src/sibyl/cli/db.py:687-689` — `except Exception as exc: entry["error"] = str(exc)` then
falls through to read the version and run invariants. Deliberate (it wants the post-mortem state)
but worth naming: a bootstrap that threw is followed by an invariant repair attempt against a
half-migrated plane.

**Size S · Severity minor**

### G7. Legacy `updated_at` string schema retried by matching an error message

`services/graph.py:2930-2971`

```python
def _is_legacy_updated_at_string_schema_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    return "coerce value for field `updated_at`" in message and (
        "expected `none | string`" in message or "expected none | string" in message)
```

On match, the whole batch is rewritten with ISO strings and replayed. This is the compatibility tail
of migration v5 — it fires only for namespaces still below graph schema v5. It is coupled to an
exact SurrealDB error string across two phrasings, i.e. it breaks silently on a server upgrade and
reverts to a hard write failure. Candidate for deletion once a floor version is declared.

**Size S · Severity minor**

---

## E. Concurrency and pooling hazards

### C1. Production code monkeypatches another module's globals on every call

`services/graph.py:1929-1950`

```python
async def get_surreal_graph_client(group_id: str) -> SurrealGraphClient:
    # Compatibility callers patch graph.SurrealGraphClient, not graph_client.
    original_client_type = _graph_client.SurrealGraphClient
    _graph_client.SurrealGraphClient = SurrealGraphClient
    try:
        return await _graph_client.get_surreal_graph_client(group_id)
    finally:
        _graph_client.SurrealGraphClient = original_client_type
```

`prepare_graph_schema` (1943-1950) does the same for `bootstrap_schema`. This is a test shim living
in the hot path: every graph client acquisition mutates a module global, awaits, and restores it.
Two concurrent coroutines interleave the save and restore, so the second one's `finally` writes back
whatever the first had installed. Today both install the same value so the damage is bounded, but a
test that patches `graph.SurrealGraphClient` while any real call is in flight gets its patch
reverted mid-test — and the pattern is a live-lock waiting for a third writer.

Same file, lines 98-99: `_clients = _graph_client._clients` and
`_prepared_groups = _graph_client._prepared_groups` alias another module's private mutable state. It
works only because every mutation is in-place (`.clear()`, `.discard()`); a single rebinding in
`graph_client.py` silently desynchronizes the two views.

**Size M · Severity major**

### C2. Every write pays an extra round trip

`backends/surreal/dedicated_client.py:287-310`

For any non-retryable (write) query the client first sends `RETURN true;` on the connection as a
liveness preflight, then sends the real statement. That is 2× the round trips for every entity
create, update, delete, and bulk upsert. The intent is to avoid replaying a write on a dead socket,
which is correct; the cost is unbounded and unmeasured.

**Size M · Severity major** (perf; the fix is a connection-health signal rather than a probe query)

### C3. Live queries bypass the pool

`backends/surreal/dedicated_client.py:252-267`

`live_table` calls `self._new_connection()` outside the pool on every invocation. Nothing bounds how
many live subscriptions an org can hold open; each is a socket the pool does not know about.

**Size S · Severity minor**

### C4. Schema bootstrap is serialized globally across all orgs

`services/graph_client.py:48, 106`

`_prepare_lock` is one module-level `asyncio.Lock` for every organization. Org B's first request
blocks behind Org A's full schema bootstrap (which on a cold namespace runs ~19 migrations including
concurrent index builds). Should be per-`group_id`.

**Size S · Severity minor**

### C5. Whole-org graph loaded into Python memory, three times per render

`services/graph_communities.py:202-235, 1940-1941, 1963-1964, 1989`

`_list_all_entities` paginates every entity in the org into a list, uncapped by default
(`max_items: int | None = None`). `count_graph_totals`, `_fetch_graph_nodes`, and
`_fetch_graph_edges` each call it (and `_list_all_relationships`) independently, so a single graph
view loads the entire org graph two to three times over and does the filtering, clustering, and
counting in Python. Two more call sites at 2417/2423.

This is the mechanism class behind the known `/graph` starfield behavior, and it is the "pull it all
client-side" pattern a redesign has to kill rather than inherit.

**Size L · Severity major**

### C6. Per-query stack introspection

`backends/surreal/dedicated_client.py:386-395`

`_caller_origin()` calls `sys._getframe(2)` on every `execute_query` / `execute_query_raw` to build
an origin string for the log line. Correct as written (the frame depth holds because it is evaluated
as an argument inside the public method), but it is unconditional CPython frame introspection on the
hottest path and it silently breaks if either wrapper is ever refactored to add a call level.

**Size S · Severity minor**

---

## F. Multi-tenancy plumbing

### T1. Namespace derivation is unvalidated and lossy

`services/graph_client.py:117-122`

```python
def _namespace_for_group(prefix: str, group_id: str) -> str:
    if not group_id:
        raise ValueError(...)
    sanitized = group_id.replace("-", "").lower()
    return f"{prefix}{sanitized}"
```

Only emptiness is checked. `group_id` is never validated as a UUID or as a legal SurrealDB
identifier, and stripping hyphens means `"ab-c"` and `"abc"` collide into the same namespace. In
practice group_ids are UUIDs and the namespace reaches the server through `client.use(ns, db)`
rather than string interpolation, so this is not an injection today. It is one refactor away from
being one, and the collision is a silent cross-tenant merge.

`validate_identifier` already exists at `schema_version.py:265-268` and is not used here.

**Size S · Severity major**

### T2. Redundant `group_id` predicates carry no index

`services/graph.py` — 33 occurrences of `group_id = $group_id`; also `retrieval/search.py` (20),
`graph_communities.py` (7), `retrieval/dedup.py` (3), and the migrate modules.

Isolation is the namespace. Migration v7 (`GRAPH_INDEX_PRUNE_DEFINITIONS`, `schema.py:320-344`)
removed every `group_id`-leading index precisely because they were redundant inside a per-org
namespace — but the `WHERE group_id = $group_id` predicates stayed. They are now unindexed filters
on every graph query, and they read as though they were the isolation mechanism, which they are not.

Two options and both are fine, but the current state is the worst of them: either drop the
predicates (isolation is the namespace) or keep them and say so.

**Size M · Severity major**

### T3. Cross-org guard applied in Python rather than in the query

`services/graph.py:496-519`

`get_many` runs `RETURN $uuids.map(|$u| (SELECT * FROM entity WHERE uuid = $u LIMIT 1)[0])` and
filters `row.get("group_id") == self._group_id` afterward in Python, because — per the inline
comment — the closure body silently drops every outer binding on at least one engine. The comment is
honest and the mitigation is sound inside a namespace. Flagging it because the workaround is
invisible from the call site and would break if anyone moved the graph to a shared namespace.

**Size S · Severity minor**

### T4. Raw query escape hatch binds `group_id` without enforcing it

`services/graph_runtime.py:157-167`

`execute_graph_query(group_id, query, **params)` executes an arbitrary caller-supplied SurrealQL
string against the org client, passing `group_id=$group_id` as a bound param. Nothing checks that
the query uses it.

**Size S · Severity minor**

---

## G. Test gaps on load-bearing invariants

- **Graph schema invariants are untested because they are unimplemented for graph.**
  `tests/test_surreal_schema_invariants.py` exercises the machinery, but no test can cover the graph
  plane because no graph invariant plan exists (S1).
- **The unique-index swallow has no graph-level test.** Nothing asserts that a namespace which
  failed to build `idx_entity_uuid` is detected.
- **Concurrent bootstrap is untested.** No test starts two `prepare_graph_schema` calls for the same
  org from different processes (S2).
- **Dialect divergence is structurally untestable in unit tests.** Everything runs on the embedded
  2.x engine and takes the un-rewritten branch of `render_surreal_compatible_sql` (S3).
  `tests/test_surreal_schema_syntax.py` (1,705 lines) is the compensating control; whether it
  asserts the _rendered_ 3.x form for every statement was not verified.
- **`attributes.metadata` staleness has no regression test** for a key outside
  `_SNAPSHOT_SHADOWED_METADATA_KEYS` (A1). Adding one would be S and would pin the hazard.
- **`storage/` Protocols are tested against Fakes only** (D2) — the test proves the Protocol is
  satisfiable, not that anything satisfies it.
- **Deleted per-operation graph suite** (D5) — coverage of entity node ops, search interface, and
  the native memory contract left the tree in `13f29fee` and its re-homing was not verified.

---

## Ranked summary

| #   | Finding                                                                                    | Where                                               | Size   | Severity |
| --- | ------------------------------------------------------------------------------------------ | --------------------------------------------------- | ------ | -------- |
| 1   | Metadata written three ways; JSON snapshot goes stale and resurrects deleted keys          | graph.py:1982, 3017, 3071                           | L      | blocker  |
| 2   | Graph plane has no schema-invariant check; UNIQUE index failures silently swallowed        | schema_helpers.py:44, cli/db.py:713                 | M      | blocker  |
| 3   | Blind full-replace upsert with per-field absence exceptions                                | graph.py:229-279                                    | M      | major    |
| 4   | Whole-org graph loaded into memory three times per render                                  | graph_communities.py:202, 1940                      | L      | major    |
| 5   | Three divergent Surreal normalizers; "canonical" module no longer canonical                | records.py:1, graph.py:2521, surreal_content.py:653 | M      | major    |
| 6   | Production code monkeypatches another module's globals per call                            | graph.py:1929-1950                                  | M      | major    |
| 7   | Write failures reported as skips; vector-search and graph-render failures render as empty  | graph.py:1469, 840; graph_communities.py:1948       | S each | major    |
| 8   | `Relationship` model cannot express the temporal validity the schema stores                | entities.py:269 vs schema.py:190                    | S      | major    |
| 9   | Test engine (SDK 2.x embedded) differs from prod (3.2.3), bridged by 4 string replacements | schema.py:970                                       | M      | major    |
| 10  | `episode`/`mentions` restore-only but fully indexed and migrated per namespace             | schema.py:142, 208                                  | M      | major    |
| 11  | Typed Entity subclasses are write-only; only Task/Procedure rehydrate                      | entities.py:147, graph.py:2213                      | M      | major    |
| 12  | `storage/` contracts: 177 lines, zero production consumers, decoy seam                     | storage/                                            | S      | major    |
| 13  | Missing timestamps fabricated as `now()`; three datetime conventions                       | graph.py:2100, 3410; records.py:19                  | M      | major    |
| 14  | No cross-process migration lock; lazy per-org bootstrap at request time                    | graph_client.py:48, schema_version.py:137           | M      | major    |
| 15  | Extra round trip on every write (preflight `RETURN true;`)                                 | dedicated_client.py:287                             | M      | major    |
| 16  | Namespace derivation unvalidated, hyphen-stripping collides                                | graph_client.py:117                                 | S      | major    |
| 17  | Redundant unindexed `group_id` predicates after v7 index prune                             | graph.py ×33, search.py ×20                         | M      | major    |
| 18  | `relates_to.name` has no enum assertion (entity_type does)                                 | schema.py:182 vs 385                                | S      | minor    |
| 19  | Vestigial `_direct_insert` and `"Entity"` label on every row                               | graph.py:3022, 3033                                 | S      | minor    |
| 20  | graph_runtime.py: duplicate dataclass, unreachable `_driver` branch, Cypher-era guard      | graph_runtime.py:36, 49, 107                        | S      | minor    |

### Cheapest high-value moves before a 1.3 rearchitecture

1. Add `graph_schema_invariant_plan()` and wire the graph plane into `sibyld db init` alongside auth
   and content (S1). Mechanical — the pattern already exists twice.
2. Delete `storage/` (D2) or implement it. Leaving it is the trap.
3. Drop the `episode` table and `mentions` edge behind a restore-time-only schema (S4), which also
   removes eight indexes, two backfill migrations, and a statement from the entity-delete hot path.
4. Give `Relationship` its temporal columns as typed fields (A4). One small model change unlocks the
   stated 1.3 goal.
5. Pin A1 with a regression test before touching anything else — the stale snapshot is the invariant
   most likely to be silently broken by a representation change.
