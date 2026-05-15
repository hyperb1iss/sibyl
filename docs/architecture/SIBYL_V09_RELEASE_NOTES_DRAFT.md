# Sibyl v0.9 Release Notes Draft

Status: HOLD. This draft is not publishable until CI, docs deploy, and nightly regression are green
on the final v0.9 candidate head.

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
  synthesis, and source inspection. The old Archive route remains as a hidden compatibility wrapper
  around raw capture review.

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
- `moon run web:test` -> 25 files passed, 101 tests passed.
- `moon run docs:lint` -> all matched files use Prettier code style.
- `moon run :check` -> 40 tasks completed, 26 cache hits before the receipt docs were written;
  post-doc rerun completed 36 tasks with 33 cache hits.

## Release Hold

The local branch is ahead of `origin/main`; no GitHub CI, docs deploy, or nightly regression receipt
covers the local v0.9 candidate yet. Ship only after those receipts are green on the exact candidate
head.
