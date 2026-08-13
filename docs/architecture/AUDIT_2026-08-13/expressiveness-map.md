# Sibyl Expressiveness Surface Map

What an agent can express when it stores a memory, and how much of that expressed
structure retrieval actually consumes. All receipts are `file:line` against
`/Users/bliss/dev/sibyl` at commit `5388d986`. Read-only survey; nothing mutated.

---

## 0. Two substrates and two retrieval paths

Two structural facts govern everything below. Both are easy to miss and both
change how you read the matrix.

### 0.1 Every memory is stored twice

| Substrate | Table | Schema receipt | Character |
| --- | --- | --- | --- |
| Content / raw | `raw_captures` (+ `supersedes`, `derived_from` relations) | `packages/python/sibyl-core/src/sibyl_core/backends/surreal/schemas/content/10_tables.surql:121-175` | Verbatim capture, shared org namespace, carries a real `review_state` lifecycle |
| Graph | `entity` + `relates_to` | `packages/python/sibyl-core/src/sibyl_core/backends/surreal/schema.py:75-205` | Projected/typed node, per-org namespace, **no lifecycle column** |

`MemoryCaptureService.capture` writes raw first, then graph, copying a metadata
bag across the seam
(`packages/python/sibyl-core/src/sibyl_core/memory_pipeline/capture.py:104-154`).

Nearly all correction and supersession expressiveness lives on the raw side.
Nearly all retrieval scoring reads the graph side. Gap G1 is the consequence.

### 0.2 Two live search entry points, not one

- `sibyl_core/tools/search.py::search` — the MCP `search` tool, the `sibyl
  search` CLI, probe rehearsal (`memory_pipeline/rehearsal.py:51`), and synthesis
  (`services/synthesis.py:138`). Delegates to `retrieval/hybrid.py::hybrid_search`
  at `tools/search.py:1074`.
- `sibyl_core/retrieval/search.py::context_search` — the context-pack / `recall`
  path, called from `tools/context.py:1489`.

These have **different scoring code**. `hybrid.py` is not dead: it is the legacy
lane still serving `search`. Where the two disagree, the matrix says so.

---

## 1. Write surface

### 1.1 The capture contract

`MemoryCaptureRequest`
(`packages/python/sibyl-core/src/sibyl_core/memory_pipeline/capture.py:16-46`) is
the shape every graph-memory write funnels into:

| Field | Type | Default | Validation |
| --- | --- | --- | --- |
| `title` | `str` | required | length-capped downstream |
| `content` | `str` | required | stripped; `stored_content` is the span-addressable string (`capture.py:49-57`) |
| `entity_type` | `str` | `"episode"` | coerced to `EntityType`; **no DB constraint** |
| `domain` | `str \| None` | `None` | freeform |
| `tags` | `Sequence[str] \| None` | `None` | **freeform, no vocabulary** |
| `related_to` | `Sequence[str] \| None` | `None` | entity ids → untyped `RELATED_TO` (§1.3) |
| `languages` | `Sequence[str] \| None` | `None` | freeform |
| `retrieval_keys` | `Sequence[str] \| None` | `None` | strict: ≤16 keys, ≤200 chars (`retrieval_keys.py:34-38, 67-110`) |
| `metadata` | `Mapping` | `{}` | free bag, minus server-owned structure keys |
| `provenance` | `Mapping` | `{}` | free bag, raw side only |
| `source_id` | `str \| None` | `None` | |
| `memory_scope` | `str` | `"private"` | **enum-asserted in DB**, raw table only (`content_schema.py:170-171`) |
| `scope_key` | `str \| None` | `None` | |
| `principal_id` | `str \| None` | `None` | server-stamped, not caller-forgeable (`capture.py:126-134`) |
| `capture_surface` | `str` | `"cli"` | |
| `wait_searchable` | `bool` | `False` | forces sync write |
| `skip_conflicts` | `bool` | `False` | |
| `diary` | `bool` | `False` | |
| `agent_id` | `str \| None` | `None` | |
| `project_id` | `str \| None` | `None` | |
| `spans` | `Sequence[Mapping] \| None` | `None` | strict, §1.4 |
| `atomic` | `bool` | `False` | strict, §1.4 |
| `probes` | `Sequence[str] \| None` | `None` | strict, ≤5 × ≤500 chars (`structure.py:39-46`) |

Two CLI-only fields ride in as untyped metadata rather than contract fields:

- `--pin` → `capture_metadata["pinned"] = True`
  (`apps/cli/src/sibyl_cli/main.py:3601, 3672`). Read only by the retention job
  (`apps/api/src/sibyl/jobs/consolidation.py:370-376`).
- `--basis` → `capture_metadata["basis"]`, normalized to one of
  `observed | inferred | told | assumed`
  (`apps/cli/src/sibyl_cli/main.py:225, 3602-3606, 3678-3679`). This is the only
  epistemic-status axis in the entire system, and its **sole reader is a blame
  display** (`apps/api/src/sibyl/api/routes/memory.py:1978`). Nothing in
  retrieval, ranking, synthesis, or retention consults it.

Absent from the contract entirely: `importance`, `confidence`,
`valid_from`/`valid_to`, any typed-relationship parameter, any `supersedes`
parameter. Importance and confidence enter only as untyped metadata keys, where
`normalize_memory_quality_metadata` canonicalizes them
(`memory_pipeline/quality.py:32-54`).

