# SurrealDB Graphiti Exit Benchmark Evidence

- Status: partial evidence, external-suite gap identified
- Last updated: 2026-05-13
- Related plan: `docs/architecture/SURREALDB_V07_GRAPHITI_EXIT_AND_PURE_SURREAL_PLAN.md`

## Answer

We do not yet have a saved apples-to-apples pre-Graphiti versus post-Graphiti benchmark pair.

We do have strong post-native context-pack evidence and earlier local compare-run artifacts, but
those are not a Graphiti baseline. Do not use them to claim native Surreal is faster or better than
Graphiti until the paired live runtime artifacts exist.

## Current Artifacts

### Passing Native/Compare Context-Pack Run

Artifact:

- `.moon/cache/evals/eval_context_pack_retrieval_compare_20260512_205740.json`

Summary:

- `search_type`: `context-pack`
- `retrieval_mode`: `compare`
- `repeat_count`: `20`
- `cases`: `160`
- `passed`: `160`
- `failed`: `0`
- `pass_rate`: `1.0`
- `latency_ms`: `105.3`
- `latency_p95_ms`: `400.2`
- `source_metadata_coverage`: `1.0`
- `facet_order_match_rate`: `1.0`
- `leak_count`: `0.0`

This supports the v0.7 default-loop quality gate for the current native/compare retrieval path.

### Earlier Failed Local Compare Run

Artifact:

- `.moon/cache/evals/eval_context_pack_retrieval_compare_20260512_204628.json`

Summary:

- `search_type`: `context-pack`
- `cases`: `8`
- `passed`: `1`
- `failed`: `7`
- `pass_rate`: `0.125`
- `latency_ms`: `622.7`
- `latency_p95_ms`: `1187.0`

This is useful debugging history. It is not a pre-Graphiti or legacy Graphiti baseline.

### Offline LongMemEval-Style Artifacts

Artifacts:

- `benchmarks/results_sibyl_raw.json`
- `benchmarks/results_sibyl_hybrid.json`

These are offline component artifacts. They do not measure the live API path, default app startup,
Graphiti construction, or post-native Surreal runtime behavior.

#### Full Overall Results

| Mode   | Questions | Elapsed | Recall@5 | NDCG@5 | Recall@10 | NDCG@10 |
| ------ | --------- | ------- | -------- | ------ | --------- | ------- |
| raw    | 500       | 312.3s  | 0.966    | 0.888  | 0.982     | 0.889   |
| hybrid | 500       | 270.8s  | 0.980    | 0.934  | 0.992     | 0.935   |

#### Full Results By Question Type

| Mode   | Question Type             | Recall@5 | NDCG@5 | Recall@10 | NDCG@10 |
| ------ | ------------------------- | -------- | ------ | --------- | ------- |
| raw    | single-session-user       | 0.914    | 0.828  | 0.971     | 0.847   |
| raw    | multi-session             | 0.992    | 0.919  | 1.000     | 0.914   |
| raw    | single-session-preference | 0.967    | 0.833  | 0.967     | 0.833   |
| raw    | temporal-reasoning        | 0.947    | 0.839  | 0.970     | 0.839   |
| raw    | knowledge-update          | 1.000    | 0.946  | 1.000     | 0.944   |
| raw    | single-session-assistant  | 0.964    | 0.953  | 0.964     | 0.953   |
| hybrid | single-session-user       | 0.986    | 0.957  | 1.000     | 0.962   |
| hybrid | multi-session             | 0.985    | 0.947  | 1.000     | 0.945   |
| hybrid | single-session-preference | 0.900    | 0.786  | 0.967     | 0.807   |
| hybrid | temporal-reasoning        | 0.985    | 0.908  | 0.992     | 0.904   |
| hybrid | knowledge-update          | 1.000    | 0.980  | 1.000     | 0.980   |
| hybrid | single-session-assistant  | 0.964    | 0.954  | 0.964     | 0.954   |

The hybrid offline mode improves overall Recall@5, NDCG@5, Recall@10, NDCG@10, and elapsed time
relative to raw mode. The one regression in this saved pair is the single-session-preference slice,
where raw Recall@5 and NDCG remain higher.

### Retrieval Component And Mini-Memory Gate

Command:

- `moon run core:bench-retrieval`

Receipt:

- `25 passed in 0.42s`

Coverage:

- Temporal boost correctness and throughput:
  - recent entities rank higher when relevance is equal
  - aggressive and gentle decay reorder results differently
  - high relevance can beat recency
  - entities without timestamps keep their original score
  - minimum boost prevents zeroing old records
  - per-type decay rates differ
  - `1,000` entities stay under the `500 ms` test budget
  - `10,000` entities stay under the `2,000 ms` test budget
