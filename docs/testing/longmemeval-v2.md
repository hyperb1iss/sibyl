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

## v1.3 sealed release experiment

The authoritative initial A/A stage runs in GitHub Actions. The `LongMemEval V2 Release A/A`
workflow dispatches six separate paid workflow runs, builds one frozen baseline for the other five
arms, and publishes one digest-bound bundle after all six runs pass. A completion-triggered job
handles aggregation, so no controller job has to remain alive while the builder and consumers
execute.

The local release runner remains available for later stages and investigation. Its lower-level
commands are also useful during development, but neither path replaces the sealed CI A/A receipt.

### Run initial A/A in CI

Set `OPENROUTER_API_KEY` and `OPENAI_API_KEY` as repository secrets. The paid child jobs use the
`longmemeval-paid` environment. Approve the builder first, then approve the five consumers after the
builder publishes the frozen web and enterprise databases.

Dispatch from the exact `main` commit under evaluation:

```bash
git fetch origin main
source_sha="$(git rev-parse origin/main)"
gh workflow run longmemeval-v2-release-aa.yml \
  --ref main \
  -f experiment_id=sibyl-v1.3-aa \
  -f source_sha="$source_sha"
```

The controller rejects a non-`main` ref or a requested SHA that differs from its checkout. The six
child runs bind their workflow run IDs, source SHA, fixed A/A seeds, official dataset revision,
reader, evaluator, runtime geometry, and shared baseline identity. The builder uploads each frozen
database separately from scored evidence. Consumer jobs validate its manifest and byte digests
before restoring it.

After the completion-triggered controller run succeeds, download and import its authoritative
bundle. Replace the two run IDs with the dispatch run and completion-triggered run shown by GitHub:

```bash
controller_run_id=<dispatch-run-id>
aggregation_run_id=<completion-triggered-run-id>
bundle_root="/absolute/path/to/v1.3-aa-ci-bundle"

gh run download "$aggregation_run_id" \
  --repo hyperb1iss/sibyl \
  --name "longmemeval-v2-release-aa-${source_sha}-${controller_run_id}" \
  --dir "$bundle_root"

moon run bench-longmemeval-v2-release-ci -- \
  import-aa \
  --bundle-root "$bundle_root" \
  --output /absolute/path/to/aa-authorization.json
```

The import command rechecks every file size and digest, validates the six distinct workflow
executions, and rewrites only the local artifact paths. Keep the downloaded bundle with the
generated authorization. Frozen database artifacts expire after seven days. The final A/A bundle and
controller evidence remain available for 30 days.

If a child run fails, preserve its diagnostics and rerun the controller with a new experiment ID. Do
not mix successful arms from separate controller runs. The run map and bundle validator reject that
splice.

### Run a local sealed stage

Do not generate a paid plan from a feature branch. Merge the runner first, create a clean checkout
of `main`, fetch the remote, and confirm that local `main` and `origin/main` name the same commit.
The planner repeats those checks against the live remote and rejects untracked files, dirty
submodules, moved refs, and detached checkouts.

### Fixed inputs

The release contract accepts one exact input set:

- The official harness checkout is clean at `2cc8c540bdb87fe6761629b585e727e1c4704520`.
- The Hugging Face dataset revision is `f152293e235517d504809563c833d7190b8c713b`.
- The complete Small corpus has 451 questions: 240 web and 211 enterprise.
- The public package description is
  `benchmarks/longmemeval_v2_release_assets/SYSTEM_DESCRIPTION.md`.
- The packaged adapter is `benchmarks/longmemeval_v2_memory/sibyl_memory.py`.
- The reader is `qwen/qwen3.5-9b` through OpenRouter. The judge is `gpt-5.2`.
- The Sibyl API points at a disposable local stack with a database reserved for this experiment.
- The package publication path runs on macOS 26 or newer. Its evidence authority uses immutable
  filesystem flags to protect completed arm and stage packages.

The planner requires these dataset payload hashes:

