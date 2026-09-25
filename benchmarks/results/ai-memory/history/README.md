# AI Memory History

This directory stores immutable summary records for v1.1 AI-memory gates.

Each file should use `sibyl-ai-memory-history-summary-v1`, point back to the
full artifact or external artifact manifest it summarizes, and preserve the
exact gate result used as the previous-run baseline for later regression checks.

`2026-07-03-live-hybrid.json` (baseline key `latest-citable-hybrid`) summarizes
the pre-1.0 LongMemEval-S live run whose headline was withdrawn. The file stays
under the immutable append policy as history, but its source is no longer
citable, so `recall@5` has no citable baseline until a new live run is promoted.
