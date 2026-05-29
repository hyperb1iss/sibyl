---
title: Retrieval System Architecture
description: How Sibyl's hybrid graph, vector, and full-text retrieval reaches LongMemEval ceiling
---

# Retrieval System Architecture

This is the deep architectural reference for Sibyl's retrieval system: the path a query takes from
HTTP request to ranked results, the data shape that path queries against, and the ranking primitives
that close the gap between hybrid search and answer-quality ceiling.

For the public claim, see [LongMemEval Results](../testing/longmemeval.md). For the user-facing
search guide, see [Semantic Search](../guide/semantic-search.md).

## System At A Glance

Sibyl's memory has three major surfaces, served from a single SurrealDB runtime:

1. **Graph memory.** Per-organization SurrealDB namespaces with entity records, relationship
   records, native vector fields, and full-text indexes. This is what `/api/search` queries.
2. **Content / raw memory.** Source-preserving records used for recall, context packs, crawled
   documents, and provenance. Connected to graph entities by reference.
3. **Runtime APIs.** REST and MCP endpoints for search, context packs, entity writes, memory writes,
   tasks, reflection, synthesis, and logs. Everything that talks to memory goes through here.

There is no Graphiti runtime, no external graph engine, and no separate vector database. SurrealDB
unifies graph traversal, vector KNN, full-text search, and document storage.

## The Isolation Model

Every organization gets its own SurrealDB namespace (`org_<uuid_hex>`). This is the native isolation
primitive — not a `WHERE org_id = ?` filter applied late, but a separate database namespace that the
query engine cannot route across.

```
SurrealDB
├── ns: org_a1b2c3d4...   ← tenant A: entities, relationships, raw memory
├── ns: org_e5f67890...   ← tenant B: entities, relationships, raw memory
└── ns: org_aabbccdd...   ← tenant C: ...
```

Practical consequences:

- **A misplaced filter cannot leak data across orgs.** The namespace boundary is a query-time
  routing decision, not a runtime predicate.
- **`GRAPH.DELETE org_<id>` is a clean deletion path.** GDPR right-to-be-forgotten is a single
  namespace drop, not a sweep across hundreds of tables.
- **Per-question LongMemEval haystacks fit naturally.** The eval harness signs up a throwaway user
  per question; their personal org becomes a throwaway namespace; teardown is automatic.
- **One driver instance per org.** The SurrealDB Python driver serializes websocket queries through
  a per-client `asyncio.Lock`. Sharing a single driver across orgs would serialize unrelated work;
  `driver.clone(group_id)` is the supported isolation primitive.

Every graph operation requires `group_id` (the org ID). There is no implicit "default org" path.
Forgetting `group_id` queries the wrong namespace or refuses the operation.

## The Write Path

Memory writes are split into a fast synchronous core and an async enrichment tail.

### Synchronous core

`POST /api/entities` (or `bulk=true`) creates entity records and, when an embedding provider is
configured, generates entity embeddings on write. The schema:

- `entity` records carry `name`, `content`, `entity_type`, scope metadata, source provenance, and a
  `name_embedding` vector field.
- `relates_to` records carry the relationship name and fact text, with a `fact_embedding` vector
  field.
- Oversized content is chunked into multiple entities sharing the same logical session ID. The
  policy is recorded as `entity_content_projection: api-entity-content-chunked-v1` so future policy
  changes are visible in artifacts.

Entity content has a 50,000-character cap. LongMemEval-S has exactly one oversized canonical
session; the harness projects it into multiple `session` entities with the same
`longmemeval_session_id` and dedupes on scoring.

### Async enrichment

After the sync write returns, the worker queues:

- **Memory projection** — deterministic propagation of new entities into raw memory records and
  derived relationship hints. Async; never blocks the writer.
- **LLM extraction** (optional, off by default) — entity and relationship extraction from longform
  content. Off for the full LongMemEval run on purpose; the retrieval baseline must not depend on
  LLM extraction.

The async lane has backpressure and concurrency limits. Writes never wait on extraction. The
previous synchronous-extraction path produced minute-plus inserts and was abandoned for being
UX-hostile and a hidden benchmark prerequisite.

