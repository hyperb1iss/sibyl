# Sibyl tech-debt audit: retrieval, ranking, synthesis, ai/extraction

Scope: `packages/python/sibyl-core/src/sibyl_core/` — `retrieval/`, `ai/`, `projection/`,
`memory_pipeline/`, `embeddings/`, `query_anchors.py`, the search/synthesis/reflection services, and
context-pack assembly in `tools/context.py` + `tools/search.py`. Out of scope (owned elsewhere):
graph client/models/persistence, `apps/api`, `apps/web`.

Audit date 2026-08-13, against `main` at `5388d986`. Read-only; nothing mutated.

All paths below are absolute. Every claim was checked by reading the file or by grep over the whole
repo; call-site counts are stated where "dead" is claimed.

---

## Section 0: the shape of the problem

Three numbers frame the rest of this document.

**The ranker carries 51 hand-tuned numeric constants and 46 regex/term-set constants** in one file,
`/Users/bliss/dev/sibyl/packages/python/sibyl-core/src/sibyl_core/retrieval/query_ranking.py` (2016
lines). Counted with `grep -cE "^_[A-Z_]+ *(:[a-z ]*)?= *[0-9]"` and
`grep -cE "^_[A-Z_]+ *(:.*)?= *(re\.compile|\{|frozenset|\()"`.

**Config for retrieval is spread across seven independent surfaces** that never reconcile:
`CoreConfig` (pydantic-settings, env-driven), `RetrievalWeights` (8 fields), `CandidateLimits` (8),
`RetrievalPlan` (16), `HybridConfig` (15), `FusionConfig` (3), `DedupConfig` (5), `TemporalConfig`
(4), `CrossEncoderConfig` (7), plus 15 module constants in `retrieval/search.py` and the 97 in
`query_ranking.py`. Three of those dataclasses have zero production readers (see 2.4).

**Fusion discards every score magnitude the lanes compute.** `rrf_merge` binds the score to
`_original_score` and never reads it
(`/Users/bliss/dev/sibyl/packages/python/sibyl-core/src/sibyl_core/retrieval/fusion.py:126`). The
coverage re-ranker then weights ordinal rank at 0.95 and the incoming prior at 0.04
(`query_ranking.py:140-141`). The pipeline is ordinal end to end. Most of the carefully-tuned
multiplicative constants downstream cannot change an outcome except by perturbing an ordering that
is immediately re-flattened. This is the single most important thing to internalize before 1.3
tuning work.

---

## BLOCKER

### B1. LongMemEval corpus vocabulary is hardcoded into the production ranker

`/Users/bliss/dev/sibyl/packages/python/sibyl-core/src/sibyl_core/retrieval/query_ranking.py:337-705`

`_CONCEPT_GROUPS` (561-705), `_CATEGORY_ALIASES` (480-510), and roughly twenty `_*_QUERY_PATTERN` /
`_*_EVIDENCE_PATTERN` regex pairs (337-479) encode benchmark content directly as ranking signal.
Verbatim examples:

- `query_ranking.py:698` — `"seattle"` is a member of a concept group.
- `query_ranking.py:586-610` — a concept group containing `basil`, `airfryer`, `tomatoes`, `mint`,
  `smoker`, `blender`.
- `query_ranking.py:617` — `"buisiness"`, a **misspelling** sitting beside `"business"` in the same
  frozenset. That is a dataset typo promoted into a shipped scoring table.
- `query_ranking.py:366-374` — `_HOMEGROWN_EVIDENCE_PATTERN` matching
  `basil|mint|tomatoes?|herbs?|pepper plants?`.
- `query_ranking.py:425-437` — `_DOCTOR_VISIT_EVIDENCE_PATTERN` matching
  `dermatologist|dentist|optometrist`.
- `query_ranking.py:385-395` — `_SPORTS_EVENT_EVIDENCE_PATTERN` matching
  `5k|triathlon|soccer tournament|bike ride`.

These feed `_concept_overlap_score` (1647) and `_query_frame_score` (946), which enter the final
score at `_CONCEPT_OVERLAP_WEIGHT` and `_QUERY_FRAME_WEIGHT` = 0.52 (`query_ranking.py:1375-1378`) —
the second-largest weight in the entire model after rank itself.

Why it matters: this is corpus overfitting shipped to every user of every org. A Sibyl user asking
about a work incident gets scored against a table that rewards the token `airfryer`. It also poisons
the benchmark: any LongMemEval delta measured while these tables are live is partly measuring the
tables, not the architecture. The 30.38% anchor is not a clean read of the retrieval design.

Fix: L. Not a delete-and-ship — the tables are load-bearing for the current benchmark number, so
removing them will move it. The honest sequence is: measure with them removed, accept the drop as
the true baseline, then rebuild the concept signal from something learned or embedded rather than
enumerated.

Severity: blocker. This is the finding that should gate 1.3 rearchitecture.

### B2. Two mutually-unaware retrieval pipelines with divergent tuning

There are two complete, separately-tuned retrieval paths:

- `/Users/bliss/dev/sibyl/packages/python/sibyl-core/src/sibyl_core/retrieval/search.py:367`
  `context_search` — 8 lanes, `RetrievalWeights`, `CandidateLimits`, Surreal-or-Python RRF,
  `_boost_score` multipliers. Reached from `tools/context.py:1489` (context packs).
- `/Users/bliss/dev/sibyl/packages/python/sibyl-core/src/sibyl_core/retrieval/hybrid.py:584`
  `hybrid_search` — 2 lanes, `HybridConfig`, weighted RRF, keyword boost, temporal boost,
  cross-encoder hook. Reached from `tools/search.py:1074` (`/api/search`, CLI `sibyl search`).

They share exactly one component: `rank_items_by_query_coverage`. Everything else is duplicated with
different constants. The clearest instance is the graph relationship priority table, which exists
twice with different scales and partially disjoint keys:

- `search.py:85-107` `_GRAPH_EXPANSION_RELATIONSHIP_WEIGHTS`, range 0.58–1.0, includes `DECIDES`,
  `SUPPORTS`, `TOUCHES`, `PRODUCES`, `ABOUT`, `SHARES_COMMUNITY`.
- `hybrid.py:44-64` `DEFAULT_GRAPH_RELATIONSHIP_TYPE_WEIGHTS`, range 0.35–1.25, includes
  `APPLIES_TO`, `ENABLES`, `BREAKS`, `CONFLICTS_WITH`.

`MENTIONS` is 0.58 in one and 0.35 in the other. `RELATED_TO` is 0.64 vs 0.85. Nothing reconciles
them, and no test asserts they agree.

Consequence: a memory that surfaces in a context pack may not surface in `sibyl search` for the same
query, and vice versa, for reasons nobody can predict from either file alone. Every ranking
experiment has to be run twice or it only covers half the product.

Fix: L. Severity: blocker for 1.3 (it is the reason lever screening keeps needing two harnesses).

### B3. The exact-name rescue lane is unreachable on the normal path

`/Users/bliss/dev/sibyl/packages/python/sibyl-core/src/sibyl_core/tools/search.py:1144-1159`

```python
if (
    query
    and not graph_search_failed
    and not enhanced_search_exhausted
    and not _graph_results_contain_exact_name_match(raw_results, query)
):
    ...
    exact_name_results = await ... entity_manager.search_exact_name(...)
```

`enhanced_search_exhausted` is set at `tools/search.py:1099-1101` from
`hybrid_result.metadata["entity_manager_search_completed"]`, which `hybrid.py:267` sets to `True`
whenever the seed vector search _succeeded_ — the normal case.

So the guard reads: run the exact-name rescue only when the seed search **failed**. The
`_graph_results_contain_exact_name_match` check on the next line makes the intent unambiguous: this
lane exists to rescue a query whose exact-name target is missing from an otherwise-successful result
set. That is precisely the case where it never fires.

Why it matters: "search for the thing by its exact name" is the highest-precision query a memory
system gets, and the lane built for it is dark in production. It also means any benchmark claim
about exact-name behavior measured through `/api/search` is measuring a lane that did not run.

Fix: S (the guard should test `not raw_results`-style exhaustion, not seed success). Severity:
blocker — confirm intent with whoever wrote it, but the current behavior does not match the
surrounding code's own stated purpose.

---

### B4. The passage retire sweep breaks early and strands spans of deleted memories

`/Users/bliss/dev/sibyl/packages/python/sibyl-core/src/sibyl_core/projection/passages.py:576-608`

```python
highest_kept = max(kept_indices, default=-1)
for index in range(MAX_PASSAGES_PER_SOURCE):
    if index in kept_indices: continue
    try: removed = await delete(passage_entity_id(source_id, index))
    except Exception: break
    if removed: retired += 1
    elif index > highest_kept: break
```

The docstring at 589-590 asserts "the walk stops once it is past every kept index and has found an
absence, which is where any previous run must have ended." That premise fails whenever a
**previous** run skipped an index, and the module contains the code that produces such gaps: the
oversize-leaf branch at `passages.py:222-227` `continue`s past an index it cannot store.

Two concrete failures, both verified by reading the code path:

