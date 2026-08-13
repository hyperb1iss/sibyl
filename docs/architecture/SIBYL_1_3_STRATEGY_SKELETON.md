# Sibyl v1.3 Strategy Skeleton — the benchmark chase, re-armed

- Status: skeleton (seeded 2026-08-13 from the completed v1.2 Track A adjudication; not yet a locked
  plan)
- Inherits: the benchmark-beat package moved whole from v1.2 by
  [`SIBYL_1_2_IMPLEMENTATION_PLAN.md`](SIBYL_1_2_IMPLEMENTATION_PLAN.md) §7 (decision
  `e180be76037b`): `baseline-beat-gate` (combined full-451 ≥ 42.8% at ≤ 10s avg, submission on
  pass), the exposure/state-recall serving targets, and the latency budget work.
- Anchor: stage-2 combined **30.38%** (enterprise 31.60%, web 29.31%), 3-pass paired, decision
  `95677ae0b87b`. Gap to the naive-RAG frontier: ~12.4pp.

## 1. What v1.2 proved

Every serving-flag lever class is adjudicated dead with receipts (§2.4 of the v1.2 plan):
composition additive stitch (reader-level NO-GO), ranking rescue and entity-overlap ordering (miss
set is pool-boundary limited), slice-serving geometry (evidence slices materialize at ~6K chars, so
reduced budgets pack fewer units), and bounded agentic traversal at shipped defaults (coverage −4,
latency +28s; sufficiency stop never fired). The one survivor is the typed-overfetch KNN fast path
(merged, #371): pack parity with the type-filtered tail halved — an efficiency lever that makes
wider pools affordable.

The consequence that shapes v1.3: **the remaining upside lives at ingest time, not at serving
flags.** Selection reaches an 82.6% phrase-presence ceiling while rendering exposes at most 65.2%,
and the oracle format ablation (slices+notes 82.5% vs raw 59.6%, ~23pp) still stands un-cashed.

## 2. Levers, ranked by expected value

1. **Sub-1K slice granularity at ingest (the big bet).** The corpus rebuild that makes the oracle
   receipt expressible at serving. The geometry arms proved 6K-char slices cannot pack precision
   into any budget; finer spans change what packs can hold. Ceiling attached: >10pp if live
   selection converts even half the oracle gap.
2. **Candidate acquisition over the overfetch fast path.** The miss set is pool-absence, not
   rank-burial (decision `715454c60c9b`), so the recall budget goes to wider pools and additional
   acquisition lanes, now affordable via #371.
3. **Retrieval-key declaration at ingest + a lexical arm for identifier queries.** Verbatim fulltext
   beats dense-alone +10pp at k=10 on identifier shapes; the exact-key lane is structurally inert on
   LME corpora because `/memory/experience` never declares keys. Rides the same corpus rebuild as
   lever 1.
4. **Premise/gotcha notes.** Note distillation is the only lever with a stable positive receipt
   (+3.7pp/domain across four gates, +8.15pp interaction). The errors-gotchas pool is 67–71%
   confidently wrong because premises are never checked; premise-checking notes attack it directly.
   Rides the same ingest pass.
5. **Reader-side conversion.** The reader converts only 50–54% when the gold literal IS exposed.
   Prompt and rendering work, screened for free with replay.
6. **Latency as a scoring lever.** Enterprise ~32s clean-server against the 10s budget; LAFS pays
   only left of the frontier. Finer packs (lever 1) and the overfetch path attack it from both
   sides.

## 3. Parked levers and their revival conditions

- **Agentic traversal**: revive only with a sufficiency stop that actually discriminates (0 early
  stops in 211 is a broken stop, not a broken idea), displacement-safe admission, and after the
  latency budget work. Route multi-hop questions only; never force the loop.
- **Re-ranking / cross-encoder**: refuted conditional on pool-absence. Becomes relevant again
  exactly when lever 2 widens pools enough that gold enters them rank-buried.
- Everything in the v1.2 killed-lever ledger stays killed absent new mechanism evidence.

## 4. Opening move

Levers 1 + 3 + 4 share one corpus rebuild, so the opening slice is a single ingest-side
re-architecture (sub-1K spans, key declaration, premise notes) followed by the standard unpaid
screen ladder against a fresh anchor. Protocol law from the v1.2 plan carries forward unchanged
(replay-before-paid, 3-pass paired gates, pre-registered numbers, same-commit pairing, jitter
floors, receipt-checked arm activity), and the measurement rig that enforced it is built and
battle-tested.

## 5. Receipts index

Decisions: `95677ae0b87b` (anchor), `715454c60c9b` (ranking + identifier flank),
`14686f9a4056`/`e180be76037b` (geometry + re-scope), `106f71e196e8` (overfetch GO), `8b69af3de865`
(A3 pre-registration and screen verdict), error pattern `e69420d130be` (silent arm-off class, fixed
in #373). Screen artifacts under `.moon/cache/evals/` (`a2-slice-*`, `a1-stage2-ent-screen-*`).