## The Search Path

`POST /api/search` is the public retrieval surface. It runs the hybrid graph search in
[`sibyl_core/retrieval/hybrid.py`](https://github.com/hyperb1iss/sibyl/blob/main/packages/python/sibyl-core/src/sibyl_core/retrieval/hybrid.py),
which fuses candidate lists and then ranks them through the shared query-aware ranker in
[`sibyl_core/retrieval/query_ranking.py`](https://github.com/hyperb1iss/sibyl/blob/main/packages/python/sibyl-core/src/sibyl_core/retrieval/query_ranking.py).

Context-pack builds run the native retrieval plan in
[`sibyl_core/retrieval/search.py`](https://github.com/hyperb1iss/sibyl/blob/main/packages/python/sibyl-core/src/sibyl_core/retrieval/search.py)
(`context_search`). It still fetches candidates with DB-native KNN, full-text, and graph expansion,
and still applies its in-DB-shaped demotions and arithmetic boosts (vector-only demotion,
graph-expansion-only demotion, active-task / project-match / direct-source / freshness boosts) to
produce a base order. When the plan carries a query, that base order is then re-ranked through the
same query-coverage core the hybrid path uses, so both surfaces share one fusion+ranking primitive
(audit H6). When there is no query (pure context assembly), the coverage pass is skipped and the
base order stands. The shared invocation is `rank_items_by_query_coverage` in
[`query_ranking.py`](https://github.com/hyperb1iss/sibyl/blob/main/packages/python/sibyl-core/src/sibyl_core/retrieval/query_ranking.py),
called by both `hybrid_search` and `context_search`.

### Candidate sources

The native search plan models each candidate source as a `RetrievalSignal`. The current production
set:

| Signal             | Backend                           | What it captures                          |
| ------------------ | --------------------------------- | ----------------------------------------- |
| Raw lexical recall | Raw memory text store             | Exact-string matches in preserved sources |
| Node full-text     | SurrealDB full-text index         | Lexical handles on entity name/content    |
| Edge full-text     | SurrealDB full-text index         | Lexical handles on relationship facts     |
| Episode full-text  | SurrealDB full-text index         | Lexical handles on episodic records       |
| Node vector        | SurrealDB KNN on `name_embedding` | Semantic similarity to entities           |
| Edge vector        | SurrealDB KNN on `fact_embedding` | Semantic similarity to relationships      |
| Graph expansion    | SurrealDB graph traversal         | k-hop neighbors of high-scoring seeds     |

Vector search uses HNSW with `efc=150`, `m=12`, and query `ef=40`. Full-text uses the SurrealDB
native index. Graph expansion runs from the top seed candidates after the lexical and vector fan-out
completes.

### Fusion

Candidate lists from each signal merge via reciprocal-rank fusion. The fusion backend is
configurable: `python_rrf` (the default and what CI uses) and `surreal_rrf` (delegated to
SurrealDB's `search::rrf()`). Either backend produces a single ranked candidate list before the
ranking pass. The in-process implementation lives in a single module,
[`sibyl_core/retrieval/fusion.py`](https://github.com/hyperb1iss/sibyl/blob/main/packages/python/sibyl-core/src/sibyl_core/retrieval/fusion.py)
(`rrf_merge` / `rrf_merge_with_metadata`); both the native plan and the hybrid path route through
it, so there is one RRF scorer, not several.

### Ranking

After fusion, candidates pass through the query-coverage core. Both surfaces enter it through
`rank_items_by_query_coverage`, a thin shared wrapper that builds coverage candidates from each
item, calls `rank_by_query_coverage`, and runs one guarded refinement pass (accepted only when it
improves the top window without sacrificing guard coverage). This is interpretable ranking, not
LLM-based reranking. The ranker takes the fused list and reshuffles based on query coverage signals,
with a top-window stabilizer that protects strong base retrieval from being destroyed by weak
signals.

The next two sections describe the ranker in detail.

## Query-Aware Evidence Ranking

The shared ranker is the single primitive used by both runtime and replay code. The LongMemEval
harness does not have its own ranker; it scores the same code the production API runs. Anything that
improves benchmark scores improves user-facing search.

`rank_by_query_coverage` takes candidates and produces a ranked list. Each candidate accumulates
signals:

- **Prior rank and prior score** from fusion — the base order to beat
- **Keyword overlap** — query tokens present in the candidate text
- **Weighted IDF-style overlap** — rarer query tokens count more
- **Best segment overlap** — the strongest contiguous match within the candidate
- **Phrase adjacency** — bigram/trigram matches score above bag-of-words
- **Primary user-turn evidence** — direct user statements about themselves
- **Assistant-turn evidence** — claims and answers the assistant made
- **Personal-pronoun evidence** — "I", "my", "we" markers in user turns
- **Concept group overlap** — domain-aware category aliases (kitchen, hair, travel, etc.)
- **Memory-evidence patterns** — "I told you", "as I mentioned", "previously I said"
- **Preference evidence** — "I prefer", "I like", "my favorite"
- **Temporal target alignment** — candidate timestamp vs parsed query temporal target
- **Typed query-frame score** — see next section

The composite score is a weighted sum (the query-frame component dominates:
`_QUERY_FRAME_WEIGHT = 0.52`). The ranker does not blindly reorder the top window; it stabilizes
(see below).

## Typed Query Frames

Plain semantic similarity is not enough for many memory questions. A user asking "how many days
between events" needs both event dates, not five near-duplicates of the same lexical hit. A user
asking "who became a parent first, Tom or Alex" needs both candidates' parenthood evidence sessions
surfaced together. Typed query frames recognize these shapes and rank evidence accordingly.

The current frame families (all interpretable, all hand-crafted, none case-ID-specific):

- **Evidence-set questions**: "how many", "how much", "total number", "order of", "sequence of"
- **Temporal instruction questions**: "ago", "before", "after", "earliest", "latest", "first",
  "recently", "from earliest to latest"
- **Recommendation and preference questions**
- **Assistant evidence questions**: "you said", "you recommended", "remind me", "previous
  conversation"
- **Generated artifact questions**: created songs, recipes, code, outlines, with sub-recognizers for
  verse/chorus/ingredients/steps/functions/classes
- **Purchase and brand lookup questions**
- **Sibling/family count questions**
- **Age arithmetic questions**
- **Homegrown ingredient and recipe questions**
- **Phone accessory questions**
- **Sports event questions**
- **Business milestone questions**
- **Social media activity questions**
- **Recurring appointment questions**
- **Doctor visit questions**
- **Nostalgia/school questions**
- **Category aliases** for kitchen, hair, homegrown, media, travel, health, family, art events,
  delivery services, workshops, furniture actions, streaming subscriptions, and similar concepts

These are retrieval intents, not benchmark case IDs. The right framing: Sibyl recognizes common
memory-question shapes and ranks evidence accordingly, without paying an LLM on every query.

::: warning Honest positioning Typed query frames were developed against LongMemEval failure
patterns. That is acceptable because they generalize to the production memory-question taxonomy, but
it is not "intelligence that emerged from nowhere." The handcrafted nature is a real claim boundary.
Memory-R1, A-MEM, and similar systems learn these operations via RL; we have not gone there yet. :::

## Stabilization

The ranker does not blindly sort everything by the new score. A top-window stabilizer keeps strong
base retrieval intact while letting genuinely strong evidence outside the top window come up.

Stabilizers currently in play:

- `_stabilize_preference_ranking` — for preference queries, keep the top window stable when base
  retrieval is already strong
- `_stabilize_evidence_set_ranking` — for set-completion questions, prefer top windows that cover
  multiple distinct events
- `_stabilize_temporal_evidence_ranking` — for temporal questions, prefer candidates with timestamp
  alignment to a parsed temporal target
- `_stabilize_artifact_evidence_ranking` — for generated-artifact questions, rescue strong artifact
  candidates from outside the top window
- `_rank_preserving_window` — for temporal-instruction questions without a parsed target, preserve
  the existing rank in the top window

Each uses margin thresholds and minimum overlap requirements so the system does not overreact to a
single strong signal. Evidence-set replacement also has a score guard, so a deep low-score candidate
cannot enter the top window on lexical overlap alone. The latest published run reaches 500/500 hit@5
and 96.96% strict R@5; local replay of the current ranker against that artifact projects 97.35%
strict R@5 with zero hit regressions.

## Embeddings

| Setting               | Value                       |
| --------------------- | --------------------------- |
| Provider              | OpenAI (configurable)       |
| Model                 | `text-embedding-3-small`    |
| Dimensions            | 1024                        |
| Entity vector field   | `entity.name_embedding`     |
| Relation vector field | `relates_to.fact_embedding` |
| Entity KNN            | SurrealDB native            |
| Relation KNN          | SurrealDB native            |
| Query timeout         | 5s                          |
| Write timeout         | 20s                         |

Embeddings are provider-pluggable: OpenAI today, Gemini supported, local providers configurable. The
1024-dim choice is a deliberate trade-off — smaller than `text-embedding-3-small`'s default 1536 to
keep the HNSW index lean while staying well above the SOTA-comparable floor.

::: tip "No LLM" caveat The full LongMemEval run records "no LLM extraction or LLM reranking." That
does not mean "no external API calls." The embedding provider is OpenAI, and the embedding API is
called on every write and every query. A future local-embedding variant will run end-to-end without
external API dependencies for direct comparison against systems that publish local-embedding
numbers. :::

A small number of embedding/vector timeouts occur in the long full-run tail (5 relationship
embedding timeouts at 20s, 3 query embedding/vector search timeouts). The system returns fallback
results in these cases and the eval passes — vector timeouts do not produce 5xx responses or broken
queries.

## Diagnostics

Every run produces diagnostics alongside metrics. From the latest full run:

- Zero HTTP 500s.
- Zero tracebacks.
- 3,000 `surreal_query_failed` warnings — almost all expected relation-cleanup `NotFoundError`s on
  throwaway org teardown.
- 513 `surreal_query_slow` warnings (informational, no failure).
- 1 `graph_embedding_failed` (relationship embedding timeout at 20s).
- 3 `native_entity_vector_search_failed` (query embedding/vector search timeouts).
- SurrealDB stayed up for the run: `restartCount=0`, `oomKilled=false`, exit code `0`, and peak
  sampled memory `6.361GiB / 15.61GiB`.

The diagnostics surface is the same one a production operator uses: `sibyl debug status`,
`sibyl logs tail -l error`, `sibyl debug query`. Eval-time diagnostics are not a separate code path.

## What's Next

The hit-rate ceiling is essentially saturated on LongMemEval-S. The next quality layer targets
strict recall and ranking order:

1. **Query-aware set completion** — for "how many", "total", "order", "first vs second" questions,
   score the top window as a set rather than as independent candidates.
2. **Temporal pair coverage** — parse question dates and relative references; for comparison
   questions, ensure both compared events appear in the top window.
3. **Preference grounding** — distinguish "assistant recommended X generically" from "user said X is
   their preference"; promote user-evidenced preferences for recommendation queries.
4. **Async LLM extraction productionization** — keep retrieval baseline LLM-free; surface extraction
   as proper async enrichment with backpressure, retries, dead-letter visibility, and queue-depth
   metrics.
5. **SurrealDB native feature adoption** — evaluate `search::rrf()` end-to-end, graph recursion and
   shortest-path features where they simplify expansion, `EXPLAIN` for slow-query analysis.
6. **Beyond LongMemEval-S** — extend the eval ladder to LongMemEval-M and a future LongMemEval-V2,
   plus larger overlapping-topic and long-context tests.

The general principle: keep interpretable, debuggable ranking primitives and move LLM cost into
async enrichment, not into the synchronous query path.

## Related

- [LongMemEval Results](../testing/longmemeval.md) — the public eval claim
- [AI Memory Landscape](../testing/ai-memory-landscape.md) — comparative positioning
- [Benchmark Methodology](../testing/benchmark-methodology.md) — the full eval ladder
- [Semantic Search](../guide/semantic-search.md) — user-facing search guide
- [Why SurrealDB](../guide/why-surreal.md) — the unified runtime rationale