**Stale span survives reprojection.** Run 1 writes `{0, 1, 3}` (index 2 was an unbreakable oversize
line). The body is shortened; run 2 writes `{0, 1}`. Now `highest_kept = 1`, index 2 deletes
nothing, `2 > 1` fires, loop breaks. Index 3 survives holding the **previous revision's text** and
keeps being served as current. `reproject_entity_passages`'s own docstring (`passages.py:391-397`)
names this as the outcome it exists to prevent.

**Delete leaves every span alive.** `retire_entity_passages` (`passages.py:558-573`) calls the same
helper with `kept_indices=frozenset()`, so `highest_kept = -1`. If index 0 was skipped as an
oversize leaf, `delete(index 0)` returns falsy, `0 > -1` fires, and the loop breaks on its first
iteration. Every span of the deleted memory survives, still serving the deleted text. The function's
docstring (565-567) calls that "the one outcome a delete must not produce."

I read `passages.py:576-608` directly to confirm the control flow; the `elif` is exactly as quoted.

Fix: S. Either drop the `elif` and eat 64 deletes on the delete path (batchable), or bound the sweep
by the parent's last-known `passage_total`.

Severity: blocker — this is a correctness bug that serves deleted and superseded content as current,
and it is silent.

### B5. The self-feeding-source guard runs only on sources that get thrown away

`/Users/bliss/dev/sibyl/packages/python/sibyl-core/src/sibyl_core/services/synthesis.py:1013,1023`
vs `:566-672`

`_without_self_feeding_sources` is applied to the searched sources (1013) and the neighborhood
sources (1023) inside `plan_synthesis`. Then `materialize_synthesis_section_packs` rebuilds every
pack from `context_fn` output in the loop at 566-656 — a loop that has **no**
`_is_self_feeding_source` check anywhere — and returns
`replace(run, source_packs=materialized_packs, ...)` at 667-672, replacing the plan-stage packs
wholesale.