### 1.2 Surface coverage for the 1.2 structure params

| Surface | spans | atomic | probes | retrieval_keys |
| --- | --- | --- | --- | --- |
| MCP `remember` (`apps/api/src/sibyl/server.py:2544-2560`) | yes | yes | yes | yes |
| MCP `add` (`server.py:2436-2458`) | no | no | no | no |
| CLI `sibyl remember` / `sibyl add` (`main.py:3614, 3622, 3627, 3558`) | `--spans-json` | `--atomic` | `--probe` | `--key` |
| CLI `sibyl capture` / `note` (`main.py:1866, 1989`) | no | no | no | no |
| CLI `sibyl entity create` (`entity.py:213`) | no | no | no | no |
| `POST /entities` (`apps/api/src/sibyl/api/schemas/entities.py:39, 47, 69, 90`) | yes | yes | yes | yes |
| `PATCH /entities/{id}` (`schemas/entities.py:155, 160`) | yes | yes | no | no |
| `POST /memory/raw` (`schemas/memory.py:17`) | no | no | no | no |

`sibyl add` is a hidden alias for the same function as `sibyl remember`
(`main.py:3774`). The MCP `add` tool is a different, older tool that carries none
of the structure params.

### 1.3 Entity type vocabulary

`EntityType` (`models/entities.py:10-68`) — 34 members: `pattern, rule, template,
guide, tool, language, topic, episode, knowledge_source, config_file,
slash_command, project, epic, task, team, error_pattern, milestone, source,
document, procedure, community, note, domain, artifact, decision, plan, idea,
claim, preference, person, place, event, session, passage`.

The `entity_type` column is bare `TYPE string` with **no ASSERT**
(`backends/surreal/schema.py:79`) — the enum is a Python convention, not a
database constraint. MCP `remember` narrows further to an 11-value `MemoryKind`
literal (`apps/api/src/sibyl/server.py:69-81`), and the LLM extractor to a
12-value subset (`models/memory_extraction.py:15-31`).

### 1.4 Relationships: what a writer can declare

`RelationshipType` (`models/entities.py:71-116`) declares 40 predicates,
including `SUPERSEDES`, `CONFLICTS_WITH`, `CONTRADICTS`, `SUPPORTS`, `DECIDES`,
`BREAKS`, `ENABLES`, `REQUIRES`.

**A writing agent can select none of them.** The only relationship parameter on
any write surface is `related_to`, and both write paths hardcode the predicate:

- `tools/add.py:712-725` — `"type": "RELATED_TO"` as a literal at `add.py:720`.
- `services/memory.py:3196-3205` — `RelationshipType.RELATED_TO` for every id.

There is no `relationship_type` parameter on `add()` (`add.py:222-256`), on
`EntityCreate` (`schemas/entities.py:65-99`), on the MCP `add`/`remember` tools,
or on `MemoryCaptureRequest`. There is also no relationship-create HTTP endpoint:
`apps/api/src/sibyl/api/routes/graph.py` exposes reads plus `POST /subgraph`
(`graph.py:524`), whose `relationship_types` field
(`apps/api/src/sibyl/api/schemas/graph.py:48`) is a filter, not a writer.

The complete live predicate vocabulary — **7 of 40**:

| Predicate | Producer | Receipt |
| --- | --- | --- |
| `RELATED_TO` | `related_to` param; also auto-discovered links at similarity ≥0.75 | `tools/add.py:712-725, 748, 756-768` |
| `BELONGS_TO` | project association | `services/memory.py:3176-3183`, `tools/add.py:693` |
| `DEPENDS_ON` | `add` tool `depends_on` param (tasks) | `tools/add.py:699-711` |
| `DERIVED_FROM` | promotion, experience projection | `services/memory.py:3185-3193`, `projection/experience.py:511, 698, 741, 780` |
| `SUPERSEDES` | reflection promotion only | `services/memory.py:3207-3226` |
| `MENTIONS` | LLM entity projection | `projection/memory.py:991-1047` |
| `PART_OF` | passage + experience projection | `projection/passages.py:266-269`, `projection/experience.py:794` |

The remaining 33 enum members have no writer at all.

`SUPERSEDES` deserves its own note because it is the one semantically loaded edge
the system can produce. It comes only from reflection-candidate metadata keys
`("supersedes", "supersedes_ids", "superseded_ids", "supersedes_entity_ids")`
(`services/memory.py:2468-2474`), and every target is re-authorized:
`_authorized_superseded_entity_ids` drops targets the principal cannot write
(`memory.py:2799-2811`) and `_with_authorized_supersedes` **overwrites** any
caller-supplied list with the authorized set (`memory.py:2480-2489`). So even
planting the key in metadata cannot forge a supersession.

A regex `RelationshipBuilder` (`apps/api/src/sibyl/ingestion/relationships.py:38-120`)
can infer `REQUIRES`, `CONFLICTS_WITH`, `SUPERSEDES`, `APPLIES_TO`,
`DOCUMENTED_IN`, `WARNS_ABOUT` from text — and is **dead on the memory path**.
Its only importer is a test (`packages/python/sibyl-core/tests/test_default_memory_loop.py:54`).
It also carries a divergent `RelationType` enum whose `WARNS_ABOUT` does not
exist in `RelationshipType`.

### 1.5 Spans, atomicity, probes

