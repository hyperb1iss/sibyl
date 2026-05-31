---
title: LongMemEval Results
description: Sibyl's live-API LongMemEval-S results, methodology, and claim boundary
---

# LongMemEval Results

Sibyl reaches the LongMemEval-S retrieval ceiling on the live API path with no LLM extraction and no
LLM reranking. This page records the exact result, how it was produced, and what we are and are not
claiming.

## Headline

::: tip Public claim sentence On **LongMemEval-S**, Sibyl's live API eval retrieves a correct answer
session in the top 5 for **500/500 questions** using SurrealDB-native graph and vector retrieval,
OpenAI embeddings, async projection, and no LLM extraction or reranking. Strict multi-answer recall
is **96.96% R@5** and **98.90% R@10**. :::

The headline metrics:

| Metric            | Value                |
| ----------------- | -------------------- |
| `hit@5`           | **100.00%**          |
| `recall@5`        | **96.96%**           |
| `ndcg@5`          | **94.63%**           |
| `hit@10`          | **100.00%**          |
| `recall@10`       | **98.90%**           |
| `ndcg@10`         | **95.48%**           |
| Questions         | 500/500              |
| Wall-clock        | 1,619.58s            |
| Runtime mode      | `live-api-ephemeral` |
| Memory extraction | disabled (0 jobs)    |
| LLM reranking     | none                 |

::: warning One sentence on `hit` vs `recall` `hit@5 = 100%` means every question has at least one
correct answer session in the top 5. `recall@5 = 96.96%` is the **strict multi-answer** metric: when
a question has several correct sessions, we measure the fraction we surface, not just whether we
surfaced any of them. Both numbers are real. They measure different things, and we report both. :::

## What This Result Is

This is a live API run. The eval driver does what any real client does:

1. Spins up an ephemeral CI stack: SurrealDB, the API daemon, and the worker.
2. Signs up a throwaway user and organization per question — every haystack lands in its own
   SurrealDB namespace, physically isolated from every other question.
3. Bulk-writes the question's haystack as `session` entities through the production
   `POST /api/entities` write path, with sync embedding generation.
4. Queues deterministic memory projection jobs in the background. Async; not waited.
5. Probes `/api/search` for readiness on the throwaway namespace.
6. Queries the production `/api/search` surface with the LongMemEval question.
7. Maps returned `session` entities back to LongMemEval session IDs by metadata.
8. Scores `hit@k`, strict `recall@k`, and `nDCG@k` against the answer key.
9. Uploads the per-case results and stack diagnostics as the run artifact.

The full eval drives the same code path a production client hits. There is no benchmark-only
shortcut, no offline notebook replay, and no special retrieval mode that bypasses production
features.

## What This Result Is Not

We are careful with the claim language because the LongMemEval landscape has historically been
overclaimed.

- **Not "100% recall."** `hit@5` is 100%; strict `recall@5` is 96.96%. Many LongMemEval-S questions
  have multiple correct answer sessions. A two-answer question scored 1/2 contributes 0.5 to strict
  recall but 1.0 to hit.
- **Not "zero API."** The retrieval path uses OpenAI's `text-embedding-3-small` (1024 dims). We do
  not use LLM extraction or LLM reranking, but we do call the embedding API.
- **Not "we beat everyone."** Sibyl is in the LongMemEval retrieval ceiling tier, comparable to the
  best public systems. See [AI Memory Landscape](./ai-memory-landscape.md) for honest comparison.
- **Not "downstream QA accuracy."** This is a retrieval metric (did we surface the right session),
  not an answer-quality metric (did the model answer the question correctly using the surfaced
  sessions). Many published memory benchmarks measure the latter; mixing the two compares unlike
  things.

## Per-Type Metrics

LongMemEval-S categorizes questions into six types. The strict recall and nDCG break down as:

| Type                        | Cases |     R@5 | NDCG@5 |    R@10 | NDCG@10 |
| --------------------------- | ----: | ------: | -----: | ------: | ------: |
| `single-session-user`       |    70 | 100.00% | 96.83% | 100.00% |  96.83% |
| `single-session-assistant`  |    56 | 100.00% | 99.34% | 100.00% |  99.34% |
| `single-session-preference` |    30 | 100.00% | 81.72% | 100.00% |  81.72% |
| `multi-session`             |   133 |  95.33% | 94.22% |  98.62% |  95.83% |
| `knowledge-update`          |    78 |  98.72% | 97.89% |  98.72% |  97.89% |
| `temporal-reasoning`        |   133 |  94.01% | 92.90% |  97.99% |  94.50% |

The remaining quality fight is concentrated in:

- **Temporal reasoning** (133 questions): both temporal-evidence sessions are usually in the
  candidate pool, but one sometimes falls outside the top 5.
- **Multi-session set completion** (133 questions): "how many", "order of", "first vs second" need
  multiple distinct events represented in the top window, not five near-duplicates.
- **Single-session preference ranking** (30 questions): the correct session is always present (R@5 =
  100%), but implicit preference evidence is semantically diffuse and the correct session often
  ranks 3rd or 4th instead of 1st (NDCG@5 = 81.72%).

These three patterns are the next quality target. See
[Retrieval System Architecture](../architecture/retrieval-system.md) for the ranking primitives we
already use and the set-completion work that is next.

## Latency

The full run took 1,619 seconds wall-clock across 500 questions, single-concurrency, on a
GitHub-hosted runner. End-to-end per question:

| Phase            |      Avg |      P50 |      P95 |       Max |
| ---------------- | -------: | -------: | -------: | --------: |
| Total (per case) | 2,944 ms | 2,770 ms | 3,913 ms | 10,198 ms |
| Ingest haystack  | 1,965 ms | 1,870 ms | 2,442 ms |  8,099 ms |
| Readiness probe  |   153 ms |   144 ms |   191 ms |  1,707 ms |
| Search           |   706 ms |   584 ms | 1,115 ms |  5,349 ms |

Ingest dominates because each question writes a fresh haystack into an isolated tenant; production
users do not pay that cost on every query. Search latency is what matters for serving: p50 584 ms,
p95 1,115 ms over the production code path with embeddings, fusion, graph expansion, and query-aware
ranking.

## Configuration

| Setting                   | Value                                      |
| ------------------------- | ------------------------------------------ |
| Dataset                   | `longmemeval_s_cleaned` (500 questions)    |
| Corpus hash (SHA-256)     | `d6f21ea9...c3a442`                        |
| Commit                    | `36032a25b2893f2fbcbc074bd0c212fb829dd975` |
| Retrieval mode (artifact) | `hybrid`                                   |
| Retrieval surface         | `POST /api/search`                         |
| Embedding provider        | OpenAI                                     |
| Embedding model           | `text-embedding-3-small`                   |
| Embedding dimensions      | 1024                                       |
| Graph HNSW                | `efc=150`, `m=12`, query `ef=40`           |
| Fusion backend            | `python_rrf`                               |
| Corpus text policy        | `user-and-assistant-turns-v1`              |
| Entity content projection | `api-entity-content-chunked-v1`            |
| Memory extraction         | disabled                                   |
| Memory projection         | async, 500 jobs queued, not waited         |
| Concurrency               | 1                                          |
| Created entity count      | 23,868                                     |

The full eval intentionally runs with `SIBYL_AUTO_EXTRACT_ENTITIES=false`. The workflow refuses to
let the full job run with extraction enabled — that flag is smoke-only. The reason: LLM extraction
is an async enrichment feature, not a hidden retrieval dependency. The full benchmark proves the
production retrieval baseline.

## Reproducibility

Everything lives in `.github/workflows/eval.yml`. The full job uses `workflow_dispatch` inputs that
are recorded in every artifact:

```yaml
retrieval_mode: native
longmemeval_concurrency: 1
longmemeval_corpus_text_policy: user-and-assistant-turns-v1
longmemeval_auto_extract_entities: false
longmemeval_wait_for_memory_extraction: false
longmemeval_wait_for_memory_projection: false
longmemeval_graph_hnsw_efc: 150
longmemeval_graph_hnsw_m: 12
longmemeval_graph_knn_ef: 40
longmemeval_native_fusion_backend: python_rrf
run_longmemeval_full: true
```

To inspect the published run from your shell:

```bash
# Inspect run metadata
gh run view 26304777971 --repo hyperb1iss/sibyl \
  --json status,conclusion,url,headSha,jobs

# Download the artifacts
mkdir -p /tmp/sibyl-eval-26304777971
gh run download 26304777971 --repo hyperb1iss/sibyl \
  --dir /tmp/sibyl-eval-26304777971

# Parse the overall + per-type metrics
jq '{completion_status,total_questions,completed_questions,elapsed_seconds,
     overall,per_type,metadata,runtime,dataset,sibyl_commit,repeat_count,k_values}' \
  /tmp/sibyl-eval-26304777971/longmemeval-live-full-*/longmemeval_live_full.json
```

To rerun the eval from a fork against your own ephemeral stack, fork the repo and dispatch the "Live
Runtime Eval" workflow with `run_longmemeval_full=true`. The job provisions its own SurrealDB,
backend, and worker, then tears them down at completion. Localhost mutation is refused unless the
caller passes `--allow-localhost` to the harness directly.

## Score Progression

