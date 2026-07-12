---
title: LongMemEval-V2
description: How Sibyl runs the official LongMemEval-V2 full-suite harness honestly
---

# LongMemEval-V2

LongMemEval-V2 is not the same shape as LongMemEval-S. V1 is a retrieval benchmark for finding the
right memory item. V2 is an official memory-system harness: the memory backend ingests web-agent
trajectories, returns compact context for a question, a fixed reader model answers, and the official
scorers grade the answer.

Sibyl's V2 path therefore uses the official `Memory` interface instead of a benchmark-only oracle.
The adapter writes trajectories through the live Sibyl API and queries `/api/search`; it strips the
gold answer from official query context before backend code can read it, and it never sees gold
trajectory IDs.

## Current Commands

Download the text-context dataset slice:

```bash
moon run bench-longmemeval-v2-download -- \
  --data-root .moon/cache/benchmarks/longmemeval-v2-full
```

Add `--include-trajectory-screenshots` only when testing a memory backend that returns image context
items.

Fast metadata check:

```bash
moon run bench-longmemeval-v2-probe -- \
  /path/to/longmemeval-v2 \
  --tier medium \
  --validate-trajectories
```

Plan an official run without model calls:

```bash
moon run bench-longmemeval-v2-official -- \
  --data-root /path/to/longmemeval-v2 \
  --domain enterprise \
  --tier small \
  --output-dir runs/sibyl_enterprise_small \
  --plan-only \
  --allow-localhost
```

Run one official domain with the official runtime dependencies:

```bash
moon run bench-longmemeval-v2-official-full -- \
  --official-repo /path/to/LongMemEval-V2 \
  --data-root /path/to/longmemeval-v2 \
  --domain enterprise \
  --tier small \
  --output-dir runs/sibyl_enterprise_small \
  --api-url http://127.0.0.1:3334/api \
  --allow-localhost \
  --reader-base-url https://openrouter.ai/api/v1 \
  --reader-model qwen/qwen3.5-9b \
  --reader-api-key-env OPENROUTER_API_KEY \
  --evaluator-model gpt-5.2
```

Test live Sibyl ingestion without reader or evaluator model calls:

```bash
moon run bench-longmemeval-v2-official-full -- \
  --official-repo /path/to/LongMemEval-V2 \
  --data-root .moon/cache/benchmarks/longmemeval-v2-full \
  --domain enterprise \
  --tier small \
  --output-dir runs/sibyl_enterprise_ingest_1 \
  --limit 1 \
  --allow-localhost \
  --save-memory \
  --skip-evaluation
```

## Resumable Phase-0 Ablations

The phase-0 workflow separates expensive memory construction from cheap retrieval and reader
experiments. It builds three memory representations once, evaluates exactly five retrieval arms on
the frozen diagnostic slice, then permits exactly three fixed-reader configurations only after a
deterministic `GO` decision.

Validate that the official loader and its import-time dependencies are available:

```bash
moon run bench-longmemeval-v2-ablations -- doctor \
  --official-repo .moon/cache/longmemeval-v2-official
```

Materialize the complete experiment plan without model calls or service changes:

```bash
moon run bench-longmemeval-v2-ablations -- plan \
  --official-repo .moon/cache/longmemeval-v2-official \
  --data-root .moon/cache/benchmarks/longmemeval-v2-full \
  --output-root .moon/cache/evals/lme-v2-phase0 \
  --output .moon/cache/evals/lme-v2-phase0/ablation_plan.json \
  --api-url http://127.0.0.1:3434/api \
  --allow-localhost
```

The plan contains executable Moon command arrays for:

1. `trajectory_18k`, `state_18k`, and `state_8k` memory builds for both domains.
2. Five retrieval-only arms over the 32 frozen questions.
3. One diagnostic report per arm.
4. The pre-registered `GO`, `NO-GO`, and `RESEARCH-MORE` thresholds.

Each memory-build command includes `--save-memory`, `--skip-evaluation`, and a dedicated
`--checkpoint-dir`. After each completed trajectory, the adapter appends its local chunk catalog
and atomically records the completed IDs, pending background job IDs, project, run, representation,
and provider usage. Re-running the same command against the same isolated API and database resumes
from the last durable trajectory. The checkpoint does not contain the SurrealDB data itself, so it
cannot resume against a fresh or deleted database.

Each completed memory artifact contains:

- `memory_config.json`, without API tokens, email addresses, or passwords.
- `chunk_catalog.jsonl.gz`, used for neighbor stitching without another ingest.
- `memory_manifest.json`, with hashes, ingest provenance, and provider-reported embedding cost.

Retrieval runs append and fsync one result per completed question. A restart validates the run and
memory hashes, skips completed question IDs, and rewrites a deterministic final JSONL file. Query
embedding cost and latency are summarized in `retrieval_summary.json`.

After all five diagnostic reports exist, evaluate the frozen promotion gate:

```bash
moon run bench-longmemeval-v2-ablations -- gate \
  --slice benchmarks/longmemeval_v2_diagnostic_slice.json \
  --arm trajectory_18k=.moon/cache/evals/lme-v2-phase0/diagnostics/trajectory_18k/diagnostic_report.json \
  --arm state_18k=.moon/cache/evals/lme-v2-phase0/diagnostics/state_18k/diagnostic_report.json \
  --arm state_8k=.moon/cache/evals/lme-v2-phase0/diagnostics/state_8k/diagnostic_report.json \
  --arm state_8k_diverse=.moon/cache/evals/lme-v2-phase0/diagnostics/state_8k_diverse/diagnostic_report.json \
  --arm state_8k_diverse_neighbors=.moon/cache/evals/lme-v2-phase0/diagnostics/state_8k_diverse_neighbors/diagnostic_report.json \
  --output .moon/cache/evals/lme-v2-phase0/ablation_gate.json
```