Three declarations, all server-validated and unforgeable through the metadata bag
(`memory_pipeline/structure.py:55-63`; `strip_structure_metadata` applied at
`tools/add.py:377-378`, re-stamped at `add.py:477`):

- **`spans`** — half-open `[start, end)` offsets into `content.strip()` with an
  optional ≤120-char label (`spans.py:92-108`). Must tile the whole body: no gap
  (`spans.py:194-199`), no overlap (`spans.py:188-193`), ≥2 spans
  (`spans.py:170-174`), ≤64 spans (`spans.py:49`), each ≤17,648 chars
  (`spans.py:66-67`), covering every character (`spans.py:215-220`). Never
  repaired, never partially honored (`spans.py:159-163`). Accepted spans become
  `passage` entities with `PART_OF` edges (`projection/passages.py:266-269`).
- **`atomic`** — "this body is one retrievable unit." Mutually exclusive with
  spans (`structure.py:131-133`); refused above 18,000 chars (`spans.py:224-232`).
  Survives a content edit unless restated, while a stale span plan is dropped
  (`apps/api/src/sibyl/api/routes/entities.py:214-222`).
- **`probes`** — up to 5 queries the memory must answer (`structure.py:39-46`).
  Each is run through the **live fused retrieval path** at write time and its rank
  (or absence) recorded (`memory_pipeline/rehearsal.py:56-131`). Raw recall and
  exposure recording are disabled so the measurement can actually fail
  (`rehearsal.py:8-13, 157-160`). Rehearsal runs after passages exist so a probe
  landing on a span counts (`tools/add.py:834-835`). Supplying probes forces a
  synchronous write (`tools/add.py:380-384`, `routes/entities.py:2154`). A failing
  probe never fails the write (`rehearsal.py:14-16`).

### 1.6 Retrieval keys

An identifier the writer asserts the memory answers to, **not derived from the
content** — so a key may name something the body never spells out
(`retrieval_keys.py:1-23`). Compared by casefold plus whitespace-collapse and
nothing else: no stemming, no punctuation stripping (`retrieval_keys.py:51-54`).
The write boundary is strict and the storage edge lenient by design
(`retrieval_keys.py:18-22`, strict at `:93-109`, lenient at `:113-148`). `None`
means "this write does not speak to keys" and preserves existing ones
(`retrieval_keys.py:118-120`).

---

## 2. Extraction and enrichment

### 2.1 Entity extraction

`ExtractedMemoryEntity` (`models/memory_extraction.py:33-48`): `name`,
`entity_type` (12-value subset), `summary`, optional `confidence` 0-1,
`evidence` ≤400 chars. Max 12 per source (`memory_extraction.py:11`).

At projection (`projection/memory.py:819-858`) confidence defaults to 0.75 when
omitted, is clamped, gates admission via `min_confidence`, and orders/dedupes
candidates. Dedup key is `f"{type}:{name.lower()}"` (`projection/memory.py:849`),
scoped to a single batch.

### 2.2 There is no relation extraction

`MemoryEntityExtractionResult` holds only `entities`
(`models/memory_extraction.py:51-57`) — the extraction contract has no relation
field. The pipeline emits exactly one edge type from extraction: `MENTIONS`
(`projection/memory.py:991-1047`).

The reflection layer has a typed relationship dataclass,
`ReflectionRelationshipRecord`, carrying a `relationship_type: str`
(`models/reflection.py:237-245`). It is populated in exactly one place, with one
hardcoded predicate: `_relationship_records_for_project` emits
`relationship_type="BELONGS_TO"` pointing at the project
(`services/reflection.py:506-524`). The records are serialized into metadata
(`reflection.py:266-268`) and **never become graph edges** — promotion builds
edges from `_relationships_for_promotion` (`services/memory.py:3165-3226`), which
never reads `relationship_records`.

So the pipeline can say *"this memory mentions Redis"* and nothing else. It
cannot say *"Redis TTL expiry causes the JWT refresh failure."*

### 2.3 What lands on `relates_to`

Built at `services/graph.py:3326-3349`:

| Column | Source |
| --- | --- |
| `name` | `relationship.relationship_type.value` — the predicate |
| `fact` | `metadata["fact"]`, else auto-generated `"<src> <predicate> <tgt>"` (`graph.py:3352-3357`) |
| `fact_embedding` | `metadata["fact_embedding" \| "embedding"]` |
| `source_id` / `target_id` | model fields |
| `episodes` | `metadata["episodes"]` |
| `attributes` | the whole remaining metadata bag (FLEXIBLE) |
| `created_at`, `expired_at`, `valid_at`, `invalid_at` | model field + metadata (`valid_from`/`valid_to` accepted as aliases) |

**`Relationship.weight` is not persisted.** The model declares it
(`models/entities.py:276`) and `_relationship_record` omits it. There is no
`weight` column on `relates_to` (full field list: `schema.py:181-193`). It exists
only if a caller puts `weight` into metadata, which lands in `attributes` and is
hydrated back at `graph.py:2442, 2494-2497`. Nothing does:
grepping `"weight"` across `sibyl-core/src` and `apps/api/src` finds readers
(`hybrid.py:559`, `graph.py:2495`, export, community layout) and **no writer**.
`_metadata_weight` therefore always returns its 1.0 default.

There is no provenance column and no confidence column on the edge. Both can only
exist as `attributes` sub-keys.

