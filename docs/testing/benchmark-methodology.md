# Benchmark Methodology

Sibyl treats retrieval evaluation as a small ladder instead of one overloaded command. That keeps
local smoke checks, runtime artifacts, and offline baselines from drifting into the same story.

## Recommended Order

1. `moon run bench-live -- --label legacy --metadata store=legacy`
2. `moon run bench-live-smoke`
3. `moon run core:bench-context -- --cases path/to/context_cases.json --label retrieval-native`
4. `moon run bench-retrieval`
5. `uv run --with chromadb python benchmarks/longmemeval_bench.py /path/to/longmemeval.json --mode hybrid`

## What Each Command Measures

### `moon run bench-live`

This is the canonical runtime benchmark.

- Talks to the live Sibyl API
- Uses the same CLI auth headers a real local user would send
- Exercises real `/api/search` or RAG HTTP surfaces
- Uses the shared evaluation runner from `sibyl_core.evals`
- Writes timestamped JSON reports to `benchmarks/results/` by default
- Accepts `--label` and repeated `--metadata key=value` flags for saved artifacts

Use this when you want artifact-producing evidence about the current running stack.

### `moon run bench-live-smoke`

This is the fast live health guard.

- Talks to the live Sibyl API
- Stays read-only
- Verifies latency budgets, response shape, and basic filtered search behavior
- Runs as pytest so it fits normal local and CI-style workflows

Use this when you want a quick “is the live stack behaving sensibly?” signal.

### `moon run core:bench-context`

This is the live context-pack quality guard.

- Talks to the live `/api/context/pack` endpoint
- Runs the frozen fixture file at `benchmarks/context_pack_cases.json` for coding handoffs, personal
  memory, project recall, delegated recall, agent diary opt-in, private leak negatives,
  stale-decision replacement, and source grounding
- Measures pass rate, source grounding, facet order, mean/p95/max latency, token budget with the
  reported estimator margin, forbidden terms, and per-case leak signals
- Writes timestamped JSON reports under `.moon/cache/evals/` by default
- Writes the same JSON report shape used by the comparison and gate tools
- Adds release metadata for retrieval mode, embedding provider/model/dimensions, tokenizer method,
  dataset name, corpus hash, auth manifest ID, commit, and live runtime mode
- Adds W3 accounting for p50/p95/max latency, estimated input/output tokens, full-context baseline
  estimate, embedding calls, and warning-only cost records

Nightly seeds the deterministic baseline corpus first and passes
`.moon/cache/baseline-runtime-manifest.json` through `--auth-manifest`, so the context benchmark
uses the same short-lived baseline user token as the seeded corpus. It also runs the frozen suite
with `--repeat 20`; the report-level `latency_p95_ms` is computed across every repeated case run,
and the gate requires `metadata.repeat_count = 20`. Compare runs must label the artifact with the
retrieval mode, for example `--label retrieval-compare --metadata retrieval_mode=compare`.

Use this when changing retrieval, source grounding, prompt hooks, policy checks, or context-pack
rendering.

### `moon run bench-retrieval`

This is the synthetic component benchmark.

- Runs retrieval helpers in-process
- Measures temporal boosting, fusion, and small benchmark fixtures
- Good for regression checks while tuning retrieval internals
- Not a measurement of the deployed HTTP runtime path

Use this for local retrieval engineering, not for product positioning.

### `benchmarks/longmemeval_bench.py`

This is the offline baseline.

- Uses an ephemeral Chroma-backed index
- Replays LongMemEval-style data for apples-to-apples offline comparison
- Useful for internal baselines and competitor-style framing
- Explicitly does not touch the live graph or API runtime
- Writes schema `longmemeval-offline-v2` artifacts with full `case_results` by default, including
  question IDs, question types, answer session IDs, ranked session IDs, and per-case metrics

Use this for offline comparison work, and label it clearly as such.

### `moon run bench-longmemeval-v2-official`

This is the official LongMemEval-V2 harness path for Sibyl.

- Registers `sibyl_live_api` as a LongMemEval-V2 `Memory` backend.
- Ingests trajectories through the live Sibyl `/api/entities/bulk` surface.
- Uses project isolation for each official memory instance.
- Queries the live `/api/search` surface for reader context.
- Delegates answer generation and scoring to the official harness.
- Supports `--plan-only` to materialize inputs and verify run shape without model calls.

