---
title: Benchmarks
description:
  How Sibyl evaluates retrieval quality, what we currently measure, and where we stand against the
  AI memory systems field
---

# Benchmarks

Sibyl publishes retrieval quality numbers because saying "we have great memory" without a measured
metric is not a claim, it's a vibe. The pages in this section record what we measure, how we measure
it, what the latest scores are, and how those numbers compare to the public AI memory systems
landscape.

## At A Glance

::: tip Latest LongMemEval-S live run **500/500 `hit@5`** · **96.67% strict `recall@5`** · **98.68%
`recall@10`** · 94.21 nDCG@5

Live API path. SurrealDB-native graph + vector retrieval. OpenAI embeddings. No LLM extraction. No
LLM reranking. Per-question physical tenant isolation. Full artifact and diagnostics published
([run 26273942749](https://github.com/hyperb1iss/sibyl/actions/runs/26273942749)). :::

## Pages In This Section

- [LongMemEval Results](./longmemeval.md) — the headline eval claim, full per-type breakdown,
  configuration, latency, score progression, reproduction commands, claim boundary.
- [AI Memory Landscape](./ai-memory-landscape.md) — honest competitive positioning. The
  retrieval-vs-QA-accuracy distinction, where Sibyl sits in the field, what we trail academic SOTA
  on.
- [Benchmark Methodology](./benchmark-methodology.md) — the full eval ladder, gate profiles,
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

The full LongMemEval live run is what we cite for public claims. Everything else is a guardrail or a
baseline. Reporting rules and gate profiles are documented in
[Benchmark Methodology](./benchmark-methodology.md).

## Why The Numbers Are Reproducible

Every full LongMemEval run uploads:

- `longmemeval_live_full.json` — overall, per-type, and per-case results with ranked result IDs,
  answer ranks, latencies, ingest stats, readiness probes, and cross-question leakage counts.
- A diagnostics summary covering warning counts, slow-query totals, SurrealDB resource usage at
  diagnostics time, and any timeout events.
- Run metadata: commit SHA, dataset corpus hash, embedding provider, HNSW settings, fusion backend,
  corpus text policy, projection settings, extraction settings, concurrency, repeat count.

To verify a published number, download the corresponding artifact, run `jq` against it, and compare
to the headline. The [LongMemEval Results page](./longmemeval.md#reproducibility) has the exact
commands.