---

## 3. Taxonomy: four axes, four contracts

| Axis | Storage | Validation | Role |
| --- | --- | --- | --- |
| `entity_type` | `entity.entity_type` string (`schema.py:79`), indexed (`:111`) | Python enum only, **no DB ASSERT** | Kind of thing; drives facet routing |
| `tags` | `entity.tags option<array<string>>` (`schema.py:104`), **no index** | none, freeform | Ad-hoc labeling |
| `labels` | `entity.labels array<string>` (`schema.py:83`), indexed on `labels.*` (`:112`) | **not writer-settable**: always `[entity_type, "Entity"]` (`graph.py:3033`) | Legacy Graphiti compat shape |
| `memory_scope` | `entity.memory_scope` (`schema.py:105`) + `idx_entity_memory_scope` (`:117`); `raw_captures.memory_scope` with **ASSERT** (`content_schema.py:170-171`) | enum-enforced on the raw side only | Authorization partition |
| `project_id` | `entity.project_id` (`schema.py:96`), in 4 indexes | freeform string | Second partition axis |

`labels` is not a writer axis despite looking like one — it is a derived
duplicate of `entity_type`. `memory_scope` is the only axis with a real database
constraint, and only on the raw table.

---

## 4. THE STORED-VS-USED MATRIX

"Used" means read by retrieval, ranking, fusion, or context-pack assembly in a
way that changes what comes back or in what order. Display pass-through does not
count. Where the two search paths (§0.2) differ, both are named.

