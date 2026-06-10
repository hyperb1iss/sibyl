# AI Memory Benchmark Results

This directory is the citable result namespace for external AI memory benchmark artifacts. Local
result files should be full records, not headline summaries. Oversized artifacts may use committed
external archive manifests that record the archive location, digest, gate receipt, and exact summary
fields.

Current committed artifacts:

- `manifest.json`
- `longmemeval_sibyl_raw_20260513.json`
- `longmemeval_sibyl_hybrid_20260513.json`
- `longmemeval_sibyl_raw_rc1_20260610.json`
- `longmemeval_sibyl_hybrid_rc1_20260610.json`
- `external/longmemeval_sibyl_live_full_26304777971.json`

`manifest.json` is the release ledger. Entries under `citable` must point to full artifacts in this
directory or committed external archive manifests, and pass the `ai-memory` gate. Entries under
`no_regression` compare local citable artifacts against named baselines and fail the default
`bench-gate` when a tracked metric drops. Entries under `planned` are not release-note evidence yet.

Each citable local artifact must include overall metrics, per-slice metrics, full per-case records,
dataset provenance, command, commit, runtime mode, and caveats. External archive manifests must
include the artifact digest, size, archive retrieval details, expiry, verification receipt, gate
receipt, and exact summary fields. Runtime metadata must name retrieval mode, embedding provider,
model, dimensions, tokenizer method, repeat count, auth manifest ID, and corpus hash. Gate new
artifacts before citing them:

```bash
moon run bench-gate -- benchmarks/results/ai-memory/<artifact>.json --profile ai-memory
```

Run `moon run bench-gate` without arguments to validate the committed manifest and every citable
artifact or external archive manifest it names, including no-regression baseline checks.

Suites without full artifacts in this directory or in a named external archive manifest are planned
coverage only.