Use `moon run bench-longmemeval-v2-official-full` for actual scored runs. It adds the official
runtime dependencies, including `transformers` and `torch`, through `uv run --with` without making
them normal Sibyl application dependencies.

Use this for V2 full-suite work. A citable V2 result requires both `web` and `enterprise` domains at
the same tier, using the official reader and evaluator settings. See
[LongMemEval-V2](./longmemeval-v2.md) for the command sequence and requirements.

The committed `benchmarks/results/ai-memory/longmemeval_sibyl_raw_20260513.json` and
`benchmarks/results/ai-memory/longmemeval_sibyl_hybrid_20260513.json` artifacts are full
`longmemeval-offline-v2` outputs as of the v0.7 Surreal release work. Re-run the benchmark before
using those numbers for a later release candidate.

`benchmarks/results/ai-memory/manifest.json` records which AI memory benchmark artifacts are citable
for the release and which suites are planned coverage only. The manifest is checked against full
JSON artifacts or committed external archive manifests by `moon run bench-gate`.

The manifest uses `sibyl-ai-memory-benchmark-ledger-v2` for v1.1. In addition to citable and planned
rows, it carries:

- `gate_contracts`: the blocking, warning-only, and planned release gates that future receipts must
  satisfy before the claim can move into public docs
- `history`: the immutable summary directory used by nightly and weekly runs as the previous-run
  baseline for regression checks

Warning-only contracts, such as the initial cost-latency gate, are still evidence requirements. They
become blocking only after the manifest has enough citable baselines to compare against.

The W10 `doc-claim-gate` writes `benchmarks/results/ai-memory/doc-claim-receipt.json` with schema
`sibyl-doc-claim-receipt-v1`. It keeps retrieval recall, QA accuracy, LongMemEval-V2 LAFS Gain,
cost/latency, local-embedding runs, and self-reported citation usage as separate evidence axes. Docs
may cite an axis only when the matching manifest contract has a citable receipt or when the text
labels the axis as planned, warning-only, approval-bound, or deferred. Cost records use the
accounting field `estimated_total_usd`; the cost-latency gate stays warning-only until the ledger
has enough citable baselines for regression enforcement.

## Threshold Gates

Saved runtime artifacts should go through `moon run bench-gate -- <report.json>` before they count
as acceptance evidence.

The default `acceptance` profile enforces:

- `success@5 >= 0.40`
- `ndcg@10 >= 0.30`
- `mrr >= 0.25`
- `latency_ms <= 3000`

The lighter `smoke` profile keeps just the fast guardrails:

- `success@5 >= 0.20`
- `latency_ms <= 3000`

The `context-pack` profile gates dogfood context reports:

- `pass_rate >= 1.00`
- `latency_p95_ms <= 1000`
- `source_metadata_coverage >= 1.00`
- `facet_order_match_rate >= 1.00`
- `leak_count <= 0`
- `forbidden_term_matches <= 0`

It also requires citable release metadata:

- `metadata.retrieval_mode` is one of `pre-graphiti`, `post-graphiti`, `native`, or `compare`
- `metadata.embedding_provider`, `metadata.embedding_model`, and `metadata.embedding_dimensions`
- `metadata.tokenizer_estimate_method`
- `metadata.dataset_name` and `metadata.corpus_hash`
- `metadata.repeat_count`, `metadata.auth_manifest_id`, `metadata.sibyl_commit`, and
  `metadata.runtime_mode`
- `label` includes the retrieval mode so charts cannot silently mix incompatible runs

New citable context-pack and AI-memory receipts must also pass `--require-accounting`. The
accounting block uses schema `sibyl-eval-accounting-v1` and records p50/p95 latency, token
estimates, full-context baseline estimate, embedding calls, embedding cost, reader cost, judge cost,
and total estimated cost. Cost regression is warning-only until the ledger has two citable baselines
for the same lane.

The W4 write-path integrity gate is blocking for v1.1. It writes
`benchmarks/results/ai-memory/write-path-integrity-receipt.json` with schema
`sibyl-write-path-integrity-receipt-v1`, and `moon run bench-gate` enforces
`hallucinated_fact_count = 0`, `self_referential_write_count = 0`, and `low_signal_write_count = 0`
from that receipt across extraction, dream-cycle source selection, reflection, and consolidation
fixtures.