| Axis | Stored | Verdict | Receipt |
| --- | --- | --- | --- |
| `entity_type` | `entity.entity_type` | **USED** — filter, request pools, tie-rank, boost | SQL filter `retrieval/search.py:1602-1604`, applied to vector (`:1436`), exact-key (`:1084`), BFS (`:2152`); KNN overfetch lifts the predicate out of the HNSW bracket and reapplies it (`:1384-1407`). Request pools `tools/context.py:77-90, 1221-1239`. Lineage tie-rank `context.py:700-720, 739`. Boosts: `type == "task"` + active → `search.py:3110`; `type == "raw_memory"` → `:3114-3115`. **No global type-prior table exists.** |
| `entity_type` facet coverage | as above | **PARTIAL** — 8 types unreachable by recall | `FACET_TYPES` (`context.py:77-90`) omits `person, place, language, team, milestone, community, knowledge_source, slash_command`; `_types_for_facets` (`:1221-1239`) builds the requested-type list from that map, so those 8 are never requested by any intent |
| `tags` | `entity.tags` (unindexed) | **STORED, UNREAD by search** | In `retrieval/`, two lines, both pass-through: `search.py:2383, 2455`. `list_by_type` accepts `tags=` (`graph.py:1104`) but emits **no SQL clause** (`graph.py:1143-1186`); filtering is a Python post-filter (`graph_search.py:99-100`) whose only caller is `tools/explore.py:241` — a browse surface. Neither `tools/search.py:343` nor `context.py:1414` passes tags |
| `labels` | `entity.labels`, indexed | **PARTIAL** — used as type fallback; the label filter is dead | Live read: `search.py:2348-2352, 2632-2636` (third fallback for deriving candidate type). Dead filter: `search.py:1605-1607, 1628-1630` gate on `SearchFilter.node_labels`, declared `:233` with default `()` and **never populated by any production caller** (constructions at `:899, 1214, 2081`). `idx_entity_labels` is never exercised |
| `memory_scope` | `entity.memory_scope` column **and** `attributes.memory_scope` | **USED as a hard gate — via attributes, not the column** | Gate `search.py:2710-2724` → `auth/memory_policy.py:643, 657, 660`, reading `metadata["memory_scope" / "scope_key" / "principal_id"]`, which arrive via the whole-attributes splat at `search.py:2211-2212`. Plan side `search.py:287-336, 623-635, 951`. Pack side `context.py:439-447, 1426-1434`. The **column** appears in no retrieval WHERE clause; `idx_entity_memory_scope` is dead weight and the two copies can drift |
| `project_id` | `entity.project_id` + `attributes.project_id` | **USED** — filter, boost, gate | Node filter `search.py:1608-1610` (checks both column and attribute); edge filter is a 5-way disjunction across endpoints `:1631-1641`; boost `:3112-3113`; post-filter gates `:2693-2707`; selectivity signal `:919-927`; episode lane skipped entirely when project filtering is on `:1143-1144` |
| `status` | `entity.status` | **USED** — filter, boost, facet suppression | SQL `graph.py:1170-1172, 1186`, reached from the pack at `context.py:1414-1419`; boost `search.py:3109-3111`; `archived` dropped and `done` demoted at `context.py:216-234`; lineage rank `-1` for in-flight `context.py:735-738` |
| `priority` | `entity.priority`, indexed | **STORED, UNREAD by retrieval** | Exactly one hit in `retrieval/`: `search.py:2380`, a pass-through key. SQL clause exists at `graph.py:1174-1177` but its only caller is `tools/explore.py:418-427` (browse). `idx_entity_priority` serves browse only. A high-priority memory does not rank higher |
| `retrieval_keys_normalized` | `entity.retrieval_keys_normalized`, element-indexed | **USED** — dedicated high-precision lane | `CONTAINSANY $probe_keys` at `search.py:1086`, index-served by `idx_entity_retrieval_keys ON ... retrieval_keys_normalized.*` (`schema.py:118`); the `.*` is load-bearing (`search.py:1071-1077`). Overlap scoring `:1126-1133, 1111, 1119`; fusion multiplier `_exact_key_signal_multiplier` `:3052-3074`; probes minted from query identifiers `retrieval/identifier_query.py:47-49, 184`; inert without an identifier `:1067-1068` |
| `retrieval_keys` (display form) | column | **STORED, UNREAD** — matching always uses the normalized form | `search.py:2384` |
| `attributes` (FLEXIBLE) | `entity.attributes` | **USED** — splatted whole, ~14 sub-keys read by name | Splat `search.py:2211-2212, 2290-2291, 2591`. Named: `project_id` (`:1609, 1634-1638, 2202`), `entity_type` (`:2339, 2623`), `content`/`description` (`:2366-2367`), `source*` (`:2205-2208`), endpoint project ids (`:2575, 2578`), scope trio (→ `memory_policy.py:643-660`), `status` (→ `:3109`), usage counters (→ `temporal.py:144-146, 199-201`), `source_entity_type` (`context.py:201`), `projection_kind` (`apps/api/src/sibyl/api/routes/context.py:502-511`), `parent_entity_id`/`passage_index`/`passage_total` (`context.py:1296-1298`), `operational_source_id` (`context.py:394-396`) |
| `attributes.importance` | `entity.attributes` | **UNREAD by retrieval; USED by retention** | Only reader: `apps/api/src/sibyl/jobs/consolidation.py:339-345`, feeding `importance × 0.5^(age/half_life)` at `:319-331`. Zero hits in `retrieval/` |
| `attributes.confidence` | `entity.attributes` | **STORED, UNREAD** | Display projections only: `search.py:2390, 2416` |
| `attributes.pinned` / `retention` | `entity.attributes` | **UNREAD by retrieval; USED by retention** | `jobs/consolidation.py:370-376` |
| `attributes.basis` | `entity.attributes` | **STORED, UNREAD** | Written `apps/cli/src/sibyl_cli/main.py:3678-3679`; sole reader is a blame display `apps/api/src/sibyl/api/routes/memory.py:1978` |
| edge `name` (predicate) | `relates_to.name`, indexed | **USED** — filter, traversal weight, candidate identity | Filter `search.py:1626-1627, 1481, 1491`; traversal filter `:1837, 1890`; hardcoded `name = "BELONGS_TO"` `:1950, 1992`; **per-predicate scoring** `:1859, 1865, 1912, 1918` → `_graph_expansion_path_score`; becomes `candidate.name` `:2307` and gates `_candidate_matches_types` `:2735` |
| edge `fact` | `relates_to.fact`, FT-indexed | **USED** — BM25 target and candidate content | `search.py:1179, 1188-1194, 2308, 2587` |
| edge `fact_embedding` | `relates_to.fact_embedding`, HNSW | **USED** — edge vector lane | `search.py:1489, 1518`; index `schema.py:205` |
| edge `weight` | **not a column**; only `attributes.weight` | **Effectively unread — and always 1.0** | Read on the legacy path only: `hybrid.py:558-562 _relationship_weight`. `context_search` ignores it entirely and uses the hardcoded table instead. No writer anywhere sets it, so `_metadata_weight` (`graph.py:2494-2497`) always returns 1.0. `_relationship_type_multiplier` (`hybrid.py:565-579`) needs `relationship_type_weights`, which no production `HybridConfig` supplies (`tools/search.py:1063-1071`) |
| edge `valid_at` | `relates_to.valid_at` | **USED indirectly** — decay anchor; never a WHERE | Projected `search.py:1661`, carried `:2391, 2417`, read first in the `"auto"` chain `retrieval/temporal.py:113` (`valid_at → valid_from → created_at`), driving `temporal_decay_multiplier` at `search.py:3120-3122` |
| edge `invalid_at` | `relates_to.invalid_at` | **STORED, UNREAD by retrieval** | Three hits in `retrieval/`, all key lists: `search.py:1661, 2394, 2420`. The traversal queries `_relation_target_hops` (`:1826-1868`) and `_relation_source_hops` (`:1871-1922`) carry no temporal predicate. An invalidated edge is walked and ranked exactly like a live one |
| edge `expired_at` | `relates_to.expired_at` | **STORED, UNREAD by retrieval** | `retrieval/` hits are `search.py:1661, 2421` only. Sole real readers are `tools/temporal.py:64, 296-333, 549` — a separate opt-in tool `context_search` never calls |
| edge `episodes` | `relates_to.episodes` | **STORED, UNREAD** | `search.py:1660, 2424` only. The `mentions` traversal at `:1794-1799` walks the separate `mentions` table, not this array |
| Graph traversal | `relates_to` | **USED and predicate-aware; outgoing-only** | `_graph_expansion_candidates` `search.py:1556-1591` → `_node_bfs_records` `:1670-1778`. 21-entry predicate weight table `:85-107` (`DECIDES` 1.0 … `MENTIONS` 0.58, unknown → 0.64); depth decay 0.72 at `:2181-2185`; highest-scoring predicate wins on hop dedup `:2171-2178`; fusion multiplier `:3077-3098`, applied only to candidates that also carry a non-expansion signal `:3085-3086`. **Incoming edges are off by default** (`include_incoming=False`, `:1680`), so "the tasks that depend on this one" is invisible to `context_search`. The `relationship_names` filter is reachable only from `tools/traverse.py`; the retrieval walk passes none (`:1575-1583`) and discriminates purely by weight |
| `last_used_at`, `last_recalled_at` | entity columns, indexed | **USED in retrieval scoring** | `get_entity_decay_timestamp` `retrieval/temporal.py:138-173`: `last_used_at` at full weight (`:144, 149-150`), `last_recalled_at` discounted by `EXPOSURE_DECAY_TIMESTAMP_WEIGHT` (`:145, 151-158`) — being shown is worth less than being used. Result shifts the decay anchor, consumed at `search.py:3117-3125` |
| `retrieval_count`, `citation_count`, `misled_count` | entity columns | **USED in retrieval scoring** | `usage_retention_multiplier` `temporal.py:197-205`: `+0.02×min(retrieval,50)`, `+0.12×min(citation,20)`, `−0.6×min(misled,5)`, clamped `[0.1, 4.0]`; applied as `decay_days × multiplier` at `:226`. A heavily cited memory decays up to 4× slower; `misled_count ≥ 5` decays 10× faster. **`misled_count` is a live negative-feedback channel** |
| `created_at` | entity column, indexed | **USED** — freshness boost, decay anchor, tiebreak | `_freshness_boost` `search.py:3116, 3129-3136` = `min(cap, 1 + 0.5/(1+age_days))`; last resort in the decay chain `temporal.py:113`; `ORDER BY … created_at DESC, uuid DESC` in every lane (`:1030, 1155, 1194, 1408, 1441, 1492, 1521`) |
| `updated_at` | entity column, in 5 indexes | **STORED, UNREAD by retrieval** | One hit in `retrieval/`: `dedup.py:307`, inside a maintenance scan. `_freshness_boost` takes `created_at` only; `updated_at` is not in the decay chain and not in `_selected_record_metadata` (`search.py:2376-2407`), so it never reaches a candidate. Its 5 indexes (`schema.py:120, 123-126`) serve `list_by_type` only. **An edited memory does not become fresher to the ranker** |
| `spans` (`attributes.agent_spans`) | entity attributes | **USED indirectly** | Consumed at projection to mint `passage` rows (`projection/passages.py`); passages are first-class retrieval targets and collapse into their parent at `context.py:1296-1298` |
| `atomic` (`attributes.agent_atomic`) | entity attributes | **USED indirectly** | Suppresses mechanical cutting, keeping the memory one retrievable unit |
| `probes` (`attributes.memory_probes`) | entity attributes | **STORED, UNREAD** | Write-time diagnostic only (`rehearsal.py:113-121`). No retrieval lane reads `memory_probes`: not query expansions, not alternate embeddings, not synonyms |
| Raw lifecycle (`review_state`, `superseded_by_*`) | `raw_captures` | **USED on the raw lane; UNENFORCED on the graph lane** | Raw: `raw_memory_recallable` (`memory_pipeline/lifecycle.py:62-85`, wired `services/surreal_content.py:545-550`, called `:606, 1892, 2173`). Synthesis re-filters at render (`services/synthesis.py:116-119, 481-492, 555-580`). The graph lane has no equivalent |

