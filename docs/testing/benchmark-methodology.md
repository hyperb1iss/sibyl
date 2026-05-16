# Benchmark Methodology

Sibyl now treats retrieval evaluation as a small ladder instead of one overloaded command. That
keeps local smoke checks, runtime artifacts, and offline baselines from drifting into the same
story.

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

The committed `benchmarks/results/ai-memory/longmemeval_sibyl_raw_20260513.json` and
`benchmarks/results/ai-memory/longmemeval_sibyl_hybrid_20260513.json` artifacts are full
`longmemeval-offline-v2` outputs as of the v0.7 Surreal release work. Re-run the benchmark before
using those numbers for a later release candidate.

`benchmarks/results/ai-memory/manifest.json` records which AI memory benchmark artifacts are citable
for the release and which suites are planned coverage only. The manifest is checked against the full
JSON artifacts by `moon run bench-gate-test`.

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

## Reporting Rules

- Lead with `bench-live` when describing Sibyl’s current runtime behavior.
- Treat `bench-live-smoke` as a guardrail, not as headline benchmark evidence.
- Treat `core:bench-context` as a blocking context-quality check for retrieval and policy changes.
- Treat offline baselines as directional. Do not present them as production latency or runtime
  quality claims.
- Keep the artifact JSON from `bench-live` whenever you cite a number in docs or PRs.
- For AI memory benchmark and competitor claims, keep full raw artifacts plus overall metrics,
  per-slice metrics, corpus or dataset version, command, commit, runtime mode, and caveats.
- If the live stack or auth context is unavailable, say so explicitly instead of substituting an
  offline result.

## AI Memory Benchmark Result Records

External AI memory benchmarks live on a stricter evidence track than local smoke checks. Any LOCOMO,
RULER, Mem0, Zep, LangMem, or similar result that appears in public docs must have a full result
record, not just a headline score.

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
- ingestion time, query latency, timeout count, error count, and skipped-case count when available
- competitor version, hosted/self-hosted mode, ingestion path, and tuning when the result compares
  against another memory product
- claim boundary: what the result supports and what stays unproven

`moon run bench-gate` with no report argument gates the committed
`benchmarks/results/ai-memory/manifest.json` ledger and every citable artifact it names. Use
`moon run bench-gate -- <artifact>.json --profile ai-memory` for a single uncommitted artifact.

The canonical ledger for which rows are citable is
`docs/_archive/SURREALDB_GRAPHITI_EXIT_BENCHMARK_EVIDENCE.md`. If a benchmark suite is missing
from that ledger, add it there before citing the result anywhere else.

## Suggested PR Notes

- Runtime evidence: artifact path from `benchmarks/results/`
- Smoke evidence: `moon run bench-live-smoke`
- Offline evidence, if relevant: `moon run bench-retrieval` or `longmemeval_bench.py`

## Store Comparison Flow

When you need to compare legacy and Surreal on the same graph data:

1. Export a manifest archive from the source store with
   `sibyld migrate export --org-id <org> --output /tmp/migration.tar.gz`
2. Rehearse the import path on the target store with
   `moon run migrate-rehearse -- /tmp/migration.tar.gz --source-type legacy-archive --target-mode postgres-rehearsal --yes --restore-database-dump`
3. Run `moon run bench-live -- --label <store> --metadata store=<store>`
4. Compare the saved artifacts with
   `uv run python benchmarks/compare_eval_reports.py <legacy.json> <surreal.json>`

The rehearsal command handles archive validation, optional PostgreSQL restore, graph import, runtime
verification, and the deterministic baseline replay in one pass. Use `--restore-database-dump` with
`--source-type legacy-archive --target-mode postgres-rehearsal` when you want a full
FalkorDB/PostgreSQL migration rehearsal instead of a graph-only replay. That keeps the public story
honest and makes it much easier to compare runs over time.

When you are ready for a real maintenance-window swap, use
`moon run migrate-cutover -- /tmp/migration.tar.gz --source-type legacy-archive --target-mode surreal --yes --write-freeze-confirmed --base-url <surreal-api>`
on the Surreal runtime. That command keeps writes frozen through import, verification, baseline
replay, and any optional live bench checks. Reopening writes is a separate explicit step with
`--reopen-writes --acknowledge-no-instant-rollback`, because instant zero-loss rollback is not
promised after Surreal starts accepting new writes.

Run `moon run chaos-archive -- /tmp/migration.tar.gz` when you want a quick corruption drill for the
archive format itself. The current probe mutates checksums, graph counts, and organization IDs to
make sure the validator rejects obviously bad cutover inputs before a restore window starts.
