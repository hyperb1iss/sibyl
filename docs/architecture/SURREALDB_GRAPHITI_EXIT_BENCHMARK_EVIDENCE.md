# SurrealDB Graphiti Exit Benchmark Evidence

- Status: partial evidence, external-suite full-result ledger required
- Last updated: 2026-05-13
- Related plan: `docs/architecture/SURREALDB_V07_GRAPHITI_EXIT_AND_PURE_SURREAL_PLAN.md`

## Answer

We do not yet have a saved apples-to-apples pre-Graphiti versus post-Graphiti benchmark pair.

We do have strong post-native context-pack evidence and earlier local compare-run artifacts, but
those are not a Graphiti baseline. Do not use them to claim native Surreal is faster or better than
Graphiti until the paired live runtime artifacts exist.

## Current Artifacts

### Passing Native/Compare Context-Pack Run

Latest artifact:

- `.moon/cache/evals/eval_context_pack_retrieval_compare_20260512_225828.json`

Summary:

- `search_type`: `context-pack`
- `retrieval_mode`: `compare`
- `repeat_count`: `20`
- `cases`: `160`
- `passed`: `160`
- `failed`: `0`
- `pass_rate`: `1.0`
- `latency_ms`: `16.1`
- `latency_p95_ms`: `23.7`
- `source_metadata_coverage`: `1.0`
- `facet_order_match_rate`: `1.0`
- `leak_count`: `0.0`

This supports the v0.7 default-loop quality gate for the current native/compare retrieval path.

The earlier passing artifact,
`.moon/cache/evals/eval_context_pack_retrieval_compare_20260512_205740.json`, also passed `160/160`
with `400.2 ms` p95 latency. The `20260512_225828` artifact is the latest release-gate receipt.

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

### Offline LongMemEval-Style Aggregate Artifacts

Artifacts:

- `benchmarks/results_sibyl_raw.json`
- `benchmarks/results_sibyl_hybrid.json`

These are historical offline component artifacts generated before the benchmark runner wrote
per-question records. They contain aggregate metrics only. They do not measure the live API path,
default app startup, Graphiti construction, or post-native Surreal runtime behavior.

They are useful internal baseline data, but they are not full AI-memory result records. Re-run
`benchmarks/longmemeval_bench.py` with the current runner before citing LongMemEval-style results as
external benchmark evidence; new artifacts must include `case_results` with answer IDs and ranked
session IDs for every question.

#### Aggregate Overall Results

| Mode   | Questions | Elapsed | Recall@5 | NDCG@5 | Recall@10 | NDCG@10 |
| ------ | --------- | ------- | -------- | ------ | --------- | ------- |
| raw    | 500       | 312.3s  | 0.966    | 0.888  | 0.982     | 0.889   |
| hybrid | 500       | 270.8s  | 0.980    | 0.934  | 0.992     | 0.935   |

#### Aggregate Results By Question Type

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

Every public benchmark claim must be traceable to a committed or archived artifact that a future
release audit can re-open without guessing the corpus, mode, or code revision. If a suite has no
artifact, it belongs in planned coverage only.

Current coverage:

| Suite or comparison             | Local artifact status                                                       | Current use                                |
| ------------------------------- | --------------------------------------------------------------------------- | ------------------------------------------ |
| Context-pack frozen suite       | Full current report in `.moon/cache/evals/`                                 | v0.7 default-loop quality gate             |
| LongMemEval-style offline suite | Aggregate raw and hybrid JSON results in `benchmarks/`; full rerun required | Internal offline retrieval baseline        |
| Live Graphiti vs native Surreal | Missing paired `bench-live` artifacts                                       | Required before public pre/post claims     |
| LOCOMO-style long-memory suite  | No harness or result artifact found in this checkout                        | Future external-suite positioning evidence |
| RULER-style long-context suite  | No harness or result artifact found in this checkout                        | Future long-context stress evidence        |
| Mem0 comparison                 | No committed result artifact found in this checkout                         | Future competitor comparison, if cited     |
| Zep comparison                  | No committed result artifact found in this checkout                         | Future competitor comparison, if cited     |
| LangMem comparison              | No committed result artifact found in this checkout                         | Future competitor comparison, if cited     |

Until a suite has a raw artifact and full per-slice table, cite it only as planned coverage, not as
evidence. If we decide to make benchmark claims in release notes, the missing rows above become
release-blocking for that claim.

### Full AI Memory Result Ledger

The table below is the release ledger for external AI memory benchmarks. Rows can move from
`planned` to `citable` only after the raw artifact and the summarized table are both present in this
document.

