# Benchmark Methodology

Sibyl now treats retrieval evaluation as a small ladder instead of one overloaded command. That
keeps local smoke checks, runtime artifacts, and offline baselines from drifting into the same
story.

## Recommended Order

1. `moon run bench-live -- --label legacy --metadata store=legacy`
2. `moon run bench-live-smoke`
3. `moon run bench-retrieval`
4. `uv run python benchmarks/longmemeval_bench.py /path/to/longmemeval.json --mode hybrid`

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

Use this for offline comparison work, and label it clearly as such.

## Reporting Rules

- Lead with `bench-live` when describing Sibyl’s current runtime behavior.
- Treat `bench-live-smoke` as a guardrail, not as headline benchmark evidence.
- Treat offline baselines as directional. Do not present them as production latency or runtime
  quality claims.
- Keep the artifact JSON from `bench-live` whenever you cite a number in docs or PRs.
- If the live stack or auth context is unavailable, say so explicitly instead of substituting an
  offline result.

## Suggested PR Notes

- Runtime evidence: artifact path from `benchmarks/results/`
- Smoke evidence: `moon run bench-live-smoke`
- Offline evidence, if relevant: `moon run bench-retrieval` or `longmemeval_bench.py`

## Store Comparison Flow

When you need to compare legacy and Surreal on the same graph data:

1. Export a manifest archive from the source store with `sibyld migrate export --org-id <org> --output /tmp/migration.tar.gz`
2. Rehearse the import path on the target store with `moon run migrate-rehearse -- /tmp/migration.tar.gz --yes`
3. Run `moon run bench-live -- --label <store> --metadata store=<store>`
4. Compare the saved artifacts with `uv run python benchmarks/compare_eval_reports.py <legacy.json> <surreal.json>`

The rehearsal command handles archive validation, graph import, runtime verification, and the
deterministic baseline replay in one pass. That keeps the public story honest and makes it much
easier to compare runs over time.
