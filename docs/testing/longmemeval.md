---
title: LongMemEval-S
description: How Sibyl runs LongMemEval-S on the live API path, and what a result can claim
---

# LongMemEval-S

Sibyl runs LongMemEval-S retrieval against the live API path, with no LLM extraction and no LLM
reranking. This page records how the eval works, what its metrics mean, and the boundary on what a
result is allowed to claim.

## Current Status

::: warning No citable retrieval number

Sibyl does not currently publish a LongMemEval-S retrieval number. The earlier headline run predates
1.0 and has been withdrawn as a public claim. A new number appears here only after a live full run
on a current release passes `moon run bench-gate` and `benchmarks/results/ai-memory/manifest.json`
lists its artifact as citable.

:::

The same rule covers every surface: the README, these docs, release notes, and the project site cite
a LongMemEval-S number only when the manifest does.

## How The Eval Runs

This is a live API run. The eval driver does what any real client does:

1. Spins up an ephemeral CI stack: SurrealDB, the API daemon, and the worker.
2. Signs up a throwaway user and organization per question, so every haystack lands in its own
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

The full eval intentionally runs with `SIBYL_AUTO_EXTRACT_ENTITIES=false`. The workflow refuses to
let the full job run with extraction enabled; that flag is smoke-only. LLM extraction is an async
enrichment feature, not a hidden retrieval dependency, so the full benchmark measures the production
retrieval baseline without it.

## What The Metrics Mean

- **`hit@k`** means at least one correct answer session appears in the top `k`.
- **Strict `recall@k`** is the multi-answer metric. When a question has several correct sessions, it
  measures the fraction surfaced, not just whether any of them appeared. A two-answer question
  scored 1/2 contributes 0.5 to strict recall but 1.0 to hit.
- **`nDCG@k`** rewards ranking the correct sessions higher inside the top `k`.

Many LongMemEval-S questions have multiple correct sessions, so `hit@k` and strict `recall@k`
measure different things. Any published Sibyl result reports both.

## Claim Boundary

We are careful with the claim language because the LongMemEval landscape has historically been
overclaimed. A LongMemEval-S result from this harness is:

- **Not "100% recall."** A perfect `hit@5` says nothing about strict `recall@5`.
- **Not "zero API."** The retrieval path uses OpenAI's `text-embedding-3-small` (1024 dims). It uses
  no LLM extraction or LLM reranking, but it does call the embedding API.
- **Not "we beat everyone."** See [AI Memory Landscape](./ai-memory-landscape.md) for how
  comparisons across the field go wrong.
- **Not "downstream QA accuracy."** This is a retrieval metric (did we surface the right session),
  not an answer-quality metric (did the model answer the question correctly using the surfaced
  sessions). Many published memory benchmarks measure the latter; mixing the two compares unlike
  things.

## Reproducibility

Everything lives in `.github/workflows/eval.yml`. The full job uses `workflow_dispatch` inputs that
are recorded in every artifact:

```yaml
longmemeval_concurrency: 1
longmemeval_corpus_text_policy: user-and-assistant-turns-v1
longmemeval_auto_extract_entities: false
longmemeval_wait_for_memory_extraction: false
longmemeval_wait_for_memory_projection: false
longmemeval_graph_hnsw_efc: 150
longmemeval_graph_hnsw_m: 12
longmemeval_graph_knn_ef: 40
run_longmemeval_full: true
```

To inspect a run from your shell:

```bash
# Inspect run metadata
gh run view <run-id> --repo hyperb1iss/sibyl \
  --json status,conclusion,url,headSha,jobs

# Download the artifacts
mkdir -p /tmp/sibyl-eval-<run-id>
gh run download <run-id> --repo hyperb1iss/sibyl \
  --dir /tmp/sibyl-eval-<run-id>

# Parse the overall + per-type metrics
jq '{completion_status,total_questions,completed_questions,elapsed_seconds,
     overall,per_type,metadata,runtime,dataset,sibyl_commit,repeat_count,k_values}' \
  /tmp/sibyl-eval-<run-id>/longmemeval-live-full-*/longmemeval_live_full.json
```