### 4.1 The short version

**Read by retrieval:** `memory_scope` (via attributes), `project_id`, `status`,
`retrieval_keys_normalized`, edge predicate `name`, edge `fact` and
`fact_embedding`, `entity_type` (filter/pools/tie-rank/two boosts), `created_at`,
all five usage counters and timestamps, spans and `atomic` via passages.

**Stored and never read by retrieval:** `tags`, `priority`, `updated_at`,
`probes`, `basis`, `confidence`, edge `weight`, edge `invalid_at`, edge
`expired_at`, edge `episodes`, the `node_labels` filter, and the
`entity.memory_scope` column itself.

**Read only by the retention job:** `importance`, `pinned`, `retention`.

---

## 5. Temporal, provenance, correction

### 5.1 Temporal expressiveness

On the **edge**: a genuine bi-temporal model exists —
`valid_at`/`invalid_at` (valid time) and `created_at`/`expired_at` (transaction
time) (`schema.py:190-193`), queryable through
`temporal_query(mode=history|timeline|conflicts)` (`tools/temporal.py:36-120`)
and `/search` conflicts mode (`apps/api/src/sibyl/api/routes/search.py:553-558`).

On the **node**: nothing. `entity` has no `valid_at`/`invalid_at` columns. The
`Episode` model declares `valid_from`/`valid_to` (`models/entities.py:204-209`)
and the storage edge does not write them (`graph.py:3026-3051`), so they land in
`attributes` as untyped metadata.

And a writer cannot set edge temporal bounds anyway, because a writer cannot
create a typed edge (§1.4). `valid_at` is populated only by reflection promotion
(`services/memory.py:3213-3222`).

### 5.2 Provenance

Raw side: a dedicated `provenance` FLEXIBLE column
(`10_tables.surql:139`) plus `principal_id`, `agent_id`, `capture_surface`,
`created_by_user_id`, and a `derived_from` relation to `source_imports`.

Graph side: `created_by`/`modified_by` (`schema.py:88-89`), `source_file`, and
`attributes`. `native_write_path` is stamped on generated relationships
(`services/memory.py:3180, 3190, 3204`). There is **no provenance column on
`relates_to`** — an edge cannot say who asserted it or on what evidence except
through `attributes` and `episodes`, and `episodes` is unread (§4).