| Payload                       | SHA-256                                                            |
| ----------------------------- | ------------------------------------------------------------------ |
| `questions.jsonl`             | `0a3ae5ebea938c24d7800e1e0b0828e08ae1646f939a53853b2b8cdc08e292b7` |
| `trajectories.jsonl`          | `363cec9a8e87aa8d9101ce4e600aadbf7031d674056ebe4f969e8424abc5f3c6` |
| `haystacks/lme_v2_small.json` | `9b5301defb23a088a5f06e45ff8d5f35e569d78305a66d492046a9fff9b46593` |

The stage spec also freezes the local API URL, runtime concurrency, retries, context geometry,
retrieval mode, seeds, arm configuration, memory lineage, and per-domain cost caps. The planner
stores paths in the private local plan, but the public execution identity contains only the
repository, ref, commit, canonical run UUID, and attempt number.

### Credentials and approval

Set both provider variables in the parent shell before the paid command:

```bash
: "${OPENROUTER_API_KEY:?set OPENROUTER_API_KEY in the parent shell}"
: "${OPENAI_API_KEY:?set OPENAI_API_KEY in the parent shell}"
```

Do not pass a token value or a credential-file path as a CLI argument. Plans, public identities,
receipts, and redacted logs contain environment variable names only. The runner refuses provider
work unless every domain first produces an exact official plan-only reservation for the full Small
corpus. The fixed cap is \$4.25 per domain for machine, naive, and render-control arms. The
render-treatment cap is \$4.75 per domain. Any reservation or actual summed cost above its sealed
cap stops the stage.

### One stage at a time

Each stage has a separate spec, plan, paid output root, official-arm package root, and final
receipt. No command creates the next stage.

| Stage         | Fixed work                                                                     | Required authority before the next stage                            |
| ------------- | ------------------------------------------------------------------------------ | ------------------------------------------------------------------- |
| A/A initial   | Three paired machine-versus-machine passes                                     | A/A authorization with `PASS` or `NEEDS_TWO_MORE`                   |
| A/A extension | Two additional paired passes, only when the initial receipt requires them      | Final five-pass A/A authorization or a rig-blocked result           |
| Anchor        | One machine arm over both domains                                              | Post-decontamination anchor plus race preregistration authorization |
| Race          | Three machine-versus-naive paired passes and one matched-geometry sanity pass  | Race decision plus render preregistration authorization             |
| Render        | Three control-versus-treatment paired passes, or zero runs when not applicable | Render decision or a canonical `NOT_APPLICABLE` receipt             |

The initial A/A stage always runs all three declared passes. The extension always runs both extra
passes. Race and render always run every declared pass. Scores never stop a pass wave early.

Create the next spec only after the prior stage package authorizes it. An A/A extension and the
anchor bind the A/A authorization. The race binds the final A/A authorization and the race
preregistration authorization issued by the anchor. The render stage binds the render
preregistration authorization issued by the race. A zero-run render spec is valid only when that
authorization says the treatment is not applicable.

### Plan a stage

Prepare an isolated database directory, then start the pinned SurrealDB 3.2.3 eval service and
Valkey:

```bash
export SIBYL_RELEASE_ROOT=/absolute/path/to/fresh-v1.3-eval-root
mkdir -p "$SIBYL_RELEASE_ROOT/surreal"

COMPOSE_PROJECT_NAME=sibyl-v13-eval SIBYL_REDIS_PORT=6393 \
  docker compose --env-file /dev/null --profile eval --profile redis \
  up -d surrealdb-eval redis
```

The eval service binds SurrealDB to port 8018. Its 8 GiB block cache, 128 MiB write buffer, and four
write buffers prevent the full small-corpus ingest from claiming the machine's entire memory limit.
Root credentials stay in the container environment rather than its command arguments.

Launch the release API and worker in two more terminals:

```bash
moon run api:serve-local-embeddings
moon run api:worker-local-embeddings
```

The sealed release path requires both tasks. Each task connects to SurrealDB on port 8018 and Valkey
on port 6393, installs `sentence-transformers==6.0.0`, and pins the `local` provider with
`sentence-transformers/all-MiniLM-L6-v2`. Do not substitute the generic API or worker task. Keep
both processes running through release execution. The adapter rejects a worker that did not report
observed MiniLM ingestion and requires observed MiniLM query usage from the API before accepting
cache-only query receipts.