To rerun the eval from a fork against your own ephemeral stack, fork the repo and dispatch the "Live
Runtime Eval" workflow with `run_longmemeval_full=true`. The job provisions its own SurrealDB,
backend, and worker, then tears them down at completion. Localhost mutation is refused unless the
caller passes `--allow-localhost` to the harness directly.

Gate a downloaded artifact before treating it as evidence:

```bash
moon run bench-gate -- /tmp/sibyl-eval-<run-id>/longmemeval-live-full-*/longmemeval_live_full.json \
  --profile ai-memory
```

## QA-Accuracy Lane

LongMemEval-S also has a separate end-to-end QA-accuracy lane. That lane answers a different
question from retrieval recall: after Sibyl retrieves sessions, can a reader model produce a correct
answer, and does the judge mark it correct?

Sibyl now has the harness and gate for that lane, but no public QA-accuracy number is citable until
a pinned model-backed artifact lands. The pinned run uses:

| Field         | Value                                      |
| ------------- | ------------------------------------------ |
| Reader        | `gpt-4o`                                   |
| Judge         | `gpt-5.2`                                  |
| QA schema     | `sibyl-longmemeval-s-qa-v1`                |
| Reader prompt | `sibyl-longmemeval-reader-v1`              |
| Judge prompt  | `sibyl-longmemeval-judge-v1`               |
| Rubric        | `longmemeval-s-answer-correctness-v1`      |
| Baseline file | `pinned-longmemeval-s-qa.json` once seeded |

The GitHub workflow enables the paid model-backed QA pass only for the Sunday full run or a manual
dispatch with `run_longmemeval_qa=true`. Manual sampled QA runs can set `longmemeval_qa_limit`; they
still validate the QA artifact contract but intentionally skip comparison against the future
full-dataset baseline.

The gate is:

```bash
moon run bench-gate -- .moon/cache/evals/longmemeval_live_full.json \
  --profile ai-memory \
  --require-accounting \
  --require-qa \
  --require-runtime qa_mode=model \
  --require-runtime qa_reader_model=gpt-4o \
  --require-runtime qa_judge_model=gpt-5.2
```

Once `benchmarks/results/ai-memory/pinned-longmemeval-s-qa.json` exists, the same gate also compares
`qa_accuracy` against that baseline and fails on a regression larger than 1.0 percentage point.

## Replay Quality Gate

Before dispatching another full live run, ranker changes can be replayed against a saved 500-case
artifact. Replay is never a public score; it is a cheap guard that catches regressions before
spending another CI run. Only a full live API run can produce a citable number.

## Why The Eval Looks Like This

We made four deliberate methodology choices that some other published numbers do not match:

1. **Live API path, not offline replay.** The harness uses real signup, real org creation, real
   entity API writes, real `/api/search` queries. An offline benchmark that skips these surfaces
   measures a different system than the one users get.
2. **Per-question physical tenant isolation.** Every question lives in its own SurrealDB namespace
   so retrieval cannot leak across the artificial haystack boundary. Every artifact records
   `cross_question_result_count`, and the gate requires it to be zero. This is stronger than
   metadata-scoped systems where one forgotten `WHERE` clause can break the boundary.
3. **No LLM extraction or LLM reranking.** Both are legitimate techniques and Sibyl supports async
   LLM extraction in production, but the retrieval baseline must not depend on either. Adding a
   reranker can lift scores; making it a retrieval prerequisite makes the system slow and expensive
   on every query.
4. **Strict recall, reported alongside hit.** The original LongMemEval offline runner labeled
   `hit@k` as `recall@k`, which overstated quality. We keep both names and report both numbers.

## Open Items

- LongMemEval-S is 500 questions. LongMemEval-M is not covered, and
  [LongMemEval-V2](./longmemeval-v2.md) has its own harness and claim rules.
- The QA-accuracy lane is wired and gated, but no QA-accuracy score is public until a pinned
  model-backed artifact is published.
- The live eval uses OpenAI embeddings. A local-embedding variant is on the roadmap for direct
  comparison against systems that report local-embedding numbers.

## Related

- [AI Memory Landscape](./ai-memory-landscape.md): honest competitive positioning
- [Retrieval System Architecture](../architecture/retrieval-system.md): how the retrieval path
  actually works
- [Benchmark Methodology](./benchmark-methodology.md): the broader eval ladder, gates, and reporting
  rules