The W7 forgetting gate writes `benchmarks/results/ai-memory/forgetting-receipt.json` with schema
`sibyl-forgetting-receipt-v2`. Its survival semantics are explicit: citation (`last_used_at`) is the
strong reset, exposure (`last_recalled_at`) is a weighted slowdown, and legacy `last_accessed_at`
remains compatible but cannot outrank an explicit citation timestamp. Public claims must keep cited,
exposed-only, and untouched memories separate. Citation usage is self-reported by the agent or
client and means "this memory informed an answer"; it is not an outcome-lift metric until
TeamMemBench consumes task completion data in v1.2.

`leak_count` is a per-case sentinel: forbidden item and forbidden term matches are reported
separately, while the summary uses the larger of those two counts for each case so one leaked memory
is not double-counted when it trips both signals.

The current standard-runner context threshold is `latency_p95_ms <= 1000` across 20 repeated frozen
suite runs. Tighten or relax that number only with a saved report artifact and a matching
`retrieval-mode-history` update, because it is part of the native-default proof.

Native Surreal retrieval starts with a vector filter-selectivity threshold of `0.1`. When a filter
retains less than 10% of the searchable corpus, vector-only candidates are demoted unless a seeded
fixture proves they preserve useful recall under that selective filter.

Use `--require-metadata store=surreal` or other metadata filters when you need to prove which stack
produced the artifact. Use `--min-metric` and `--max-metric` to tighten a gate for a specific run
without forking the script.

Use `--baseline <report.json>` to make the gate fail on absolute regressions against a saved
baseline report. By default, the comparison uses the selected profile's metrics and allows zero
regression. Narrow the comparison with `--baseline-metric <metric>` and allow known measurement
noise only by naming it explicitly with `--max-regression <metric>=<amount>`. Custom baseline
metrics must have a known direction or an obvious lower-is-better suffix such as `_ms`, `_seconds`,
`_count`, `_chars`, or `_tokens`; unknown names fail closed.

Use `moon run bench-compare-reports -- <baseline.json> <candidate.json>` to render the human-facing
comparison table. The output includes accuracy, p50, p95, token estimate, embedding calls, estimated
cost, and accounting schema for every row.

## Product Gates

Post-v0.8 release claims use small product gates alongside benchmark gates. These do not replace the
broad package suites; they make the claim boundary repeatable from a clean checkout.

`moon run synthesis-gate` is the source-grounded synthesis gate. It delegates to focused
`sibyl-core` slices that require section-level source IDs, hidden-scope absence, unresolved-gap
reporting, artifact provenance, and remember provenance. Saved synthesis artifacts that support a
release note should live under `benchmarks/results/synthesis/`; local scratch artifacts can use
`.moon/cache/evals/synthesis/`.

`moon run adapter-ingest-gate` is the source-preserving ingest gate. It delegates to adapter
contract and mailbox ingest slices that require stable adapter identity, import resumability, dedupe
correctness, private scope enforcement, and source-preserving payload metadata. Saved ingest
receipts or import manifests that support a release note should live under
`benchmarks/results/source-ingest/`; local scratch artifacts can use
`.moon/cache/evals/source-ingest/`.

`benchmarks/context_pack_cases.json` carries the frozen context-pack case suite plus the gate
metadata for the default release run. Local reports from `core:bench-context` are written under
`.moon/cache/evals/`; promoted release artifacts should be copied to
`benchmarks/results/context-pack/` and then gated with
`moon run bench-gate -- <report.json> --profile context-pack`.

`benchmarks/golden_context_retrieval_dataset.json` is the labeled RC golden dataset that ties the
retrieval and context-pack fixtures together. It defines stable fixture document IDs, graded
retrieval positives, forbidden leakage sentinels, and per-case context labels. The loader in
`sibyl_core.evals` validates the schema, document references, and corpus fixture hashes so future
retrieval and context-pack runners can consume the same labels instead of drifting into parallel
fixtures.

## Reporting Rules

- Lead with `bench-live` when describing Sibyl’s current runtime behavior.
- Treat `bench-live-smoke` as a guardrail, not as headline benchmark evidence.
- Treat `core:bench-context` as a blocking context-quality check for retrieval and policy changes.
- Treat offline baselines as directional. Do not present them as production latency or runtime
  quality claims.