### 5.3 What `sibyl correct` actually does

Ten actions (`services/memory.py:186-205`, literal type at
`apps/api/src/sibyl/api/schemas/common.py:20-31`): `delete`, `mark_duplicate`,
`mark_stale`, `mark_wrong`, `restore`, `supersede`, `hide`, `mark_sensitive`,
`redact`, `revise`. Aliases `duplicate | stale | superseded | wrong`
(`memory.py:200-205`). `delete` and `redact` are irreversible (`memory.py:216`).

**What it touches:** `apply_memory_correction` (`services/memory.py:1652-1732`)
builds a `replace(memory, ...)` and calls `save_raw_memory`. It mutates three
things on the **raw** row: `raw_content` (revise only), `review_state`, and
`metadata` — writing `metadata["superseded_by_source_id"]` (`memory.py:1598`), a
`MemoryLifecycle` record (`:1607`), a correction-history entry (`:1621`), and a
`ReflectionFinding` (`:1637-1640`). On `supersede` it passes
`superseded_by_memory_id`, which can materialize the content-layer `supersedes`
relation (`content_schema.py:301-312`).

**What it does not touch:** the graph. No `relates_to` write, no `entity` update,
no reprojection job. The route reports `affected_records=[f"raw_captures:{id}"]`
only (`apps/api/src/sibyl/api/routes/memory.py:2585-2591`).
`_correction_derived_ids` (`memory.py:1236-1250`) *lists* the promoted entity and
relationship ids and nothing mutates them. `_correction_impact`
(`memory.py:1333-1352`) returns `{"excluded_from_recall": ...}` — a **declaration
about** exclusion, not an enforcement of it.

Note also that the physical `supersedes` table is materialized only by
`_materialize_supersedes_lineage` (`services/surreal_content.py:1485, 1508,
1518-1526`) reading `metadata.supersedes_raw_memory_id`, whose only writer is the
source-import job (`apps/api/src/sibyl/jobs/source_imports.py:542`). No
agent-facing parameter sets that key.

Enforcement is split three ways: the raw recall lane honors lifecycle
(`surreal_content.py:545-550`); synthesis re-filters at render
(`synthesis.py:481-492`); the graph retrieval lane honors nothing (G1).

### 5.4 Contradiction and consolidation

Contradiction detection exists in exactly one form: a regex over an 11-word
polarity vocabulary (`services/reflection.py:145-161` — enabled/disabled,
allowed/blocked, required/forbidden, use/avoid/skip/disable/enable) firing a
`ReflectionFindingKind.CONTRADICTION` when two candidates share a subject with
opposite polarity (`reflection.py:664-678`, reason
`"same_subject_opposite_polarity"`, confidence 0.82). The action is
`route_to_review` — it resolves nothing. Supersession and staleness detection are
explicit-signal-driven (`reflection.py:631-663`).

`ClaimRecord` (`models/reflection.py:121-181`) is the richest structure in the
codebase for this: `supports_source_ids`, `contradicts_source_ids`,
`supersedes_source_ids`, `superseded_by_source_id`, `validity`, `freshness`,
`confidence`. Only `supersedes_source_ids` ever becomes a graph edge
(`services/memory.py:3207-3226`). `contradicts_source_ids` and
`supports_source_ids` never become `CONTRADICTS` or `SUPPORTS` edges, though both
predicates exist in the enum and both carry high expansion weights (0.64 default
and 0.94 respectively).

---

## 6. Gaps an agent hits

### G1. A correction cannot reach the memory retrieval serves

`sibyl correct <id> --action mark_wrong` corrects the `raw_captures` row. The
projected `entity` row keeps its capture-time metadata, has no `review_state` or
`lifecycle_state` column (`schema.py:75-108`), and is never reprojected. Nothing
in `retrieval/` joins a graph row back to its raw memory: `raw_memory_id` and
`raw_source_id` are written into graph metadata at `capture.py:137-140`, and
grepping either name across `retrieval/` and `tools/` returns zero hits. The
wrong memory keeps ranking, keeps its embedding, keeps being expanded into. The
only backstop is the synthesis render filter, which reads metadata the graph row
does not carry.

The system has a correction verb whose blast radius stops at the substrate
retrieval mostly does not read.

### G2. No typed edge between two memories at write time

An agent that knows "this supersedes that" or "this claim contradicts that
decision" has one channel: `related_to`, which becomes `RELATED_TO`
unconditionally (`tools/add.py:720`, `services/memory.py:3196-3205`).
`CONTRADICTS`, `SUPPORTS`, `CONFLICTS_WITH`, `DECIDES`, `BREAKS`, `ENABLES`,
`REQUIRES` have no writer at all.

The sharp edge: retrieval *would* use it. The expansion weight table
(`search.py:85-107`) already assigns `DECIDES` 1.0, `REQUIRES` 0.98, `SUPERSEDES`
0.95, `SUPPORTS` 0.94 — a tuned scoring table for predicates the write surface
cannot produce. Traversal is typed; the write surface is not. The one typed
relationship dataclass in the codebase
(`ReflectionRelationshipRecord`, `models/reflection.py:237-245`) is populated
with a single hardcoded `BELONGS_TO` (`services/reflection.py:506-524`) and never
reaches the graph.

