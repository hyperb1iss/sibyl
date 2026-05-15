# Sibyl v0.9 Release Notes

Status: released as `v0.9.0`.

- Release URL: <https://github.com/hyperb1iss/sibyl/releases/tag/v0.9.0>
- Published: 2026-05-15 07:05:58 UTC
- Release branch target: `main`
- Released commit: `64c3c838`

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
- The web Memory workspace is now the primary product surface for review, captures, imports,
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

## Release Receipt

`v0.9.0` is published. Treat this document as the release receipt and claim boundary, not an active
hold notice.

Post-v0.9 planning moved to [`SIBYL_1_0_ROADMAP.md`](SIBYL_1_0_ROADMAP.md), which promotes the
manual review and workspace work into the larger 1.0 automatic memory operating-system plan.
