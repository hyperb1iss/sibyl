# Sibyl v0.9 Release Notes Draft

Status: HOLD. The candidate has been rebased on the v0.8.1 release bump and is not publishable until
it is pushed to `main` and docs deploy is green on the final v0.9 candidate head.

## Highlights

- Source-grounded synthesis can plan, verify, draft, and remember Markdown or JSON artifacts from
  authorized memory. Sections carry source IDs, hidden-source signals, unresolved gaps, and
  provenance back to the memories that supported the output.
- Memory is inspectable and correctable. Source inspection shows raw source metadata, derived
  records, visibility, audit receipts, freshness, and correction history. Correction actions support
  preview before apply.
- Source import is source-preserving. The adapter contract and mailbox import path prove private
  defaults, stable dedupe keys, resumable checkpoints, skipped-record accounting, and progress
  visibility.
- The web Memory cockpit is now the primary product surface for review, captures, imports,
  synthesis, and source inspection. The old Archive route redirects into Memory Captures so legacy
  links land in the unified flow.

## Trust Boundary

- Synthesis claims are gated by `moon run synthesis-gate`.
- Source-ingest claims are gated by `moon run adapter-ingest-gate`.
- Existing memory-policy claims remain gated by `moon run memory-trust-gate`.
- Benchmark claims remain limited to artifacts accepted by `moon run bench-gate`.

## Local Evidence

- `moon run memory-trust-gate` -> PASS, 7 checks and 0 failed.
- `moon run synthesis-gate` -> PASS, 2 checks and 0 failed.
- `moon run adapter-ingest-gate` -> PASS, 2 checks and 0 failed.
- `moon run bench-gate` -> Gate passed for `benchmarks/results/ai-memory/manifest.json`.
- `moon run core:test` -> 932 passed, 14 skipped, 20 deselected.
- `moon run api:test` -> 1467 passed, 1 skipped, 16 deselected.
- `moon run cli:test` -> 174 passed.
- `moon run web:test` -> 26 files passed, 102 tests passed.
- `moon run web:test-cov` -> 26 files passed, 102 tests passed after the synthesis runner coverage
  test was stabilized.
- Post-unified-UX update: `moon run web:lint web:typecheck web:test` -> 3 tasks completed, 102 tests
  passed.
- `moon run docs:lint` -> all matched files use Prettier code style.
- `moon run :check` -> 40 tasks completed, 26 cache hits before the receipt docs were written;
  post-doc rerun completed 36 tasks with 33 cache hits.

## GitHub Evidence

- CI-only PR #7 (`codex/v09-ci`) succeeded on `e05a52c01a183876c4b9247203e329856edc293c`; run
  `25899235827` passed Build, Static Checks, Package Tests, E2E, Storybook, and Detect Changes.
- Nightly Regression run `25899328897` succeeded on `e05a52c01a183876c4b9247203e329856edc293c`.
- Earlier pre-rebase CI-only PR #7 runs succeeded on `e944a1d3a81dc0f1c840a053394d59c9c61bce30` and
  `bc5bf7c33e5459c60819a7fa00880cf39e1cca0e`; Nightly Regression run `25898704879` succeeded on
  `bc5bf7c33e5459c60819a7fa00880cf39e1cca0e`.

## Release Hold

The local branch is ahead of `origin/main`. Ship only after Bliss explicitly approves pushing
`main`, the final main CI run is green, and docs deploy succeeds on the exact candidate head.