The initial experiment plan contains the two baseline fixed-reader commands. Run those only after
the retrieval gate permits reader work. `reader-plan` rejects every decision except `GO`. With a
passing gate and completed baseline reader runs, it derives the observed median baseline context
budget and emits exactly three configurations: baseline, winner, and winner with matched context
tokens.

```bash
moon run bench-longmemeval-v2-ablations -- reader-plan \
  --plan .moon/cache/evals/lme-v2-phase0/ablation_plan.json \
  --gate .moon/cache/evals/lme-v2-phase0/ablation_gate.json \
  --baseline-run web=.moon/cache/evals/lme-v2-phase0/reader/baseline_fixed_reader/web \
  --baseline-run enterprise=.moon/cache/evals/lme-v2-phase0/reader/baseline_fixed_reader/enterprise \
  --output .moon/cache/evals/lme-v2-phase0/reader_plan.json
```

A leaderboard-valid operating point needs both domains at the same tier and method:

```bash
moon run bench-longmemeval-v2-official-full -- ... --domain enterprise --tier small
moon run bench-longmemeval-v2-official-full -- ... --domain web --tier small

python /path/to/LongMemEval-V2/leaderboard/build_submission_step_1_single_operating_point.py \
  runs/sibyl_web_small \
  runs/sibyl_enterprise_small \
  sibyl_live_api \
  official \
  small \
  --method sibyl_live_api \
  --output-root runs/submissions \
  --force

python /path/to/LongMemEval-V2/leaderboard/build_submission_step_2_build_package.py \
  sibyl_live_api \
  runs/SYSTEM_DESCRIPTION.md \
  benchmarks/longmemeval_v2_memory/sibyl_memory.py \
  runs/submissions/sibyl_live_api/operating_points/official \
  --output-root runs/submissions \
  --force

python /path/to/LongMemEval-V2/leaderboard/combine_aggregated_metrics.py \
  runs/sibyl_web_small/aggregated_metrics.json \
  runs/sibyl_enterprise_small/aggregated_metrics.json \
  -o runs/sibyl_small_combined_metrics.json
```

Build the receipt from the official submission package:

```bash
moon run bench-longmemeval-v2-official -- \
  --official-repo /path/to/LongMemEval-V2 \
  --data-root /path/to/longmemeval-v2 \
  --domain combined \
  --tier small \
  --output-dir runs/sibyl_small_combined_receipt \
  --receipt-only \
  --metric-overview runs/submissions/sibyl_live_api/operating_points/official/metric_overview.json \
  --combined-metrics runs/sibyl_small_combined_metrics.json \
  --submission-overview runs/submissions/sibyl_live_api/submission_overview.json \
  --submission-archive runs/submissions/sibyl_live_api.tar.gz \
  --web-output-dir runs/sibyl_web_small \
  --enterprise-output-dir runs/sibyl_enterprise_small \
  --receipt-output runs/sibyl_small_combined_receipt.json
```

Gate the receipt before pinning it as release evidence:

```bash
moon run bench-gate -- \
  runs/sibyl_small_combined_receipt.json \
  --profile longmemeval-v2
```

## Honest-Run Requirements

- Official LongMemEval-V2 checkout available through `--official-repo`.
- Full dataset prepared with `questions.jsonl`, `haystacks/lme_v2_<tier>.json`,
  `trajectories.jsonl`, and screenshots if image evidence is enabled.
- Live disposable Sibyl API stack. The adapter mutates the target through `/entities` and `/search`.
- Reader model endpoint, normally OpenRouter `qwen/qwen3.5-9b` with `OPENROUTER_API_KEY`.
- Evaluator key/model for LLM-graded categories, normally `gpt-5.2`.
- Same method and tier for `web` and `enterprise` before combining metrics.
- Combined receipt with official repo SHA, official harness presence, source web/enterprise run
  artifacts, dataset hashes, reader and evaluator model pins, LAFS gain, latency, token/cost
  accounting, and PASS checks for every required evidence surface.

## Adapter Contract

`benchmarks/longmemeval_v2_memory/sibyl_memory.py` registers `sibyl_live_api` with the official
harness.

For each memory instance it:

1. Authenticates once and reuses the token inside the process.
2. Creates an isolated Sibyl project unless `--project-id` is supplied.
3. Converts each trajectory into state-aware `session` chunks.
4. Writes chunks with `POST /api/entities/bulk`.
5. Searches only that project with `POST /api/search`.
6. Returns text context items to the official reader.

The project boundary is the V2 equivalent of the V1 per-question tenant boundary. It avoids
cross-question leakage without relying on repeated local signups, which would fight the local-first
single-user default.

## Claim Boundary

The current V2 path proves we can run Sibyl inside the official full-suite contract. It is not yet a
published V2 score until both domains complete with the official reader and evaluator.

The PR and push workflow path is intentionally metadata-only. The paid official full-suite path is
manual-only through `workflow_dispatch` with `run_official_full: true`; it requires a reachable
Qwen3.5-9B reader endpoint, `OPENROUTER_API_KEY`, and `OPENAI_API_KEY`.

Known limits:

- The adapter is text-context only today. It preserves screenshot references in text when requested,
  but does not yet return image context items.
- Medium haystacks can approach 500 trajectories per question; this is intentionally a stress test
  of ingestion backpressure and search isolation.
- The official harness loads trajectories into memory. Large runs should use a machine sized for the
  dataset and model endpoints.