Choose absolute, canonical paths that do not exist yet for the plan file and paid output root. The
plan file's immediate parent must also be a fresh directory dedicated to that one plan. The
publisher makes the directory immutable with the plan, so each later stage needs another fresh
parent. Both paths must live outside and remain disjoint from the Sibyl checkout, official checkout,
dataset, stage spec, and fixed package inputs. Neither path may traverse a symlink. The plan file
and paid root must also be disjoint from each other. The planner binds the reviewed system
description and adapter without override flags.

```bash
moon run bench-longmemeval-v2-release -- release-plan \
  --spec /absolute/path/to/aa-initial-spec.json \
  --official-repo /absolute/path/to/LongMemEval-V2 \
  --data-root /absolute/path/to/longmemeval-v2-v1-3 \
  --output-root /absolute/path/to/evidence/aa-initial \
  --output /absolute/path/to/plans/aa-initial/stage.json
```

Planning makes no provider call and does not create the paid output root. It seals the source and
input identities, allocates a distinct canonical execution UUID for each arm, expands both domains,
and records every plan-only and paid command.

### Run the sealed stage

The local-machine contract permits one through four workers. Four is the preregistered default, not
a product throughput limit.

```bash
moon run bench-longmemeval-v2-release -- release-run \
  --plan /absolute/path/to/plans/aa-initial/stage.json \
  --max-workers 4
```

Before any paid wave, the runner executes and validates every sealed plan-only command. A later wave
that reuses memory also reattests both domain memories before either domain can spend. The runner
then joins the whole wave, validates the complete claimed root and every official artifact, and
publishes domain exits atomically. A peer failure prevents later waves from starting.

### Package each official arm

Run the arm command once for every `runs[].arm_id` in the sealed plan. All arms for a stage share
one fresh canonical package root. The command creates one immutable official authority or validates
the already published authority on retry. It does not package the stage outcome.

```bash
moon run bench-longmemeval-v2-release -- release-arm-package \
  --plan /absolute/path/to/plans/aa-initial/stage.json \
  --arm-id aa-1-left \
  --packages-root /absolute/path/to/official-packages/aa-initial
```

Each arm package runs the official operating-point, submission, combined-metrics, and receipt
builders under a write-confined staging directory. Its command receipts bind redacted logs, exact
commands, return codes, outputs, the reviewed public description, and the adapter. Publication is
atomic and content addressed.

### Package and verify the stage

A/A and render packaging need no preregistration template. Anchor packaging needs the reviewed,
scoreless race template. Race packaging needs the reviewed, scoreless render template. The package
owner injects the canonical upstream receipts; a template cannot supply scores, stack identity, or
producer-owned digests.

```bash
moon run bench-longmemeval-v2-release -- release-package \
  --plan /absolute/path/to/plans/aa-initial/stage.json \
  --packages-root /absolute/path/to/official-packages/aa-initial

moon run bench-longmemeval-v2-release -- release-verify \
  --plan /absolute/path/to/plans/aa-initial/stage.json
```

Add `--preregistration-template /absolute/path/to/template.json` only for anchor or race packaging.
The stage package contains the paired-pass artifacts, score-aware rig outcome, package claim, final
stage receipt, and a scoreless downstream authorization when the stage issues one. The command
freezes the completed tree before publishing `PACKAGED`. `release-verify` opens that immutable
authority, reconstructs the outcome, revalidates the current execution inputs and status, and
returns the canonical stage receipt.

### Restart and failure rules

Retry only the exact command with the exact plan and roots. A valid completed arm or stage is
consumed through its canonical validator. A stale command, changed source, moved remote, different
model or runtime, changed dataset, modified saved memory, incomplete receipt, missing cost, foreign
file, symlink escape, or partial output is a hard failure. The runner never deletes or silently
repairs evidence.