| Suite or comparison             | Required artifact pattern                                        | Current status | Required summary before citation                                          |
| ------------------------------- | ---------------------------------------------------------------- | -------------- | ------------------------------------------------------------------------- |
| LongMemEval-style offline suite | `benchmarks/results_sibyl_<mode>.json`                           | aggregate-only | Overall, per-question-type metrics, and `case_results` for every question |
| LOCOMO-style long-memory suite  | `benchmarks/results/ai-memory/locomo_<engine>_<timestamp>.json`  | planned        | Overall score, per-category score, abstention/error rate, latency         |
| RULER-style long-context suite  | `benchmarks/results/ai-memory/ruler_<engine>_<timestamp>.json`   | planned        | Task-level score, context length, failure modes, latency                  |
| Mem0 comparison                 | `benchmarks/results/ai-memory/mem0_<engine>_<timestamp>.json`    | planned        | Overall quality, per-slice quality, ingestion latency, query latency      |
| Zep comparison                  | `benchmarks/results/ai-memory/zep_<engine>_<timestamp>.json`     | planned        | Overall quality, per-slice quality, ingestion latency, query latency      |
| LangMem comparison              | `benchmarks/results/ai-memory/langmem_<engine>_<timestamp>.json` | planned        | Overall quality, per-slice quality, ingestion latency, query latency      |

For citable rows, include both native Surreal and comparator artifacts when the claim is
comparative. For non-comparative rows, include the native Surreal artifact and label the claim as
Sibyl-only. Every artifact must include enough metadata to reproduce the run without relying on
local memory or chat history.

### External Benchmark Acceptance Checklist

Before any external AI memory result appears in release notes, README copy, launch notes, or a
comparison table:

- store the raw artifact under `benchmarks/results/ai-memory/` or another committed/archive path
  named in this document
- record the exact suite source, dataset split, version or commit, and any preprocessing
- record the Sibyl commit, runtime mode, graph engine, store, embedding model, generation model if
  used, and tokenizer or context budget
- include overall metrics and the complete per-slice table, even when a slice regresses
- include ingestion time, query latency, timeout count, error count, and skipped-case count when the
  suite exposes them
- keep prompts, evaluators, and scoring scripts versioned or archived with the result
- add a claim boundary explaining what the result does and does not prove

This is deliberately stricter than a normal smoke benchmark. External memory benchmarks are
positioning evidence, so they need the whole receipt.

### Full Result Record Required Fields

Each cited suite needs a result record with:

- raw artifact path
- benchmark suite name and version
- corpus or dataset name and version
- Sibyl commit
- command
- runtime mode, including graph engine and store
- model and embedding configuration when the suite uses generated content or embeddings
- overall metrics
- per-slice metrics
- per-case result records with answer IDs, ranked result IDs, and case metrics
- elapsed time and latency metrics when available
- pass/fail gate and profile
- known caveats

For competitor comparisons, the record also needs competitor version, hosted versus self-hosted
mode, data-ingestion path, and any tuning that changes retrieval quality or latency.

### Release Notes Rule

Release notes can cite only the rows above that have full artifacts. For v0.7 today, that means the
context-pack frozen suite is citable. The offline LongMemEval-style raw/hybrid files can be
described only as historical aggregate internal baselines until re-run with full `case_results`.
LOCOMO, RULER, Mem0, Zep, LangMem, and live Graphiti-vs-native claims stay out of release notes
until their artifact rows exist.

### Missing Live Runtime Artifacts

This checkout currently has no saved `benchmarks/results/*.json` live-runtime artifacts. That means
there is no canonical `bench-live` legacy-vs-native pair to cite.

No committed full-result LOCOMO, Mem0, Zep, LangMem, RULER, or other external AI-memory benchmark
result files were found in this checkout. If we add those suites later, their raw result files
should live next to the LongMemEval-style outputs and be summarized here with the same
artifact-first rule.

## Claim Boundaries

Supported claims:

- Native/compare context-pack evaluation passes the frozen 20-run suite with `160/160` cases and
  `23.7 ms` p95 latency.
- The default memory loop and selected entrypoints can run with `graphiti_core` imports blocked when
  `moon run core:no-graphiti-smoke` is green.
- Historical LongMemEval-style aggregate files show offline raw and hybrid summary metrics, but not
  full per-question evidence.

Unsupported claims until the missing pair exists:

- Native Surreal is faster than Graphiti.
- Native Surreal has better retrieval quality than Graphiti.
- The failed `20260512_204628` context-pack artifact represents pre-Graphiti performance.
- Offline LongMemEval-style files represent production runtime behavior.
- Offline LongMemEval-style aggregate files are full external AI-memory benchmark receipts.

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