- Keep the artifact JSON from `bench-live` whenever you cite a number in docs or PRs.
- For AI memory benchmark and competitor claims, keep full raw artifacts plus overall metrics,
  per-slice metrics, corpus or dataset version, command, commit, runtime mode, and caveats.
- If a full artifact is too large for git, keep a committed external archive manifest with the
  archive location, digest, expiry, verification receipt, gate receipt, and exact summary fields.
- If the live stack or auth context is unavailable, say so explicitly instead of substituting an
  offline result.

## AI Memory Benchmark Result Records

External AI memory benchmarks live on a stricter evidence track than local smoke checks. Any LOCOMO,
RULER, Mem0, Zep, LangMem, or similar result that appears in public docs must have a full result
record rather than a headline score alone.

Store new artifacts under `benchmarks/results/ai-memory/` unless the suite requires a larger archive
outside git. If an artifact is too large to commit, commit a small manifest that names the archive
location, content hash, suite version, command, commit, runtime mode, and result summary.

Gate every new citable AI-memory artifact before it enters the release ledger:

```bash
moon run bench-gate -- benchmarks/results/ai-memory/<artifact>.json --profile ai-memory
```

Required record fields:

- suite name, suite version or commit, dataset name, split, and preprocessing notes
- Sibyl commit, runtime mode, graph engine, store, auth scope, and seeded corpus or import manifest
- embedding model, dimensions, index settings, generation model if used, tokenizer, and context
  budget
- exact command, environment variables that affect behavior, and timeout settings
- overall metrics and the complete per-slice table
- per-case result records with answer IDs, ranked result IDs, and case metrics
- ingestion time, query latency, p50/p95 latency, token estimates, embedding call count,
  warning-only cost estimate, timeout count, error count, and skipped-case count when available
- competitor version, hosted/self-hosted mode, ingestion path, and tuning when the result compares
  against another memory product
- claim boundary: what the result supports and what stays unproven

`moon run bench-gate` with no report argument gates the committed
`benchmarks/results/ai-memory/manifest.json` ledger, every citable artifact it names, and each
manifest `no_regression` baseline comparison. Use
`moon run bench-gate -- <artifact>.json --profile ai-memory --baseline <baseline>.json` for a single
uncommitted artifact that needs the same no-regression policy.

The canonical ledger for which rows are citable is
`docs/_archive/SURREALDB_GRAPHITI_EXIT_BENCHMARK_EVIDENCE.md`. If a benchmark suite is missing from
that ledger, add it there before citing the result anywhere else.

## Suggested PR Notes

- Runtime evidence: artifact path from `benchmarks/results/`
- Smoke evidence: `moon run bench-live-smoke`
- Offline evidence, if relevant: `moon run bench-retrieval` or `longmemeval_bench.py`

## Store Comparison Flow

> **Note (2026-07):** the legacy-vs-Surreal comparison below is historical. FalkorDB and PostgreSQL
> are fully removed, and the current `sibyld migrate` CLI accepts only
> `--source-type surreal-archive --target-mode surreal`; the `legacy-archive`, `postgres-rehearsal`,
> and `--restore-database-dump` flags no longer exist.

To compare two Surreal deployments (or validate a restore) on the same graph data today:

1. Export a manifest archive from the source with
   `sibyld migrate export --org-id <org> --output /tmp/migration.tar.gz`
2. Rehearse the import on the target with
   `moon run migrate-rehearse -- /tmp/migration.tar.gz --source-type surreal-archive --target-mode surreal --yes`
3. Run `moon run bench-live -- --label <store> --metadata store=<store>` against each stack
4. Compare the saved artifacts with
   `uv run python benchmarks/compare_eval_reports.py <baseline.json> <candidate.json>`

Historically, the same flow compared FalkorDB/PostgreSQL against Surreal via
`--source-type legacy-archive --target-mode postgres-rehearsal --restore-database-dump`, and
maintenance-window swaps ran `moon run migrate-cutover -- ... --write-freeze-confirmed` with
reopening writes as a separate explicit `--reopen-writes --acknowledge-no-instant-rollback` step.
Those enums were removed in the v0.6–v1.0 line; the citable comparison artifacts from that era live
in the benchmark ledger.

Run `moon run chaos-archive -- /tmp/migration.tar.gz` when you want a quick corruption drill for the
archive format itself. The current probe mutates checksums, graph counts, and organization IDs to
make sure the validator rejects obviously bad cutover inputs before a restore window starts.