### G3. Supersession is expressible, unenforced, and inverted in ranking

Three compounding problems. The traversal queries carry no temporal predicate
(`search.py:1826-1868, 1871-1922`), so an edge with `invalid_at` set is walked
like a live one. `invalid_at` and `expired_at` are projected all the way into
`SearchResult.metadata` (`:2394, 2420-2421`) and consulted by nothing in the
fusion or boost path. And `SUPERSEDES` carries a weight of **0.95**, one of the
highest in the table — so following a supersession edge *boosts* its target,
which is the superseded memory. Retrieval treats "X replaced Y" as a strong
reason to also surface Y.

### G4. Confidence and epistemic basis have nowhere to live and nothing to read them

Per-item confidence is produced in four places — the extractor
(`models/memory_extraction.py:39`), `ClaimRecord` (`models/reflection.py:125`),
`ReflectionFinding` (`:189`), and `Pattern` (`models/entities.py:153`) — and
reaches no column. `entity` has no `confidence`; `relates_to` has no
`confidence`. It survives only in `attributes`, where the only readers are
display projections (`search.py:2390, 2416`). A low-confidence memory ranks
identically to a certain one.

`--basis` is worse, because it is a well-designed axis with a validated
four-value vocabulary (`main.py:225`) that an agent can use to distinguish what
it *observed* from what it *assumed* — and its only consumer in the entire system
is a blame view (`routes/memory.py:1978`).

Same shape for edge weight: not persisted, no column, always 1.0 (§2.3).

### G5. Tags are a write-only axis on the retrieval path

`tags` is the most natural thing for an agent to set and the least load-bearing
thing it can set. Unindexed (`schema.py:104`), absent from every search WHERE
clause, absent from every scoring term. `list_by_type` accepts a `tags` argument
and emits no SQL for it (`graph.py:1104, 1143-1186`); the only real filter is a
Python post-filter reached from `explore()` (`graph_search.py:99-100`,
`tools/explore.py:241`). An agent tagging a memory `gotcha` and later searching
for gotchas gets zero lift — the tag helps only if the reader already knows to
*browse* by it.

### G6. Eight entity types can never appear in a context pack

`FACET_TYPES` (`tools/context.py:77-90`) maps 12 facets to entity types and
`_types_for_facets` (`:1221-1239`) builds the requested-type list from exactly
that map plus `passage`. Absent from every facet: `person`, `place`, `language`,
`team`, `milestone`, `community`, `knowledge_source`, `slash_command`. `person`
and `place` are the pointed ones — they were added deliberately for
domain-general personal memory (`models/entities.py:54-55`) and `recall` can
never request them.

### G7. Structural expressiveness the schema lacks outright

- **N-ary relations.** `relates_to` is `IN entity OUT entity` (`schema.py:179`).
  "We decided X, in meeting Y, because of constraint Z" must be decomposed into
  pairwise edges or flattened into prose.
- **Negation.** No polarity field on node or edge. "Redis is *not* the
  bottleneck" is stored as text and embeds close to "Redis is the bottleneck".
  The only negation-aware machinery in the system is the 11-word regex vocabulary
  in reflection (`services/reflection.py:145-161`).
- **Entity aliasing / merge.** No `same_as` predicate, no merge verb, no
  canonical-id indirection. Extraction dedupes only within a single batch by
  `f"{type}:{name.lower()}"` (`projection/memory.py:849`), so "Bliss",
  "Stefanie", and "stef@gradial.com" become three permanent nodes.
- **TTL / explicit decay.** `raw_captures` has `purge_after`
  (`10_tables.surql:145`); `entity` has none. Decay is job-driven
  (`jobs/consolidation.py:319-331`) with no writer-facing knob beyond untyped
  `attributes.importance` or `attributes.pinned`.
- **Probes as retrieval assets.** The single most direct statement an agent can
  make about *how this memory should be found* is rehearsed once at write time
  and then inert. Not indexed, not embedded, not used as query expansions
  (`rehearsal.py`).
- **Edit-time freshness.** `updated_at` is written and indexed five ways and read
  by no ranker (`search.py:3129` takes `created_at` only). Revising a memory
  cannot make it more current to retrieval.

---

## 7. Summary judgement

The write surface is asymmetric in a specific way: **rich about the shape of a
single memory, nearly mute about relations between memories.** Spans, atomicity,
probes, retrieval keys, and scope are first-class, validated, unforgeable
declarations with real server-side enforcement. Predicates, confidence, validity
intervals, epistemic basis, and supersession are either absent from the contract
or reachable only through internal code paths.

Retrieval mirrors the asymmetry from the other side, and is better than the write
surface deserves in two places and worse in three. Better: the graph expansion
lane is genuinely predicate-aware with a tuned 21-entry weight table, and the
usage-feedback loop is real — citations stretch a memory's half-life up to 4×
and `misled_count` collapses it 10×, both feeding live ranking. Worse: no type
prior, no tag signal, no confidence term, no temporal filter on edges, and no
path from a corrected raw memory to the graph row that answers for it.

The clearest single statement of the gap: **the predicate weight table
(`search.py:85-107`) is a wish list.** It scores `DECIDES`, `SUPPORTS`,
`REQUIRES`, `BLOCKS`, and `VALIDATED_BY` — five predicates no agent, and no
extractor, can ever write.