Every production caller runs plan then materialize:
`apps/api/src/sibyl/api/routes/synthesis.py:98-112` and
`packages/python/sibyl-core/src/sibyl_core/tools/synthesis.py:90-104`. So the guard landed by
`4bb5260d` ("never select synthesis or reflection output as a source", #354) protects only the
sources that are discarded.

The loop is closed and live. `remember_synthesis_artifact` (`synthesis.py:928-960`) stores the
artifact via `remember_raw_memory` with `capture_surface="synthesis_artifact"`, and
`compile_context` recalls raw memories (`tools/context.py:1519`). The signal is even deliberately
preserved: `capture_surface` sits in `_ITEM_METADATA_KEYS` (`tools/context.py:600`) under a comment
saying "synthesis render filters read these." Nothing reads it.

I read `synthesis.py:560-672` directly and confirmed the materialize loop's filter set: hidden,
render-authorization, correction-reason, redaction, and duplicate-source-id. No self-feeding check.

Fix: S — one filter call in the materialize loop. Severity: blocker. It is the regeneration-loss
loop the gate was written to close, and the fix landed one stage upstream of where it mattered.

### B6. The handbook gate that "proved" B5 tests a stage production never renders

`/Users/bliss/dev/sibyl/packages/python/sibyl-core/tests/test_handbook.py:70-77, 260-285`

`_plan(...)` calls `plan_synthesis` alone, and **every** test in the file asserts on its output —
including the byte-stability test at 221 and the line-tracing gate at 288.
`materialize_synthesis_section_packs` never appears in the file. The handbook's entire test suite
validates a pack shape the REST route does not render.

This is the mechanism by which B5 shipped green, and it generalizes: any future synthesis invariant
asserted through `_plan` is asserting on discarded data.

Fix: M (add a materialize stage to the gate fixtures). Severity: blocker.

---

## MAJOR

### M1. Two age-decay functions stacked on the same candidate, pulling opposite ways

`/Users/bliss/dev/sibyl/packages/python/sibyl-core/src/sibyl_core/retrieval/search.py:3115-3123`

```python
boosted *= _freshness_boost(candidate.created_at, cap=plan.weights.freshness_boost_cap)
temporal_multiplier = (
    1.0 if temporal_target is not None
    else temporal_decay_multiplier(candidate, decay_days=core_config.temporal_decay_days)
)
boosted *= temporal_multiplier
```

`_freshness_boost` (`search.py:3129-3137`) is `min(cap, 1 + 0.5/(1+age_days))` — a hyperbolic
_boost_ capped at 1.5. `temporal_decay_multiplier` (from `retrieval/temporal.py`) is exponential
_decay_ on a 365-day half-life. Both are functions of the same `created_at`, applied
multiplicatively, configured by two unrelated knobs (`freshness_boost_cap` on the plan,
`temporal_decay_days` on `CoreConfig`). Their product is a curve nobody designed.

A second copy of `_freshness_boost` runs at `search.py:3147` with a **hardcoded** `cap=1.5` to
populate the `freshness` receipt field, so if `plan.weights.freshness_boost_cap` is ever changed
from its default the reported freshness will disagree with the applied one.

Fix: M (pick one decay model, delete the other, wire the receipt to the applied value). Severity:
major.

### M2. The coverage ranker runs twice on every query, in Python, on the serving path

`/Users/bliss/dev/sibyl/packages/python/sibyl-core/src/sibyl_core/retrieval/query_ranking.py:859-892`

`rank_items_by_query_coverage` scores the full candidate set through `rank_by_query_coverage`, then
unconditionally rebuilds the candidates and scores the whole set **again**
(`refined = rank_by_query_coverage(...)` at 883), then throws the second pass away unless
`should_accept_query_coverage_refinement` approves it (889).

Each pass is pure-Python and per-candidate does: tokenization, three separate token-set extractions,
sliding-window segment overlap at window 18 / stride 6 (`_best_segment_overlap`,
`_best_weighted_segment_overlap`), IDF weighting, ~20 regex searches through `_query_frame_score`,
and fact-frame matching. It is offloaded to a thread (`search.py:568`, `hybrid.py:854`) which keeps
the loop alive but does not make it cheaper — and under CPython the GIL means the thread competes
with the event loop.

Given the measured clean-server latency (ent ~32s, web ~18s against a 10s budget), this is a named,
measurable, unconditional 2x on the one stage that is entirely local CPU. The refinement pass is a
candidate for gating on a cheap precondition rather than running always.

Fix: M. Severity: major, and it is the cheapest latency win visible in this subsystem.

### M3. Round-robin facet interleaving discards the global ranking

`/Users/bliss/dev/sibyl/packages/python/sibyl-core/src/sibyl_core/tools/context.py:790-804`

```python
for item_index in range(per_facet_limit):
    for section_index, section in enumerate(unique_sections):
        ...
        item = section.items[item_index]
        selected[section_index].append(item)
        remaining -= 1
```

Selection takes item 0 from every facet, then item 1 from every facet, and so on until the pack
budget is spent. Score never enters the loop. A top-scoring item sitting at rank 3 within a strong
facet loses its slot to the rank-0 item of a weak facet.

Everything upstream — RRF, four multiplicative boost classes, the 15-term coverage model, the seven
stabilizer branches — feeds an ordering that this loop then reshuffles by facet position. Combined
with `per_facet_limit = max(2, min(8, ...))` at `context.py:1540`, the pack cap is 8 per facet
regardless of how much char budget is left, which matches the previously-diagnosed "8-item pack cap
against a 1/3-spent char budget".

Fix: M (score-ordered selection with a per-facet floor, rather than strict round-robin). Severity:
major.

### M4. Four expansion lanes issue sequential round trips, two of them against

tables nothing in the write path populates

`/Users/bliss/dev/sibyl/packages/python/sibyl-core/src/sibyl_core/retrieval/search.py:1693-1744`

Per BFS depth level, `_node_bfs_records` awaits in strict sequence: `_mentioned_entity_hops` (1702),
`_relation_target_hops` (1711), optionally `_relation_source_hops` (1722), optionally
`_community_member_hops` (1737) — and `_community_member_hops` itself awaits
`_community_ids_for_entities` first (`search.py:1935`), so it is two round trips. All four read from
the same frozen `entity_frontier`/`episode_frontier` for that level; they are fully independent and
could be one `asyncio.gather`. That is up to 5 serialized DB round trips where 1 latency-unit would
do.

Two of them cannot return anything on a live graph:

- `_mentioned_entity_hops` (`search.py:1791`) queries `FROM mentions`. Grep across
  `sibyl_core/graph`, `sibyl_core/services`, `sibyl_core/backends` finds no writer for the
  `mentions` table outside `backends/surreal/schema.py` (definitions/indexes) and the archive tuple
  `GRAPH_EDGES` at `schema.py:574`. `mentions` is restore-only.
- `_community_member_hops` (`search.py:1925`) needs `entity_type = "community"` rows. Community
  entities are created only by `store_communities` (`services/graph_communities.py:2704`), and
  `detect_communities` / `store_communities` have **zero callers** anywhere in `packages/` or
  `apps/` outside their own module. No ingestion path, no worker, no job builds communities. The
  only importers of `graph_communities` are three web-visualization helpers in
  `apps/api/src/sibyl/persistence/graph_runtime.py`.

So every context-pack search pays three guaranteed-empty round trips (one `mentions`, two community)
before the useful hops run.

Fix: S for the gather; S for gating the two dead lanes behind a capability probe or deleting them.
Severity: major (pure latency on the loudest budget miss).

### M5. `context_search`'s three retrieval phases are serialized

`/Users/bliss/dev/sibyl/packages/python/sibyl-core/src/sibyl_core/retrieval/search.py:458, 472, 490`

The critical path is: gather 4 lexical lanes (458) → **await** → embed query + gather 2 vector lanes
(472) → **await** → graph expansion (490). Only the third phase has a real data dependency (it seeds
from the first two). The lexical gather and the vector gather are independent and are executed one
after the other. The embedding call inside phase 2 (`search.py:1281`) is itself a network round trip
that could overlap the entire lexical phase.

Fix: S (gather phases 1 and 2, keep expansion downstream). Severity: major.

### M6. The context-pack vector lane has no embedding timeout

`/Users/bliss/dev/sibyl/packages/python/sibyl-core/src/sibyl_core/retrieval/search.py:1281`

```python
embeddings = await embedding_provider.embed_texts([plan.query], input_kind="query")
```

Bare await, no `asyncio.wait_for`. `CoreConfig.graph_search_embedding_timeout_seconds` exists
(`config.py:271`, default 5.0s) and **is** applied on the other path (`services/graph.py:783`), so
the two retrieval pipelines disagree about whether a hung embedding provider can stall a request. On
the context-pack path it can, up to whatever outer timeout the HTTP layer imposes.

Fix: S. Severity: major.

### M7. `compile_context` swallows every retrieval exception into a warning

`/Users/bliss/dev/sibyl/packages/python/sibyl-core/src/sibyl_core/tools/context.py:1557-1571`

```python
try:
    sections = await _compile_native_sections(...)
except Exception as exc:
    retrieval_failed = True
    log.warning("context_native_search_failed", error_type=type(exc).__name__)
```

A bug anywhere in the 3200-line native retrieval path becomes a `WARNING` line with the exception
_type_ only (no message, no traceback) and an empty or fallback-sourced pack. The `ContextPack`
returned carries no field saying retrieval failed — `usage_metadata` only gets an exposure summary.
A caller cannot distinguish "no relevant memories" from "retrieval crashed".

The contrast is instructive: the vector lane one level down does this correctly.
`VectorCandidateFetch` (`retrieval/candidates.py:83-127`) carries `requested`, `attempted`,
`reason`, `failures`, and surfaces them into `SearchResponse.filters` via
`vector_fetch.as_metadata()` (`search.py:603`). That receipt discipline stops at the
`compile_context` boundary.

Fix: S (propagate a `retrieval_failed` receipt into the pack). Severity: major.

### M8. Seven ranking stabilizer branches, one of them tested

`/Users/bliss/dev/sibyl/packages/python/sibyl-core/src/sibyl_core/retrieval/query_ranking.py:1472-1497`

Final ordering is chosen by an if/elif chain over regex query classifiers, selecting one of seven
mutually exclusive stabilizers:

| Stabilizer                             | Definition | Direct test coverage                           |
| -------------------------------------- | ---------- | ---------------------------------------------- |
| `_stabilize_fact_frame_ranking`        | 1893       | none                                           |
| `_stabilize_preference_ranking`        | 1839       | none                                           |
| `_stabilize_evidence_set_ranking`      | 1850       | none                                           |
| `_stabilize_artifact_evidence_ranking` | 1872       | none                                           |
| `_stabilize_temporal_evidence_ranking` | 1862       | none                                           |
| `_rank_preserving_window`              | 1789       | none                                           |
| `_stabilize_top_window_ranking`        | 1943       | none                                           |
| `_apply_evidence_cluster_affinity`     | 1682       | none                                           |
| `_stabilize_explicit_anchor_ranking`   | 1798       | `tests/test_retrieval_advanced.py:429,449,471` |

Verified by grepping each name across `packages/python/sibyl-core/tests`, `apps/*/tests`, and
`tools/tests`. Only the explicit-anchor stabilizer has direct tests. The branch _selection_ logic
(which classifier wins when a query matches two patterns, e.g. a preference query that is also a
temporal-instruction query) has no test at all. The only dedicated `query_ranking` test file,
`tests/test_query_ranking_semantic_rescue.py` (104 lines), tests the dead experiment arm described
in M9.

Why it matters: these branches decide the final order of every search result in the product, and the
invariants they encode ("a preference query must not let a generic assistant turn outrank the user's
own statement") live only in the code. Any 1.3 refactor will break them silently.

Fix: M (characterization tests per branch before any rewrite). Severity: major.

### M9. Dead experiment arm wired into the production ranker

`/Users/bliss/dev/sibyl/packages/python/sibyl-core/src/sibyl_core/retrieval/query_ranking.py:1148, 1395-1407`

`rank_by_query_coverage(..., semantic_prior_rescue_weight: float = 0.0)` plus a 13-line scoring
block guarded by `if semantic_prior_rescue_weight > 0.0`.

`rank_items_by_query_coverage` — the _only_ entry point used by either production pipeline
(`search.py:41`, `hybrid.py:155`) — does not accept or forward the parameter (signature at
`query_ranking.py:816-824`). The arm is therefore unreachable from every production caller. Every
non-test reference is under `benchmarks/` (`longmemeval_v2_official.py:92,743,936`,
`longmemeval_v2_live_retrieval.py:77,327`, `longmemeval_v2_memory/sibyl_memory.py` in eight places)
plus `tests/test_query_ranking_semantic_rescue.py`.

This matches the recorded verdict that the additive arm was NO-GO at reader level. The residue is a
parameter, a scoring branch, and a 104-line test file living in the hottest function in the
subsystem.

Fix: S. Severity: major (it is small, but it is exactly the accreted-arm class the 1.3 map is meant
to surface).

### M10. Cross-encoder reranking: 531 lines, off by default, one branch dead entirely

`/Users/bliss/dev/sibyl/packages/python/sibyl-core/src/sibyl_core/retrieval/reranking.py`

- `cohere_rerank` (`reranking.py:443-531`, ~88 lines) has **zero** call sites anywhere in the repo —
  not in `packages/`, `apps/`, `benchmarks/`, `tools/`, or any test. It also reads `COHERE_API_KEY`
  directly from `os.environ` (`reranking.py:469`), bypassing `CoreConfig` entirely.
- `rerank_results` / `cross_encoder_rerank` are reachable from exactly one production site,
  `hybrid.py:797-807`, behind `config.apply_reranking`, which is fed from
  `core_config.rerank_enabled` (`tools/search.py:1067`), default `False` (`config.py:303`).
  Re-ranking was screened and refuted.
- The feature-weighted reranker (`fit_feature_weighted_reranker`, `rerank_by_feature_weights`,
  `FeatureWeightedRerankModel`) is used only by `sibyl_core/evals/longmemeval_replay.py:221-577`.
  That is benchmark machinery shipping inside the published `sibyl-core` package, not a dev-only
  tree.
- The knob is declared three times: `CoreConfig.rerank_model` / `rerank_top_k` (`config.py:311,315`)
  and again as `HybridConfig.rerank_model` / `rerank_top_k` (`hybrid.py:191-192`) with an
  independently-hardcoded default model string.

Fix: M (delete `cohere_rerank` outright; decide whether the cross-encoder hook survives 1.3; move
`evals/` out of the shipped package or behind an extra). Severity: major.

### M11. Structure is fetched and persisted but never scored

Three cases, each verified by grepping read sites:

1. **Entity type never enters ranking.** `rank_by_query_coverage` receives only `text`,
   `prior_score`, `original_rank`, `timestamp` (`QueryCoverageCandidate`,
   `query_ranking.py:709-716`). The candidate's `entity_type` is resolved at `search.py:2334` and
   `2621`, carried through `RetrievalCandidate`, used for _filtering_
   (`_candidate_matches_types`, 2727) and for facet assignment, and never for scoring. The only
   type-aware ranking in the whole subsystem is `_LINEAGE_TYPE_RANK` (`tools/context.py:700-719`),
   which runs at pack-assembly time for dedup, not retrieval.
2. **Relationship weights never reach the fused score.** `_graph_expansion_path_score`
   (`search.py:2181`) computes a score from the 23-entry `_GRAPH_EXPANSION_RELATIONSHIP_WEIGHTS`
   table times `_GRAPH_EXPANSION_DEPTH_DECAY`, stores it on the candidate, and `rrf_merge` then
   ignores the magnitude (`fusion.py:126`). The table only controls intra-lane ordering; any
   monotone relabeling of those 23 numbers produces an identical fused result.
3. **Vector cosine magnitude is discarded the same way.** A 0.95 match and a 0.31 match at the same
   rank in the vector lane contribute identically to fusion. `RetrievalPlan.vector_min_score`
   (`search.py:225`) is the only magnitude gate, and it is never set to anything but its `0.0`
   default (grep: the only writes are the four read sites at `search.py:1413,1446,1497,1526`).

Fix: L (this is a design question for 1.3, not a patch). Severity: major — it is the mechanism by
which "we have a knowledge graph" fails to become "the graph structure improves ranking".

### M12. RetrievalPlan fields frozen at their defaults

`/Users/bliss/dev/sibyl/packages/python/sibyl-core/src/sibyl_core/retrieval/search.py:202-227`

`build_context_retrieval_plan` (276-364) is the only plan constructor, and `context_search` mutates
the plan only via `replace(plan, candidate_limits=...)` (`search.py:385-388`). Fields never set by
any caller:

- `signals` (212) — always all 8 lanes; the per-lane disable path exists and is unused.
- `graph_expansion_depth` (224) — always 1, so the `max_depth` loop in `_node_bfs_records` always
  runs exactly one iteration and the BFS frontier logic at `search.py:1765-1770` is unreachable.
- `vector_min_score` (225) — always 0.0.
- `filter_selectivity_threshold` (227) — always `DEFAULT_FILTER_SELECTIVITY_THRESHOLD`.

Fix: S (either wire them or delete them). Severity: major — four knobs that read as tunable in every
code review and are not.

### M13. Operational passages never stamp `passage_covers_parent`, so parent

suppression is inert for half the corpus

`/Users/bliss/dev/sibyl/packages/python/sibyl-core/src/sibyl_core/projection/experience.py:490-506`
against `/Users/bliss/dev/sibyl/packages/python/sibyl-core/src/sibyl_core/tools/context.py:1294`

`_suppress_parents_of_passages` gates on `if not metadata.get(PASSAGE_COVERS_PARENT_KEY): continue`.
Repo-wide grep for that key finds writes only in the **prose** projection (`passages.py:258` sets it
`True`, `passages.py:289` sets it `False`). The operational projection's passage metadata block
never writes it at all.

Consequence: every operational passage fails the gate on line one, so the `raw_observation` parent
it was cut from is never suppressed even when the whole part is present in the pack. Both copies of
the same text spend the reader's char budget — the specific defect the slice substrate exists to
kill, and a plausible contributor to the "1/3-spent char budget" symptom.

The `/context` evidence-expansion path is unaffected: `_finest_granularity_units`
(`retrieval/operational_sources.py:358-372`) dedupes correctly on its own. The leak is confined to
search and context-pack assembly, which is the main read surface.

Fix: M (stamp the key in `experience.py`, plus a backfill). Severity: major.

### M14. Passages do not inherit `retrieval_keys`, so the exact-key lane can only

return the fat parent

`/Users/bliss/dev/sibyl/packages/python/sibyl-core/src/sibyl_core/projection/passages.py:718-731`
(`_inherited_scope_metadata`), copying the key list at `passages.py:85-93`

`_SCOPE_METADATA_KEYS` covers memory scope only. `retrieval_keys` is stamped on the parent
(`tools/add.py:493`) and promoted to the indexed column (`services/graph.py:3055-3057`). The
exact-key lane queries `retrieval_keys_normalized CONTAINSANY $probe_keys`
(`retrieval/search.py:1086`), and passages carry no value in that column, so a passage can never
match a writer-declared key.

Net effect: for the single highest-precision query class the system supports, retrieval returns the
whole fat memory and the passage substrate contributes nothing. This directly undercuts the 1.2
headline. Zero test coverage on either side: no `retrieval_keys` occurrence in
`tests/test_projection_passages.py`, no passage-shaped case in
`tests/test_retrieval_exact_key_arm.py`.

Fix: M. The interesting 1.3 design question is whether keys inherit wholesale or only onto the span
whose text contains them. Severity: major.

### M15. Two divergent passage metadata contracts under one entity type

`/Users/bliss/dev/sibyl/packages/python/sibyl-core/src/sibyl_core/tools/traverse.py:138-141` papers
over the split at the read site:

```python
def _passage_total(metadata):
    # The prose projection stamps passage_total, the operational one stamps passage_count
    return _int_metadata(metadata, "passage_total") or _int_metadata(metadata, "passage_count")
```

The full divergence, all on rows of the same `EntityType.PASSAGE`:

| concept         | prose (`projection/passages.py`) | operational (`projection/experience.py`) |
| --------------- | -------------------------------- | ---------------------------------------- |
| total           | `passage_total` (254)            | `passage_count` (500)                    |
| cut reason      | `passage_cut_reason` (256)       | `passage_reason` (502)                   |
| trail           | `passage_breadcrumb` (255)       | absent                                   |
| coverage        | `passage_covers_parent` (258)    | absent                                   |
| plan provenance | `passage_plan` (257)             | absent                                   |
| line offsets    | absent                           | `passage_line_start`/`_end` (503-504)    |
| id scheme       | `_generate_id` (117)             | `_stable_id` (104)                       |
| parent edge     | `PART_OF` (269)                  | `DERIVED_FROM` (511)                     |

Every read-side consumer must know both shapes or silently handle one (M13 is exactly the case where
a consumer handled one). No test pins the two schemas against each other.

Fix: L. Severity: major — this is the largest single item on the 1.3 rearchitecture list.

### M16. `_query_term_weights` zeroes every term the source actually contains

`/Users/bliss/dev/sibyl/packages/python/sibyl-core/src/sibyl_core/retrieval/operational_sources.py:516-525`

```python
weights = {term: len(group_terms) - sum(term in terms for terms in group_terms)
           for term in query_terms}
if any(weights.values()): return weights
return {term: 1 for term in query_terms}
```

With a single group (`len(group_terms) == 1`), a **present** term scores `1 - 1 = 0` and an
**absent** term scores `1 - 0 = 1`. A query mixing present and absent terms — the ordinary case —
makes `any(weights.values())` true on the strength of the absent terms, so the uniform fallback
never fires and every present term carries weight zero.

Downstream `_weighted_term_match` (`operational_sources.py:584-589`) returns 0 for every entity and
`_select_window_entities` (536-546) falls through entirely to the positional tie-break:
representative selection inside the window becomes query-blind. Absent terms also inflate the
`total_weight` denominator in `_window_coverage_score` (573), diluting coverage by terms that could
never match.

Fix: S (`len(group_terms) - df + 1`, or fall back to uniform when every _present_ term weighs 0).
Severity: major.

### M17. `restamp_entity_passages` does 64 sequential graph reads per scope-touching

update, including on memories with no passages

`/Users/bliss/dev/sibyl/packages/python/sibyl-core/src/sibyl_core/projection/passages.py:497-529`

`for index in range(MAX_PASSAGES_PER_SOURCE)` with an `await get(passage_id)` inside and no early
exit — deliberately, since the comment at 473-475 explains it must reach past an index gap.
Sixty-four sequential round trips.

The two callers disagree about when to pay that:

- Job path (`apps/api/src/sibyl/jobs/entities.py:1421-1432`) triggers on
  `scope_bearing_entity_update(updates)` (`passages.py:427-439`), which is **presence-based, not
  change-based**: any update mentioning one of seven keys, including `project_id` and `source_id`,
  fires it. A 200-char note with zero passages updated with a `project_id` in the patch pays 64
  sequential reads.
- Route path (`apps/api/src/sibyl/api/routes/entities.py:2444`) diffs
  `entity_scope_stamps(existing) != entity_scope_stamps(updated)` first.

Two cost profiles for the same function with no stated reason.

Fix: M (batch through `get_many`; make the job trigger diff-based like the route). Severity: major.

### M18. Ten passage/projection metadata keys are written on every row and read by nobody

Each verified by grep for non-test read sites; every hit below is the write itself.

| key                                      | write site                  | reads                |
| ---------------------------------------- | --------------------------- | -------------------- |
| `support_spans`                          | `experience.py:688,733,773` | 0                    |
| `passage_cut_reason`                     | `passages.py:256`           | 0                    |
| `passage_plan`                           | `passages.py:257`           | 0                    |
| `passage_cut_depth`                      | `experience.py:501`         | 0                    |
| `passage_reason`                         | `experience.py:502`         | 0                    |
| `passage_line_start` / `_end`            | `experience.py:503-504`     | 0                    |
| `ui_inventory_item_count` / `_truncated` | `experience.py:689-690`     | 0                    |
| `evidence_part_count`                    | `experience.py:633`         | 0                    |
| `procedure_part_count`                   | `experience.py:730`         | 0                    |
| `resolution_status: "unknown"`           | `experience.py:771`         | 0 (literal constant) |

`support_spans` is the fattest: a nested dict per observation carrying `image_refs` and
`evidence_part_ids` lists, on every transition, procedure, and failure row. I confirmed its three
writes and zero reads directly.

`passage_plan` is worth calling out separately because the constant block's own comment
(`passages.py:43-45`) justifies it as needed by "the replay job and any audit of retrievability" —
and `apps/api/src/sibyl/jobs/probes.py` never mentions it.

Why it matters beyond tidiness: row size feeds lexical document length, and `_passage_description`'s
own docstring (`experience.py:305-310`) names that as "the dilution mechanism Stage 1 named". These
keys are paying a ranking cost for zero retrieval value.

Fix: S each, M as a sweep. Severity: major in aggregate.

### M19. `plan_synthesis`'s whole source-selection pipeline is dead work

`/Users/bliss/dev/sibyl/packages/python/sibyl-core/src/sibyl_core/services/synthesis.py:1013-1091`

Plan runs four searches, up to 100 neighborhood expansions, and per-section scoring to build
`source_packs` and `source_ids`. Materialize then overwrites `source_packs` wholesale (642-672) and
overwrites each section's `source_ids` via `replace(section, source_ids=source_ids, ...)` (641). The
only surviving product of all that work is the presence of a `no_source_supports_requested_section`
gap — which materialize's own `no_materialized_sources` gap (628-635) already covers.

This is the largest single latency item in the synthesis slice: an expensive fan-out whose sole
output is a duplicate gap flag.

Fix: M, and it needs a decision rather than a patch — either delete plan's selection half, or make
materialize merge instead of replace. Severity: major, and it gates M20.

### M20. 116 sequential round trips on one `/synthesis/draft`

`/Users/bliss/dev/sibyl/packages/python/sibyl-core/src/sibyl_core/services/synthesis.py`

- `309-321` — 4 sequential `search_fn` calls.
- `331-339` — one `related_fn` call per entity id, capped at 100 (`MAX_EXPLICIT_NEIGHBORHOOD_IDS`,
  line 134), fully serial.
- `544-557` — one `context_fn` call per outline section, serial; up to 12 sections (line 345), 5 for
  a handbook.

Worst case 4 + 100 + 12 = 116 serialized round trips for a single draft. This sits directly behind
the measured clean-server latency miss.

Fix: S for the search and context loops (`asyncio.gather` with a semaphore); M for the neighborhood
loop if bounded concurrency is wanted. Severity: major.

### M21. Untrusted web content is interpolated into the distillation prompt unfenced

`/Users/bliss/dev/sibyl/packages/python/sibyl-core/src/sibyl_core/ai/operational_distillation.py:85-125`
feeding the template at `:54-65`

`build_operational_experience_digest` inlines `Goal:`, `Outcome:`, `Action:`, `Reasoning:`, and
accessibility-tree node text straight into `_PROMPT_TEMPLATE` under a bare `Trajectory digest:`
label. No delimiter, no escaping, no instruction-boundary marker. `_clean` (`:293`) only collapses
whitespace.

For a browsing agent that content is fully attacker-controlled, and the model's output lands in the
graph as an `EntityType.NOTE` titled "Observed environment facts" (`:224`) that future agents read
as ground truth. The `.format()` call at `:129` is safe (the digest is an argument, not the
template), so the exposure is instruction injection, not brace injection.

Fix: M — fence the digest and add an explicit data-not-instructions boundary. Worth a design
conversation: fencing alone does not fully close indirect injection from browsed pages. Severity:
major.

### M22. Batch extraction prompt permits in-batch source attribution spoofing

`/Users/bliss/dev/sibyl/packages/python/sibyl-core/src/sibyl_core/ai/memory_extraction.py:64-98`

`build_memory_batch_entity_extraction_prompt` emits `source_id: X` / `content:` blocks separated by
`\n\n---\n\n`, with raw unescaped content at line 84. A memory whose body contains its own
`source_id: <other>` line can be attributed to a different source in the same batch. The consuming
job's guard (`apps/api/src/sibyl/jobs/memory_extraction.py:275-284`) rejects only **unknown** source
ids, so a swap between two ids that are both legitimately in the batch passes clean.

Fix: S — escape or fence the content, or return per-block indices instead of echoing ids. Severity:
major.

### M23. Distilled operational notes are truncated mid-sentence with no marker

`/Users/bliss/dev/sibyl/packages/python/sibyl-core/src/sibyl_core/ai/operational_distillation.py:60-61`
vs `:20, 226`

The prompt authorizes up to 10 facts x 300 chars plus a 900-char workflow. Storage clips at
`MAX_OPERATIONAL_NOTE_CHARS = 1_600` with a bare slice:
`content=f"{header}\n\n{body}"[:MAX_OPERATIONAL_NOTE_CHARS]`. A full facts note runs header (~200) +
28 + ~3,020 ≈ 3,250 chars, so roughly half is dropped silently. The digest truncation at `:125` does
mark itself; this one does not. The two budgets contradict each other by a factor of two.

Fix: S. Severity: major.

### M24. Reflection lifecycle decisions silently no-op on any lookup error

`/Users/bliss/dev/sibyl/packages/python/sibyl-core/src/sibyl_core/tools/reflect.py:489-492`

Bare `except Exception`, log a warning, return `[]`. With an empty prior list,
`apply_reflection_lifecycle_decisions` finds no duplicates, no supersessions, no stale targets, and
no contradictions — so every candidate persists as clean and new. The `ReflectionPack` carries no
degradation marker, so the caller cannot tell.

Same shape as M7 (`compile_context`) and the same fix: a receipt.

Fix: S (stamp `lifecycle_check_degraded` into candidate metadata). Severity: major.

### M25. LLM telemetry records literal placeholders instead of provider and model

`/Users/bliss/dev/sibyl/packages/python/sibyl-core/src/sibyl_core/ai/llm/extractor.py:76-82, 88-94`
and
`/Users/bliss/dev/sibyl/packages/python/sibyl-core/src/sibyl_core/ai/llm/generator.py:49-55, 58-64, 83-89, 91-97`

Every metric records `provider="runtime"` and `model=self.model_override or "default"`. The
extractor has the true values two lines later — `_extraction_usage` (`:139-149`) pulls
`provider_name` and `model_name` off the responses. Per-model cost and latency breakdown is
impossible from these metrics, on the only paid path in the system.

Fix: S for the extractor. Severity: major.

### M26. Budget enforcement is a one-way estimate that is never reconciled

`/Users/bliss/dev/sibyl/packages/python/sibyl-core/src/sibyl_core/ai/llm/budget.py:78-100` and
`/Users/bliss/dev/sibyl/apps/api/src/sibyl/ai/llm/budget.py:37-101`

`reserve_llm_budget` charges `estimate_llm_tokens` = `len(text)//4 + (output_token_limit or 0)`.
`DBLLMBudgetEnforcer` increments the bucket and stops — there is no settle, commit, or refund
anywhere in the protocol. Two consequences:

- When `max_tokens is None` (the `Generator.generate` default, and any `Extractor` without an
  explicit cap) the output side is charged **zero**.
- When `max_tokens` is set, the full cap is charged even if the model returns 20 tokens.

`ExtractionUsage` carries the real counts and both jobs log them; nothing feeds them back. Drift is
unbounded and monotonic in both directions depending on call shape.

Fix: M (add a `settle` hook to the `LLMBudgetEnforcer` protocol). Severity: major.

### M27. `depth` and `constraints` are dead knobs exposed through the public API

`/Users/bliss/dev/sibyl/packages/python/sibyl-core/src/sibyl_core/models/synthesis.py:57,66`

Both are threaded through the CLI (`apps/cli/src/sibyl_cli/client.py:1899`), the MCP server
(`apps/api/src/sibyl/server.py:906,951,996`), the REST schema
(`apps/api/src/sibyl/api/schemas/synthesis.py:33`), and the tool wrappers
(`tools/synthesis.py:50,116,167,219`). The only read anywhere is the field-for-field copy inside
`plan_synthesis` at `synthesis.py:986` and `:995` — I verified this by grep; no scoring, rendering,
retrieval, or prompt code touches either.

Both also feed `_run_id` (`synthesis.py:442-445`, `repr(asdict(request))`), so flipping `depth=deep`
mints a fresh run id for byte-identical output — an advertised knob whose only observable effect is
a cache miss.

Fix: S (delete, or wire `depth` to `max_sections` and per-section limits). Severity: major — an
advertised knob that does nothing is worse than a missing one.

### M28. `synthesis_plan` reports gaps for sections that have sources

`/Users/bliss/dev/sibyl/packages/python/sibyl-core/src/sibyl_core/tools/synthesis.py:148-155` and
`/Users/bliss/dev/sibyl/apps/api/src/sibyl/api/routes/synthesis.py:156`

Both return the materialized run without calling `apply_synthesis_verification`.
`materialize_synthesis_section_packs:658-666` carries `run.verification.gaps` forward unfiltered,
and only `verify_synthesis_run:706-711` drops absence gaps for sourceful sections.
`synthesis_verify` and `synthesis_draft` call it (`tools/synthesis.py:206,267`); `synthesis_plan`
does not.

The related defect: stale plan-stage gaps are permanent in the outline. `synthesis.py:637-641`
builds `section_gaps = [*section.gaps, *materialization_gaps]` — plan's gaps are never re-evaluated,
and `verify_synthesis_run` only recomputes `run.verification`, never `outline.sections[*].gaps`. So
`synthesis_run_to_dict` always ships outline sections claiming
`no_source_supports_requested_section` beside a populated `source_ids` list.

Fix: S each. Severity: major (the plan endpoint lies about its own coverage).

### M29. Model snapshot pinning is unreachable, untested, and a no-op for half the registry

`/Users/bliss/dev/sibyl/packages/python/sibyl-core/src/sibyl_core/ai/providers.py:79-85`

`_pin_snapshots()` gates `resolve_provider_model_id`. Repo-wide grep for `SIBYL_LLM_PIN_SNAPSHOTS`
outside its own definition returns exactly one hit: `docs/_archive/SIBYL_LLM_SUBSTRATE_PLAN.md:308`,
which claims "CI uses this." It is set in no workflow, chart, compose file, or Ansible role, and no
test exercises it — so both branches of `resolve_provider_model_id:58-60` are unexercised.

Even when set it is a no-op for three of six models: `claude-sonnet-4-6` (`ai/registry.py:148-152`),
`gpt-5.4-mini` (`:207-211`), and `gpt-5.4-nano` (`:230-234`) all have
`snapshot == alias == provider_model_id`. Only `claude-haiku-4-5` and the two Gemini entries carry
real snapshots.

A reproducibility guarantee that does not hold is worse than none, especially for a project that
runs benchmark comparisons across weeks.

Fix: S to set the env in CI; M to add real snapshot ids. Severity: major.

---

## MINOR

### m0. Benchmark overfit in the production tokenizer, second instance

`/Users/bliss/dev/sibyl/packages/python/sibyl-core/src/sibyl_core/query_anchors.py:7-31, 42-43`

`_NORMALIZED_TOKEN_ALIASES` is a 25-entry hand-written stem table with unmistakably
LongMemEval-shaped vocabulary (`weddings`, `volunteered`, `subscribed`, `serviced`, `attended`), and
lines 42-43 hardcode a correction for one misspelling:

```python
if token == "buisiness":
    return "business"
```

This is the same dataset typo that appears in `query_ranking.py:617` (B1), now fixed in a second
place. `query_anchors` is live in production ranking — imported by
`retrieval/query_ranking.py:900,1092,1196`, `retrieval/hybrid.py:113`, and
`services/graph.py:166-207` — so a benchmark artifact shapes token normalization for every real user
query.

Fix: S to delete the typo line; M to replace the alias table with a real stemmer. Severity: minor on
its own, but it is B1's evidence that the overfit is not confined to one file.

### m1. Test-only exports presented as public API

`/Users/bliss/dev/sibyl/packages/python/sibyl-core/src/sibyl_core/retrieval/__init__.py`

Exported in `__all__`, zero production call sites (verified by repo-wide grep):

- `simple_hybrid_search` (`hybrid.py:918`, `__init__.py:118`) — referenced only by
  `tests/test_retrieval_advanced.py` and `tests/test_retrieval_benchmarks.py`.
- `weighted_score_merge` (`fusion.py:246`, `__init__.py:122`) — referenced only by
  `tests/test_retrieval.py` and `apps/api/tests/test_retrieval.py`.
- `FusionConfig` (`fusion.py:28`, `__init__.py:90`) — instantiated only in
  `tests/test_retrieval.py:412`.
- `_vector_candidate_sources` (`search.py:1244`) — a wrapper whose only callers are three sites in
  `tests/test_search.py`.

Fix: S each. Severity: minor.

### m2. `defaultdict(float)` accumulator that never accumulates

`/Users/bliss/dev/sibyl/packages/python/sibyl-core/src/sibyl_core/retrieval/search.py:2881,2889`

`score_by_id` is a `defaultdict(float)` but line 2889 **assigns** rather than adds, and does so once
per (lane, candidate) pair — so for a candidate found by four lanes the same value is written four
times. The `defaultdict` and the repetition are both vestigial; the shape suggests this was an
accumulating fusion before RRF moved into `rrf_merge`. Harmless today, actively misleading to read.

Fix: S. Severity: minor.

### m3. Object identity used as a dictionary key across a re-ranking boundary

`/Users/bliss/dev/sibyl/packages/python/sibyl-core/src/sibyl_core/retrieval/search.py:2999-3022` and
`/Users/bliss/dev/sibyl/packages/python/sibyl-core/src/sibyl_core/retrieval/query_ranking.py:868-880`

Both `_apply_query_coverage_to_fused` and `rank_items_by_query_coverage` key metadata by
`id(candidate)` and then look it up after the ranker has reordered the list. Correct today (the
source list holds a strong reference for the whole window, so ids are stable and unique), but it
silently breaks if the ranker ever copies, replaces, or re-wraps a candidate. The stable id is right
there on the object.

Fix: S. Severity: minor.

### m4. The coverage rerank result flag is discarded on the context path

`/Users/bliss/dev/sibyl/packages/python/sibyl-core/src/sibyl_core/retrieval/search.py:3011`

```python
reranked, _applied, _refined = rank_items_by_query_coverage(...)
```

`hybrid_search` surfaces both flags as `query_coverage_rerank_applied` and
`query_coverage_refinement_applied` in its metadata (`hybrid.py:881-882`). `context_search` throws
them away, so the context-pack receipt cannot say whether the coverage rerank fired or whether the
refinement pass was accepted. Given M2 (the pass always runs), this is the one receipt that would
let you measure whether the second pass earns its cost.

Fix: S. Severity: minor, high leverage for the M2 decision.

### m5. `SIBYL_MOCK_LLM` silently swaps in hash-based embeddings, with no production guard

`/Users/bliss/dev/sibyl/packages/python/sibyl-core/src/sibyl_core/embeddings/providers.py:606-617`

Read straight from `os.getenv`, not through `CoreConfig`, so the `environment == "production"`
validator in `config.py:220-232` (which does correctly forbid `memory://`) cannot see it. If the var
leaks into a production process, every graph embedding becomes a SHA-256 derived vector and semantic
retrieval degrades to noise while every health check stays green.

The same module reads `SIBYL_GRAPH_EMBEDDING_PROVIDER`, `SIBYL_GRAPH_EMBEDDING_MODEL`, and
`SIBYL_GRAPH_EMBEDDING_DIMENSIONS` directly (`providers.py:620,705,719`), each shadowing the
corresponding `CoreConfig` field. `SIBYL_FUSION_BACKEND` (`search.py:266`) and `COHERE_API_KEY`
(`reranking.py:469`) do the same. Six retrieval-relevant env vars bypass the settings object that
validates the rest.

Fix: S. Severity: minor (as a bug), major as a class if a production incident ever traces to it.

### m6. Embedding provider returns `None` on a missing key and logs at INFO

`/Users/bliss/dev/sibyl/packages/python/sibyl-core/src/sibyl_core/embeddings/providers.py:628-650`

`configured_embedding_provider()` returns `None` for a missing dependency or a missing API key,
logging `graph_embeddings_disabled` at INFO. Downstream, `_vector_candidate_sources_detailed`
handles `None` correctly and emits a receipt (`search.py:1276-1277` sets `attempted=False`), so this
one _is_ observable in `SearchResponse.filters` — the degradation is honest at the retrieval layer.
The debt is the log level: a whole retrieval modality going dark is not INFO, and the receipt does
not survive into `ContextPack` (see M7).

Fix: S. Severity: minor.

### m7. Duplicated helpers and redeclared constants across the projection modules

- **Scope-inheritance helper, verbatim twice**: `projection/memory.py:122-130` + `1192-1198` and
  `projection/passages.py:85-93` + `718-731`. Same seven-key tuple, same dict comprehension.
  `passages.py` additionally re-exports its copy as the public `SCOPE_BEARING_UPDATE_KEYS`, so the
  two can drift with nothing catching it.
- **`PASSAGE_COVERS_PARENT_KEY` redeclared** at `tools/context.py:180`, duplicating
  `projection/passages.py:52`. `tools/traverse.py:34` imports it properly, so the codebase does
  both. Confirmed by grep.
- **`PASSAGE_PROJECTION_KIND` redeclared** at `retrieval/operational_sources.py:17`, duplicating
  `passages.py:41`.
- **Trail-truncation block duplicated**: `passages.py:216-227` and `experience.py:457-470` are the
  same three-step algorithm against two constants that both equal `18_000`
  (`memory_pipeline/spans.py:46`, `experience.py:30`), with near-identical comment prose.

Fix: S each. Severity: minor.

### m8. `SliceStats` computed on every cut, discarded at every production call site

`/Users/bliss/dev/sibyl/packages/python/sibyl-core/src/sibyl_core/projection/slicing.py:81-89`,
incremented at 332, 353, 376, 390. Every non-test caller drops it: `passages.py:675`,
`passages.py:687` (`mechanical, _ = slice_prose(content)`), `experience.py:448`
(`slices, _ = slice_body(...)`). The only readers are `tests/test_projection_slicing.py` and the
frozen `benchmarks/longmemeval_v2_chunk_geometry/` harness — the counters existed to compare
`slicer.py` against `slicer_v2.py` during the killed geometry arm. `prepend_deferred` has exactly
one increment and zero readers anywhere in `packages/`.

Worth noting as a correction to the brief's premise: the geometry-reshaping arm is otherwise **not**
in production code. `slicing.py` is the surviving v2 cutter and is live on both paths; the abandoned
comparison harness lives entirely under `benchmarks/`. `SliceStats` is the only production residue.

Fix: S (keep it, but emit it on the structlog line so it stops floating). Severity: minor.

### m9. Legacy quality keys are dual-written and take precedence over the canonical ones

`/Users/bliss/dev/sibyl/packages/python/sibyl-core/src/sibyl_core/memory_pipeline/quality.py:32-67`

`expand_memory_quality_storage_metadata` writes two importance shadows and four confidence shadows
onto every entity, relationship, and raw capture (`services/graph.py:3327,3445`,
`services/surreal_content.py:1151`, `apps/api/src/sibyl/persistence/surreal/content.py:590`). No
application code reads them — grep finds only schema-migration `UPDATE` statements in
`backends/surreal/schema.py:443-489` and `content_schema.py:511-533`.

The sharper edge: `normalize_memory_quality_metadata` ranks the legacy names **above** the canonical
ones (`quality.py:36` puts `retention_importance` before `importance`; 41-45 put three legacy
confidence names before `confidence`). The behavior is pinned by
`tests/test_memory_pipeline_quality.py:8-24`: `{"importance": 0.8, "retention_importance": 0.3}`
normalizes to `0.3`. So a partial metadata patch that sets `importance` without clearing the shadows
is silently overridden by the stale value.

Fix: S to invert precedence, M to drop the shadows. Severity: minor as written, but the precedence
inversion is a live data-correctness trap.

### m10. Positional `relationships[:created]` assumes failures are suffix-aligned

`/Users/bliss/dev/sibyl/packages/python/sibyl-core/src/sibyl_core/projection/passages.py:660-661`
and `/Users/bliss/dev/sibyl/packages/python/sibyl-core/src/sibyl_core/projection/memory.py:543-545`

`create_bulk` returns a count; both sites slice the input list by it. If relationship 0 fails and 1
succeeds, the reported "created" set names the wrong row. Ten lines up in the same function,
`passages.py:648-658` gets this right by filtering on the returned id set — two contradictory
assumptions about the same manager API in one file.

Fix: S. Severity: minor.

### m11. Reprojection fires on unchanged content

`/Users/bliss/dev/sibyl/apps/api/src/sibyl/api/routes/entities.py:2424` and
`/Users/bliss/dev/sibyl/apps/api/src/sibyl/jobs/entities.py:1405`

Both guard on presence (`if update.content is not None`, `if "content" in updates`), never on
change. `_resolve_structure_metadata` already computes `content_changed` at
`routes/entities.py:243`, and the reprojection guard 2180 lines later does not use it. A client that
PUTs an entity back unchanged pays a full re-cut and re-embed of every passage.

Fix: S. Severity: minor (it is in `apps/api`, flagged here because the cost lands entirely in this
subsystem).

### m12. Per-query keyword extraction over the whole operational inventory, uncached

`/Users/bliss/dev/sibyl/packages/python/sibyl-core/src/sibyl_core/retrieval/operational_sources.py:486-513`

`_source_discriminative_terms` runs `extract_keywords` over every entity of every group on every
call to `select_operational_source_span`, then intersects group line sets. Nothing is memoized, so a
source with 200 passages re-tokenizes 200 bodies per query. The inventory itself is fetched
correctly in one `get_many` (line 179), so this is the remaining hot spot on that path.

Fix: M (cache term sets on the frozen inventory dataclass). Severity: minor.

### m13. Sequential probe replay across memories and orgs

`/Users/bliss/dev/sibyl/apps/api/src/sibyl/jobs/probes.py:137-165`, with
`DEFAULT_MAX_MEMORIES_PER_RUN = 200` (line 35) and up to `MAX_PROBES_PER_MEMORY = 5`
(`memory_pipeline/structure.py:39`) live searches per memory, each awaited singly inside
`rehearse_memory_probes` (`memory_pipeline/rehearsal.py:85-110`). Worst case 1000 sequential fused
searches per org, and `replay_memory_probes_all_orgs` (207-222) then loops orgs sequentially. The
passage lookup on the same path _is_ batched (`_PASSAGE_QUERY`, line 53), so the instinct is there;
the probe loop never got it.

Fix: M. Severity: minor (background job, not serving path).

### m14. Rehearsal receipts are recorded and nothing acts on them

`memory_pipeline/rehearsal.py` produces `REHEARSAL_STATUS_ABSENT` (line 185) and
`jobs/probes.py:190` counts `probes_retrievable`, but nothing reads a stored `probe_rehearsal` /
`probe_last_replay` receipt to alert, rank, re-project, or fail a gate. The only consumers are an
API response field (`apps/api/src/sibyl/api/schemas/entities.py:191`) and a CLI pretty-printer
(`apps/cli/src/sibyl_cli/main.py:615-659`).

The module docstring says probes exist "because this campaign was twice burned by features that
passed their unit tests and did nothing in production." The measurement is real; nothing closes the
loop on a failing one. Related: probes cannot be edited or withdrawn through the update route —
`apps/api/src/sibyl/api/routes/entities.py:261` calls
`build_memory_structure(content, spans=spans, atomic=atomic)` with no `probes` argument, so
`memory_probes` never enters `declaration_keys` (line 269) and MERGE semantics preserve whatever was
stored first.

Fix: S for the loop-closing counter, S for the probes argument. Severity: minor, but worth naming as
a 1.3 design gap: the system's own anti-inert-feature instrument has no consumer.

### m15. AI registry: most of the metadata surface has no reader

`/Users/bliss/dev/sibyl/packages/python/sibyl-core/src/sibyl_core/ai/registry.py`

Zero readers anywhere: `ModelCapability` (19-23) outside the export map, `deprecated_after` (42),
`cost_source_url` (40), `last_verified_at` (41), the `is_custom` property (45-47), and
`ModelRegistry.custom()` (97-115). `max_output_tokens`, `default_temperature`, and
`input_cost_per_mtok_usd` are read only by `scripts/llm/smoke.py`. `recommended_for` (91, 265) is
test-only.

Two sharper edges inside that: `_provider_output_type` (`ai/clients.py:100-103`) hardcodes
`config.provider == "gemini"` instead of checking the `STRUCTURED_OUTPUT` capability the registry
exists to express — so the capability model is bypassed at the one place it would matter. And
`last_verified_at` is pinned to 2026-05-15 (`:50`) with no staleness gate, which is three months
stale as of this audit.

The embedding half of the registry is scaffolding: all six `_DEFAULT_ENTRIES` are `ModelKind.LLM`,
so `embedding_entries()` (261) always returns `[]` and `recommended_for(kind=EMBEDDING)` always
raises. `tests/ai/test_registry.py:19` pins the emptiness. `ProviderName` (11) lists `cohere`,
`voyageai`, and `bedrock`, none of which `build_model` (`ai/providers.py:25-43`) can construct — its
`match` has no `case _`, so an unlisted provider falls through silently rather than raising.

Fix: S to prune, M to wire `STRUCTURED_OUTPUT`. Severity: minor.

### m16. Duplicated helpers across the synthesis and reflection services

- **Three copies of the same text-compaction helper**: `_compact_text`
  (`services/synthesis.py:200-207`), `_compact` (`services/reflection.py:881-888`) — byte-identical
  algorithm, different defaults — and `_clean` (`ai/operational_distillation.py:293`). A fourth,
  `_compact_text`, lives at `tools/context.py:826`.
- **Two functions named `_section_requests` with different jobs**: `services/synthesis.py:343-351`
  (template expansion) and `services/handbook.py:157-164` (outline reconstruction).
- **Two functions named `_tags_for` with divergent behavior**: `services/reflection.py:898-904`
  appends a `sensitive` tag, `tools/reflect.py:40-44` does not. Both run inside the same reflect
  call.
- **Cross-module private imports**: `services/handbook.py:26-28` imports `_query_for`,
  `_section_markdown`, and `_section_source_score` — three underscore-private symbols — from
  `services/synthesis`.

Fix: S each. Severity: minor.

### m17. Assorted correctness and hygiene items in synthesis/reflection

- **Class identity checked by string name**: `ai/validation.py:251`,
  `if error.__class__.__name__ == "LLMRateLimitError"`, two lines below a correct
  `isinstance(exc, ModelHTTPError)` at 247. A rename or subclass silently breaks rate-limit
  classification.
- **Non-deterministic ids in a module documented as deterministic**: `ClaimRecord.id` uses `uuid4`
  and `created_at` uses `_now_iso` (`models/reflection.py:121,131`), while
  `services/reflection.py:1` is titled "deterministic providers". Re-reflecting identical content
  mints new claim ids.
- **O(n·m) re-normalization per reflect**: `_duplicate_prior` (`services/reflection.py:729`) calls
  `_normalize_reflection_text` on prior content inside the inner loop. 25 candidates against 100
  priors (`tools/reflect.py:486`) is 2,500 full-body re-normalizations. `_prior_memory_snapshots`
  (682-700) already caches the hash; caching the word set is the same edit.
- **Misleading empty-section text after argmax reassignment**: `cite_each_source_once`
  (`services/handbook.py:117-154`) strips a source from every section but its best; the emptied pack
  then renders "_No citable sources were available for this section._" (`synthesis.py:824`). Sources
  were available; they went elsewhere. The section also stays out of "Not Covered"
  (`handbook.py:103`) because verification saw it as sourceful.
- **Handbook route over-reports sources**: `apps/api/src/sibyl/api/routes/synthesis.py:191-193`
  builds `source_ids` from the full `run.source_packs` while the markdown at 190 renders the deduped
  subset. The response claims citations the body does not contain.
- **Stale comment narrating a fixed bug**: `services/synthesis.py:299-301` says the hint families
  are types "none of which any bucket fetched" — the bucket at 306 fetches exactly those types. Both
  landed in commit `2de35c41`.
- **Obfuscated selection threshold**: `services/synthesis.py:411-415`, `if score > source.score`
  where `score = overlap + hint_bonus + explicit_bonus + source.score` — i.e. it means "any bonus at
  all". The `[:4]` cap and `min(3, len(sources))` fallback at 417 are unexplained, as are the `+4.0`
  hint bonus and `+1.5` explicit bonus at 383-387.
- **Dead enum members**: `SynthesisRunStatus.DRAFTING` and `.FAILED` (`models/synthesis.py:30-31`)
  have zero references — failures raise HTTP 500 instead. `ReflectionFindingKind.EXCEPTION`
  (`models/reflection.py:50`) has zero references.
- **Single-source memory extraction is production-dead**: `memory_entity_extractor`
  (`ai/memory_extraction.py:101`) and `build_memory_entity_extraction_prompt` (44) have no non-test
  callers — the job uses only the batch variants — yet both are still exported from
  `ai/__init__.py:39,50`.
- **`get_budget_enforcer`** (`ai/llm/budget.py:40`) has zero callers outside the export map.
- **Vestigial fan-out**: `apps/api/src/sibyl/jobs/memory_extraction.py:259` calls
  `extract_many([prompt], max_concurrent=max_concurrent)` on a one-element list, then unwraps
  `results[0]` — a semaphore and a gather over one item, left from the pre-batch per-source design.
- **`ReflectionExtractionRequest.source_ids` is production-dead**: `tools/reflect.py:104-111` never
  sets it, so `HeuristicReflectionExtractor.extract:210` always validates with
  `require_source_ids=False`, making the source-id checks in
  `validate_reflection_candidates:298-308` unreachable from that call site. Only
  `tests/test_reflection_extractor.py:49` sets it.
- **Silent config fallback**: `coerce_write_mode` (`services/memory.py:251-261`) maps any
  unrecognized value to `DISABLED`, so a typo in `SIBYL_NATIVE_WRITE` silently routes reflection
  persistence down the legacy `add_fn` path (`tools/reflect.py:177-207,310-323`) instead of the
  native one — two full persistence branches maintained in parallel.
- **Streaming telemetry can never fire**: `Generator.stream` (`ai/llm/generator.py:67-98`) records
  its success metric after the `async for` completes; an abandoned async generator records neither
  success nor error.
- **`_pin_snapshots` reads `os.environ` on every model resolution** (`ai/providers.py:80`) rather
  than once at import or startup.

Fix: S each. Severity: minor.

---

## Test gaps on load-bearing invariants

Beyond M8 (seven untested ranking stabilizers), the projection layer:

1. **The B4 retire hole.** `tests/test_projection_passages.py:608`
   (`test_reprojection_keeps_a_span_written_past_a_skipped_index`) covers a gap in the _current_
   run. Nothing covers a gap left by a _previous_ run, which is the case that strands a stale span.
   Nothing covers `retire_entity_passages` when index 0 was skipped —
   `test_deleting_a_memory_retires_every_span_cut_from_it` (line 449) uses four contiguous indices
   and `test_retiring_a_memory_that_never_had_spans_is_a_no_op` (line 464) uses zero.
2. **Cross-projection metadata contract (M15).** No test asserts that a prose passage and an
   operational passage present the fields their shared readers (`traverse.py:138`,
   `context.py:1294`) need. `tests/test_memory_spans.py` pins the _constants_ against their sources
   and pins nothing about the two metadata schemas.
3. **`passage_covers_parent` on operational passages (M13).** `tests/test_context_pack.py:2311-2593`
   builds passage fixtures by hand with prose-shaped metadata, so the operational shape's failure to
   suppress is invisible to the suite.
4. **Retrieval-key inheritance (M14).** Zero occurrences of `retrieval_keys` in
   `tests/test_projection_passages.py`; zero passage-shaped cases in
   `tests/test_retrieval_exact_key_arm.py`. Neither current nor intended behavior is pinned.
5. **`_query_term_weights` degenerate case (M16).** None of the 25 tests in
   `tests/test_operational_source_retrieval.py` exercises a single-group inventory with a query
   mixing present and absent terms.
6. **Quality round-trip (m9).** `tests/test_memory_pipeline_quality.py` asserts `expand` writes the
   shadows and `normalize` prefers them, but never that `normalize(expand(x)) == normalize(x)` — the
   invariant that stops the dual-write from corrupting a canonical value.
7. **Batching shape.** No test bounds the read count of `restamp_entity_passages`, so the
   64-round-trip behavior (M17) is free to persist.
8. **`MemoryStructure.declared`** (`memory_pipeline/structure.py:74-76`) is both unused (zero call
   sites across `packages/` and `apps/`) and untested.

And the LLM/synthesis layer:

9. **No test that materialize filters self-feeding sources (B5).** `tests/test_synthesis.py` has six
   `materialize_synthesis_section_packs` tests (369, 469, 537, 616, 729, 809) covering
   authorization, redaction, principal scoping, and corrections. None covers `capture_surface`; grep
   for `self_feeding` or `capture_surface` in that file returns only line 952, an assertion on what
   gets _written_.
10. **All 13 handbook tests are plan-only (B6).**
11. **`SIBYL_LLM_PIN_SNAPSHOTS` has zero test coverage (M29)**, so both branches of
    `resolve_provider_model_id:58-60` are unexercised.
12. **No `tests/ai/test_providers.py` and no `tests/ai/test_errors.py`.** `classify_llm_exception`
    is tested only at `tests/test_optional_dependencies.py:57-78`, which covers the `ImportError`
    fallback. The 429 → `LLMRateLimitError` branch (`ai/errors.py:103-110`), the timeout branch
    (88-95), and the `ModelRetry`/`UnexpectedModelBehavior` branch (119-126) are untested.
13. **No test for the reflection lifecycle swallow path (M24).** The eleven tests in
    `tests/test_reflect.py` cover extraction, dedup, scope policy, and both write modes; none
    injects a lookup failure.
14. **No test for the 1,600-char note truncation (M23).**
    `tests/ai/test_operational_distillation.py` (125 lines) never builds a payload large enough to
    trip `operational_distillation.py:226`.
15. **No test for `require_note_content` against the retry budget.**
    `DistilledOperationalNotes.require_note_content` (`ai/operational_distillation.py:77-82`) raises
    on an honestly-empty trajectory; with `output_retries=2` (`:147`) that burns three model calls
    and then fails the job. Untested, and arguably wrong behavior for a trajectory with nothing to
    distill.
16. **No test for batch source-id spoofing (M22).** `apps/api/tests/test_jobs_memory_extraction.py`
    covers the unknown-source-id rejection but not an in-batch swap.

---

## Suggested 1.3 ordering

Small and paired, do first: **B5 with B6** (the guard and the gate that should have caught it), then
**M28**, **M27**, and **M12** — all S, all visible in the public API surface.

Then the correctness bug: **B4**. It is S and it is currently serving deleted text.

Then the decisions that unblock everything else. **M19** (does plan's selection survive?) gates the
synthesis latency work in **M20**. **B2** (two pipelines) gates every ranking experiment. **B1**
(benchmark vocabulary in the ranker) gates any honest read of a benchmark number, and should be
sequenced as: measure with the tables removed, accept the drop as the true baseline, then rebuild.

Latency, roughly by ratio of win to effort: **M5** and **M4** (gather the retrieval phases and the
expansion lanes, drop the two dead lanes), **M2** (stop running the coverage ranker twice), **M20**
(gather the synthesis loops), **M17** (batch the 64-read restamp), **M6** (add the missing embedding
timeout).

**M21** deserves a design conversation rather than a patch — fencing alone does not close indirect
injection from browsed pages.

---

## One structural observation worth carrying into planning

There is **no LLM call anywhere in the synthesis or reflection path**. `services/synthesis.py`,
`services/handbook.py`, and `services/reflection.py` are entirely deterministic: search, lexical
scoring, template rendering. Only three surfaces call a model at all — `ai/memory_extraction.py`,
`ai/operational_distillation.py`, and `apps/api/src/sibyl/generator/llm.py`.

That is not itself debt, and the determinism buys real things (byte-stable runs, testability). But
several findings above (M25, M26, M29, m15) are LLM-shaped scaffolding built around code that never
calls a model, and the budget/telemetry/pinning machinery is being maintained for a surface much
smaller than its footprint suggests. Worth deciding deliberately in 1.3 whether synthesis stays
deterministic or the scaffolding gets pruned to match.

---

## What I did not audit

- `retrieval/dedup.py` (1030 lines), `retrieval/refinement.py` internals, and
  `services/graph_search.py` got call-graph treatment, not line-level review. One item I did see in
  `dedup.py` and did not pursue: `_find_hnsw_candidates_for_seeds` (`dedup.py:412-427`) falls back
  to a **sequential per-seed loop** when `execute_query_raw` is absent, where the batched path
  issues one multi-statement query. Grep found no non-test caller passing `execute_query_raw`, so
  which path production takes depends on whether the live client exposes that method — worth one
  check before trusting the batched path.
- `retrieval/identifier_query.py` was read only where the ranker touches it.
- The graph client, models, and persistence layer; `apps/web`. Out of lane by assignment. Where a
  finding's fix lands in `apps/api` (m11, M17's job path) I said so.
- No tests were executed and no benchmarks were run. Every claim here is static: read from source,
  or proved with grep whose command is stated. Nothing in this document rests on a run I did not do.
- Three findings originated with delegated readers on the projection and LLM slices. I independently
  re-read and confirmed the three load-bearing ones before including them: the retire-sweep control
  flow (B4, read `passages.py:576-608`), the materialize loop's missing filter (B5, read
  `synthesis.py:560-672`), and the `passage_covers_parent` write sites (M13, grep). The remainder
  carry their reporters' file:line and were not independently re-verified.

---