These rows trace the live LongMemEval improvements that drove the latest result. Each is a real CI
run; the artifact paths live under `gh run view <id>`.

Only run `26304777971` is canonical for public claims. Earlier rows are historical progression
evidence and should not be quoted as the latest Sibyl score.

| Run         | Commit     |         H@5 |        R@5 |     NDCG@5 |        H@10 |       R@10 | Notes                     |
| ----------- | ---------- | ----------: | ---------: | ---------: | ----------: | ---------: | ------------------------- |
| 26137429505 | early      |      96.20% |     92.45% |     89.40% |      98.20% |     96.29% | early live quality gap    |
| 26256752834 | `3c29529d` |      98.80% |     94.85% |     93.04% |           — |          — | evidence ranking gains    |
| 26259548500 | `9dae3857` |      99.80% |     95.45% |     93.29% |     100.00% |     98.17% | query-frame result        |
| 26266367070 | `85e54410` |      99.80% |     96.09% |     93.74% |      99.80% |     98.49% | typed evidence frames     |
| 26273942749 | `972cf093` |     100.00% |     96.67% |     94.21% |     100.00% |     98.68% | artifact evidence ranking |
| 26304777971 | `36032a25` | **100.00%** | **96.96%** | **94.63%** | **100.00%** | **98.90%** | evidence-cluster polish   |

The latest live jump came from tightening evidence clusters without changing the production path:
typed frames, artifact evidence, and set completion all run inside the same query-aware ranker used
by `/api/search`.

## Replay Quality Gate

Before dispatching another full live run, ranker changes are replayed against the latest 500-case
artifact. Replay is not a public score replacement; it is a cheap guard that catches regressions
before spending another CI run.

The current improvement round expands personal action language and domain concept groups for
art-related events, food delivery, workshops, furniture actions, streaming subscriptions, and
recurring yoga/health routines. Forced replay against run `26304777971` produced:

| Metric      | Live baseline | Replay result |   Delta |
| ----------- | ------------: | ------------: | ------: |
| `hit@5`     |       100.00% |       100.00% | +0.00pp |
| `recall@5`  |        96.96% |        97.35% | +0.38pp |
| `ndcg@5`    |        94.63% |        94.77% | +0.14pp |
| `hit@10`    |       100.00% |       100.00% | +0.00pp |
| `recall@10` |        98.90% |        99.10% | +0.20pp |
| `ndcg@10`   |        95.48% |        95.55% | +0.06pp |

Replay improved 5 cases and regressed 0. The full live API job is still the authority for any public
headline update.

## Why The Eval Looks Like This

We made four deliberate methodology choices that some other published numbers do not match. They are
intentional:

1. **Live API path, not offline replay.** The harness uses real signup, real org creation, real
   entity API writes, real `/api/search` queries. An offline benchmark that skips these surfaces
   measures a different system than the one users get.
2. **Per-question physical tenant isolation.** Every question lives in its own SurrealDB namespace
   so retrieval cannot leak across the artificial haystack boundary. The artifact records
   `cross_question_result_count: 0`. This is stronger than metadata-scoped systems where one
   forgotten `WHERE` clause can break the boundary.
3. **No LLM extraction or LLM reranking.** Both are legitimate techniques and Sibyl supports async
   LLM extraction in production, but the retrieval baseline must not depend on either. Adding a
   reranker can lift scores; making it a retrieval prerequisite makes the system slow and expensive
   on every query.
4. **Strict recall, reported alongside hit.** We chose to publish strict multi-answer recall as the
   primary metric. The Codex review process that produced this harness pointed out that the original
   LongMemEval offline runner labeled `hit@k` as `recall@k`, which overstated quality. We keep both
   names and report both numbers.

## Caveats and Open Items

- The score is from LongMemEval-S (500 questions). LongMemEval-M and a future LongMemEval-V2 are not
  yet covered. The eval ladder is being extended.
- The full run uses OpenAI embeddings. We have not yet published a local-embedding variant. That is
  on the roadmap for direct comparison against systems that report local-embedding numbers (e.g.,
  Memweave).
- The retrieval ceiling on this benchmark is essentially saturated for `hit@5`. The remaining delta
  is strict recall and ranking order on multi-answer, temporal, and preference questions.
- The eval workflow is green on the published commit. Replay-only improvements stay labeled as
  projections until a full live API run confirms them.

## Related

- [AI Memory Landscape](./ai-memory-landscape.md) — honest competitive positioning
- [Retrieval System Architecture](../architecture/retrieval-system.md) — how the eval-passing path
  actually works
- [Benchmark Methodology](./benchmark-methodology.md) — the broader eval ladder, gates, and
  reporting rules