A terminal `FAIL` is resumable only when every existing paid domain has a complete receipt whose
cost, artifacts, command log, and sealed execution identity still validate. The retry repeats all
live planning and stage barriers before it reuses those domains, so a post-wave barrier failure does
not repay valid provider work. A failed domain receipt, partial output, or drifted artifact still
requires a new plan with an explicitly fresh output root. Preserve the failed root for audit, and do
not advance until `release-verify` succeeds and its receipt authorizes the transition.

## Lower-level commands

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
  --output-root .moon/cache/evals/lme-v2-phase0/runs \
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
`--checkpoint-dir`. After each completed trajectory, the adapter appends its local chunk catalog and
atomically records the completed IDs, pending background job IDs, project, run, representation, and
provider usage. Re-running the same command against the same isolated API and database resumes from
the last durable trajectory. The checkpoint does not contain the SurrealDB data itself, so it cannot
resume against a fresh or deleted database.

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
  --arm trajectory_18k=.moon/cache/evals/lme-v2-phase0/runs/diagnostics/trajectory_18k/diagnostic_report.json \
  --arm state_18k=.moon/cache/evals/lme-v2-phase0/runs/diagnostics/state_18k/diagnostic_report.json \
  --arm state_8k=.moon/cache/evals/lme-v2-phase0/runs/diagnostics/state_8k/diagnostic_report.json \
  --arm state_8k_diverse=.moon/cache/evals/lme-v2-phase0/runs/diagnostics/state_8k_diverse/diagnostic_report.json \
  --arm state_8k_diverse_neighbors=.moon/cache/evals/lme-v2-phase0/runs/diagnostics/state_8k_diverse_neighbors/diagnostic_report.json \
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
  --baseline-run web=.moon/cache/evals/lme-v2-phase0/runs/reader/baseline_fixed_reader/web \
  --baseline-run enterprise=.moon/cache/evals/lme-v2-phase0/runs/reader/baseline_fixed_reader/enterprise \
  --output .moon/cache/evals/lme-v2-phase1/reader_plan.json
```

Replicate the primary baseline-versus-winner contrast with five fixed reader passes. Pass one reuses
the completed reader artifacts. Passes two through five use predeclared question-order seeds and
load the saved memory states, so they do not rebuild memory or regenerate stored embeddings. The
plan freezes the source runtime configuration and requires every pass; there is no sequential
stopping based on intermediate scores.

```bash
moon run bench-longmemeval-v2-ablations -- reader-replication-plan \
  --reader-plan .moon/cache/evals/lme-v2-phase1/reader_plan.json \
  --source-run baseline_fixed_reader=.moon/cache/evals/lme-v2-phase0/runs/reader/baseline_fixed_reader \
  --source-run winner_fixed_reader=.moon/cache/evals/lme-v2-phase0/runs/reader/winner_fixed_reader \
  --output-root .moon/cache/evals/lme-v2-phase2/reader_replication \
  --output .moon/cache/evals/lme-v2-phase2/reader_replication_plan.json
```

The runner validates completed receipts before skipping them, executes one pass wave at a time, and
writes a log beside each run. Restarting the same command resumes from the first incomplete or
invalid receipt.

```bash
OPENROUTER_API_KEY="$(tr -d '\r\n' < openrouter.key)" \
  moon run bench-longmemeval-v2-ablations -- reader-replication-run \
  --plan .moon/cache/evals/lme-v2-phase2/reader_replication_plan.json \
  --max-workers 4
```

After all five passes validate, build the receipt-bound replication report. It summarizes per-pass
and majority-vote accuracy, question-level stability, a predeclared question-cluster bootstrap,
domain deltas, provider-reported cost, and the frozen `GO`, `NO-GO`, or `RESEARCH-MORE` decision.

```bash
moon run bench-longmemeval-v2-ablations -- reader-replication-report \
  --plan .moon/cache/evals/lme-v2-phase2/reader_replication_plan.json \
  --output .moon/cache/evals/lme-v2-phase2/reader_replication_report.json
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
- Live disposable SurrealDB 3.2.3 and Valkey services, plus the Sibyl API and worker started with
  `api:serve-local-embeddings` and `api:worker-local-embeddings`. The adapter mutates the target
  through `/entities` and `/search`.
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