- Reciprocal Rank Fusion correctness and throughput:
  - top shared results rank highest
  - list weights influence final ranking
  - disjoint lists merge successfully
  - `3 x 100` result fusion stays under the `100 ms` test budget
  - `3 x 1,000` result fusion stays under the `500 ms` test budget
- Mini-LongMemEval-style retrieval:
  - factual recall
  - recent temporal recall
  - entity-type filtering
  - aggressive versus gentle temporal reshuffling
  - `Recall@5 >= 0.60` across known queries
- Reranking and hybrid-search wiring:
  - reranking defaults and disabled passthrough
  - graceful fallback when cross-encoder dependencies are unavailable
  - content extraction
  - hybrid config wiring
  - temporal and non-temporal hybrid ordering

This is a component correctness and budget gate. It does not write a saved JSON benchmark artifact.

## AI Memory Benchmark Coverage

The release evidence bundle should include full results for every AI memory benchmark or competitor
comparison we cite. A one-line headline is not enough; each suite needs the raw result artifact,
overall metrics, per-slice metrics, corpus or dataset version, command, commit, runtime mode, and
known caveats.

Current coverage:

| Suite or comparison             | Local artifact status                                | Current use                                |
| ------------------------------- | ---------------------------------------------------- | ------------------------------------------ |
| Context-pack frozen suite       | Full current report in `.moon/cache/evals/`          | v0.7 default-loop quality gate             |
| LongMemEval-style offline suite | Full raw and hybrid JSON results in `benchmarks/`    | Internal offline retrieval baseline        |
| Live Graphiti vs native Surreal | Missing paired `bench-live` artifacts                | Required before public pre/post claims     |
| LOCOMO-style long-memory suite  | No harness or result artifact found in this checkout | Future external-suite positioning evidence |
| RULER-style long-context suite  | No harness or result artifact found in this checkout | Future long-context stress evidence        |
| Mem0 comparison                 | No committed result artifact found in this checkout  | Future competitor comparison, if cited     |
| Zep comparison                  | No committed result artifact found in this checkout  | Future competitor comparison, if cited     |
| LangMem comparison              | No committed result artifact found in this checkout  | Future competitor comparison, if cited     |

Until a suite has a raw artifact and full per-slice table, cite it only as planned coverage, not as
evidence. If we decide to make benchmark claims in release notes, the missing rows above become
release-blocking for that claim.

### Missing Live Runtime Artifacts

This checkout currently has no saved `benchmarks/results/*.json` live-runtime artifacts. That means
there is no canonical `bench-live` legacy-vs-native pair to cite.

No committed LOCOMO, Mem0, Zep, LangMem, RULER, or other external AI-memory benchmark result files
were found in this checkout. If we add those suites later, their raw result files should live next
to the LongMemEval-style outputs and be summarized here with the same artifact-first rule.

## Claim Boundaries

Supported claims:

- Native/compare context-pack evaluation passes the frozen 20-run suite with `160/160` cases and
  `400.2 ms` p95 latency.
- The default memory loop and selected entrypoints can run with `graphiti_core` imports blocked when
  `moon run core:no-graphiti-smoke` is green.

Unsupported claims until the missing pair exists:

- Native Surreal is faster than Graphiti.
- Native Surreal has better retrieval quality than Graphiti.
- The failed `20260512_204628` context-pack artifact represents pre-Graphiti performance.
- Offline LongMemEval-style files represent production runtime behavior.

## Required Pre/Post Pair

To make a public pre/post Graphiti claim, capture two `bench-live` artifacts against the same
corpus, queries, auth scope, and release candidate.

Legacy Graphiti runtime:

```bash
moon run bench-live -- \
  --label graphiti-legacy \
  --metadata store=legacy \
  --metadata graph_engine=graphiti \
  --metadata corpus=<corpus-name>
```

Native Surreal runtime:

```bash
moon run bench-live -- \
  --label surreal-native \
  --metadata store=surreal \
  --metadata graph_engine=native_surreal \
  --metadata corpus=<corpus-name>
```

Gate and compare:

```bash
moon run bench-gate -- benchmarks/results/<legacy-artifact>.json \
  --profile acceptance \
  --require-metadata graph_engine=graphiti

moon run bench-gate -- benchmarks/results/<surreal-artifact>.json \
  --profile acceptance \
  --require-metadata graph_engine=native_surreal

uv run python benchmarks/compare_eval_reports.py \
  benchmarks/results/<legacy-artifact>.json \
  benchmarks/results/<surreal-artifact>.json
```

Keep the raw artifact paths in release notes or PR notes whenever citing any number.

## Release Rule

v0.7 can ship on default-loop no-Graphiti proof plus the required context-pack gate. Any release
note, README, or announcement that compares native Surreal against Graphiti must wait for the paired
`bench-live` artifacts above.
