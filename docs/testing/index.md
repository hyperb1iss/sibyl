---
title: Benchmarks
description:
  How Sibyl evaluates retrieval quality, what we currently measure, and where we stand against the
  AI memory systems field
---

# Benchmarks

Saying "we have great memory" without a measured metric is not a claim, it's a vibe. The pages in
this section record what we measure, how we measure it, which results are citable, and how results
compare across the public AI memory systems landscape.

## At A Glance

::: info No citable LongMemEval-S number right now

The earlier headline run predates 1.0 and has been withdrawn as a public claim. The live eval
harness still runs on the API path with SurrealDB-native graph and vector retrieval, OpenAI
embeddings, no LLM extraction, no LLM reranking, and per-question physical tenant isolation. A new
number is published only when a current run passes the benchmark gate and the manifest marks it
citable.

:::

## Pages In This Section

- [LongMemEval-S](./longmemeval.md): how the live eval runs, what its metrics mean, reproduction
  commands, and the claim boundary.
- [LongMemEval-V2](./longmemeval-v2.md): the official full-suite harness path, live Sibyl memory
  adapter contract, and honest-run requirements.
- [AI Memory Landscape](./ai-memory-landscape.md): honest competitive positioning. The
  retrieval-vs-QA-accuracy distinction, where Sibyl sits in the field, what we trail academic SOTA
  on.
- [Benchmark Methodology](./benchmark-methodology.md): the full eval ladder, gate profiles,
  reporting rules, and the AI memory ledger format.

## What We Measure

Sibyl runs a small ladder of evaluations, each with a specific scope:

| Eval                          | What it measures                                      | When to cite             |
| ----------------------------- | ----------------------------------------------------- | ------------------------ |
| `moon run bench-live-smoke`   | Fast live health guard (latency, response shape)      | Local sanity check       |
| `moon run core:bench-context` | Frozen context-pack fixtures (eight scenarios)        | Retrieval & policy churn |
| `moon run bench-live`         | Canonical runtime benchmark against live API          | Runtime evidence claims  |
| `LongMemEval Live Smoke` (CI) | 25-question live LongMemEval slice on every dispatch  | Quick regression signal  |
| `LongMemEval Live Full` (CI)  | Full 500-question LongMemEval against ephemeral stack | Public eval claims       |
| `LongMemEval offline`         | Chroma-backed offline replay                          | Algorithm baseline       |

Only a full LongMemEval live run that the manifest marks citable can back a public claim. Everything
else is a guardrail or a baseline. Reporting rules and gate profiles are documented in
[Benchmark Methodology](./benchmark-methodology.md).

## Why The Numbers Are Reproducible

Every full LongMemEval run uploads:

- `longmemeval_live_full.json`: overall, per-type, and per-case results with ranked result IDs,
  answer ranks, latencies, ingest stats, readiness probes, and cross-question leakage counts.
- A diagnostics summary covering warning counts, slow-query totals, SurrealDB resource usage at
  diagnostics time, and any timeout events.
- Run metadata: commit SHA, dataset corpus hash, embedding provider, HNSW settings, fusion backend,
  corpus text policy, projection settings, extraction settings, concurrency, repeat count.

To verify a published number, download the corresponding artifact, run `jq` against it, and compare
it to the cited value. The [LongMemEval-S page](./longmemeval.md#reproducibility) has the exact
commands.
