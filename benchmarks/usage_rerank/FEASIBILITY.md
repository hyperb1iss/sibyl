# P5 phase 1: usage-signal extraction and offline rerank feasibility

**Verdict: NO-GO on a production scoring change now. Instrument better, then accumulate, then re-gate.**

The usage loop has produced real labeled data, and the volume is enough to measure with.
It is not enough to ship a ranking change on: the single most useful arm of the obvious
usage prior cannot be distinguished from no change at all, with a paired-bootstrap
interval on its MRR delta of [-0.037, +0.043] straddling zero. Worse, the curve Sibyl
already ships for retention is measurably harmful when applied to ranking, because its
retrieval-count term both penalizes freshly written memories and rewards a signal that is
genuinely anti-correlated with citation. The blocker is instrumentation, not patience: the
query that produced an exposure is not recoverable from any stored field, so no
query-conditioned reranker can be trained or even evaluated offline against this data.

All numbers below come from a read-only extraction against the local dev store on
2026-08-03, reproduced by `extract.py` and `whatif.py`. Receipts are committed in `out/`.

## What exists

| Measure | Value |
| --- | --- |
| Total events | 14,740 |
| Exposure events | 14,523 (context_pack 7,560, search 6,963) |
| Citation events | 211 (cli_cite 199, task_complete 12) |
| Misled events | 6 (cli_cite_misled) |
| Time span | 27.5 days (2026-07-07 to 2026-08-03) |
| Distinct exposed items | 2,470 |
| Distinct cited items | 154, of which 151 were also exposed |
| Exposure sessions (one served page each) | 923 |
| Organizations / principals | 1 / 1 |

One caveat on every count in that table: an exposure event is deduplicated on
`(organization_id, session_key, message_key, source_surface, item_kind, item_id,
signal_type)` by a unique index (`content_schema.py:425-427`) written through
`INSERT ... ON DUPLICATE KEY UPDATE` (`services/usage.py:66-81`), and the session key is a
digest with no timestamp in it (`tools/usage_exposure.py:468-486`). A byte-identical
request returning a byte-identical id list therefore writes no new row. So 923 counts
distinct served pages rather than serve events, and 14,523 counts distinct item exposures
rather than times an item was shown. This is not a harness artifact: production's own
`retrieval_count` is `array::len($exposure_events)` over the same table
(`services/usage.py:129`), so the harness and the shipped counters agree by construction.

The citation rate is 1.45% of exposures, and 6.1% of exposed items were cited at least
once. That is sparse but not empty, and it is genuinely labeled: 193 positive labels
attach to a specific served page rather than to an item in the abstract.

## The join key is dead, and item-plus-time replaces it

The natural join, matching an exposure to its feedback on `(session_key, message_key)`,
returns nothing at all. Across 923 exposure session keys and 118 feedback session keys
the intersection is exactly zero, and it is zero by construction rather than by accident.
Both keys are sha256 digests, but the citation digest folds `cited_ids` into its payload
(`packages/python/sibyl-core/src/sibyl_core/tools/usage_citation.py:311-322`) while the
exposure digest does not
(`packages/python/sibyl-core/src/sibyl_core/tools/usage_exposure.py:476-486`), and the
two families draw from disjoint `source_surface` prefixes. No amount of data will make
this join match.

Attribution therefore walks item identity and time instead: each feedback event is
credited to the most recent exposure session that served that item. It works well. At a
24 hour window, 200 of 217 feedback events attribute, 5 name an item that was never
exposed, 8 name an item first served only after the feedback, and 4 sit beyond the window.
Splitting the last two matters, because a missing earlier exposure and a stale one are
different failures with different fixes. The median exposure-to-citation gap is 425
seconds, about seven minutes, which is a very plausible agent turnaround. The window is
not doing the work either: the sweep runs 81 attributed at five minutes, 182 at one hour,
197 at six hours, 200 at 24 hours, and 201 at seven days, so it saturates well before the
chosen default.

## Rank is recoverable, but only within an item kind

The served rank is not a column, and no exposure event carries a rank or score in
metadata (0 of 14,523 for both). It is nevertheless recoverable, because
`record_memory_usage` stamps each event with its own `datetime.now(UTC)`
(`services/usage.py:245`) and the emitter builds the event list in served order
(`tools/usage_exposure.py:366-383`). Timestamps therefore increase monotonically with
rank at microsecond granularity, and the audit confirms it holds in all 923 sessions with
no ties.

The recovery stops at the kind boundary. The emitter records raw_capture targets and
graph_entity targets in two separate `record_memory_usage` calls
(`tools/usage_exposure.py:174` and `:207`), so a raw event in a session normally precedes
every graph event no matter how the two were interleaved on the page. 631 of 923 sessions
are mixed-kind, and in 628 of them the kinds form contiguous timestamp blocks, which is
the fingerprint of the two-call batching. Global rank is not reconstructable for those
sessions, so all ranking analysis here is scoped within a kind.

The remaining 3 sessions matter more than their count suggests, because they are the
counterexample to the assumption the whole rank recovery rests on.
`context_pack:a8ec3ad1e742c79e9979f705`, `context_pack:c73de246a5b9c688dd2741c5`, and
`context_pack:d11cb242c2766405eccb63de` show kinds alternating rather than blocked, so
their writes interleaved and their timestamp order is not a served order. One of the three
is contrastive and would otherwise contribute a cited item, so `run_whatif` drops any
session without contiguous kind blocks and the population is 186 cited items rather than
187. Rank recovery is an implementation artifact rather than a contract, which is exactly
why the harness audits it on every run and names the exceptions instead of assuming
uniformity.

## The offline what-if

124 sessions are contrastive, meaning they served both an eventually cited item and at
least one item nobody cited. Dropping the interleaved sessions leaves 119 of them, and
because ranks are only comparable inside one item kind, those 119 sessions decompose into
133 independently ranked candidate lists. Together they carry 186 cited items against
1,013 uncited candidates, averaging about ten candidates per list. Baseline MRR of the
cited item under the served order is 0.448, and the cited item already sits at rank 1 in
45 of 186 cases, so roughly a quarter of the population has no headroom to buy at all.

The what-if multiplies a proxy fused score, `rrf_score` of the recovered rank, by a
point-in-time usage prior shaped after the shipped `usage_retention_multiplier`
(`retrieval/temporal.py:196-205`). Counts are strictly causal: every count for an item at
session S comes only from events before S began, so the citation being predicted can
never feed the score that promotes it. Sessions whose item kinds interleave are dropped,
since their recovered ranks are not a served order.

| Arm | retrieval w | citation w | mean rank delta | MRR delta | 95% CI vs zero | verdict |
| --- | --- | --- | --- | --- | --- | --- |
| citation_only | 0.00 | 0.12 | +0.032 | +0.0037 | [-0.0372, +0.0435] | indistinguishable from zero |
| production_retention_shape | 0.02 | 0.12 | -1.032 | -0.1439 | [-0.1908, -0.0980] | harms the baseline |
| retrieval_heavy | 0.06 | 0.12 | -1.177 | -0.1559 | [-0.2039, -0.1097] | harms the baseline |
| citation_heavy | 0.02 | 0.36 | -0.866 | -0.1287 | [-0.1781, -0.0809] | harms the baseline |

Two different questions are at stake here and they need two different tests. "Did this
beat the served baseline" is a claim about zero, so each arm carries a paired bootstrap
over its per-item reciprocal-rank changes: `citation_only` lands at +0.0037 with a 95%
interval of [-0.0372, +0.0435], and 44.1% of resamples fall at or below zero, so it is
indistinguishable from no change. The three harmful arms have intervals entirely below
zero, so their harm is real.

"Did the prior beat a coin flip of the same strength" is a different question, answered by
the permutation null: mean -0.0592, standard deviation 0.0179, 95th percentile absolute
delta 0.0873. That distribution sits well below zero because any reordering degrades an
already-good baseline, so it must not be read as a two-sided floor around zero. Against
it, `citation_only` sits 3.5 standard deviations high, which means the citation signal
does carry real information, and it still fails to beat current fusion. Both statements
hold at once, and only the first one bears on whether to ship anything.

Restricting to interactive-only sessions moves nothing of consequence, so contamination is
not driving any of this.

## Why the production curve hurts: two effects, not one

Applying the retention curve to ranking is not merely unhelpful, it lands 4.7 standard
deviations below a random prior of the same strength. The retrieval-count term is the
cause, and it is harmful for two separate reasons that a single statistic cannot
separate. Both need stating, because each implies a different fix.

Among candidates in contrastive sessions, items nobody cited carry a mean of 19.4 prior
exposures against 8.0 for the cited ones, a raw ratio of 2.43. Two things drive that gap.

**Age is the larger part, and it is large.** True creation timestamps, read from
`entity.created_at` in the org graph namespace and `raw_captures.created_at` in the
content namespace, put the median cited candidate at 1.24 days old against 7.75 days for
uncited ones. So a raw exposure count is substantially a measure of how long an item has
been available to be exposed, and a bonus on it penalizes new memories.

**A roughly 2x anti-correlation survives once age is held fixed.** Standardizing the
uncited group onto the cited group's true-age distribution, band by band, brings its mean
exposure count from 19.45 down to 17.43 against 8.01 for cited candidates, a residual
ratio of 2.18. The gap narrows from 2.43 to 2.18 and does not vanish. It is present in
every age band:

| True age at serve | cited mean (n) | uncited mean (n) | ratio |
| --- | --- | --- | --- |
| 0 to 0.5 days | 2.86 (62) | 7.53 (92) | 2.64x |
| 0.5 to 1 day | 8.16 (25) | 28.11 (71) | 3.45x |
| 1 to 3 days | 7.23 (31) | 23.36 (148) | 3.23x |
| 3 to 7 days | 12.96 (23) | 26.63 (174) | 2.06x |
| 7 to 14 days | 16.52 (21) | 18.88 (121) | 1.14x |
| 14 to 30 days | 8.63 (8) | 8.69 (26) | 1.01x |
| over 30 days | 10.87 (15) | 16.83 (380) | 1.55x |

So heavily exposed items really are less likely to be the cited one, independently of
age, and the effect is strongest in the first three days. Both readings hold: a raw
retrieval-count bonus acts as an age penalty on new memories *and* rewards a genuinely
anti-correlated signal.

Phase 2 follows from that. An age-normalized rate is worth testing but is not sufficient
on its own, since standardization shows a residual the rate would miss, and recency
should be modelled explicitly rather than smuggled in through a counter.

Two measurement cautions belong with these numbers. The `exposures_per_day` columns in
the receipt are the weakest statistic here and should not be read alone: the rate is
undefined for a candidate with almost no history, so it retains 44.6% of cited candidates
against 75.5% of uncited ones, discarding exactly the fresh candidates where the effect
concentrates. Direct standardization is reported because it controls for age without
dropping anybody. Separately, the `first_seen_at` age proxy is asymmetrically censored and
is not fit for this comparison: it understates age for 9.5% of uncited candidates against
4.3% of cited ones, compressing exactly the long histories a control has to see. True
`created_at` is used wherever it resolves, which covers 185 of 186 cited and 1,012 of
1,013 uncited candidates.

Using `created_at` as an age source needs its own justification, because the entity upsert
assigns it unconditionally (`services/graph.py:245`) rather than preserving an existing
value the way the adjacent `created_by` does, and 7,561 of 10,926 entities in this store
carry a revision above 1, so most rows have been rewritten at least once. The check that
licenses it is that an item cannot be served before it exists: a `created_at` later than
the item's first usage event would prove the timestamp had drifted forward. Across all
1,197 resolved candidates there are zero such violations, so rewrites are preserving the
original value in practice. `age_source_integrity` in the what-if receipt reports this on
every run, and a nonzero count there invalidates every age number above.

The citation term points the correct way but is thin. 27.8% of cited candidates carry a
prior citation against 17.7% of uncited ones, a real signal (the citation-only arm sits
3.5 standard deviations above the random-prior null) that is nonetheless far too weak to
improve on current fusion. This is the crux: only 136 of 186 cited items had any prior
usage signal at all, so 50 had none whatsoever. A usage prior cannot help an item it has
never seen used.

The citation term points the correct way but is thin. 27.8% of cited candidates carry a
prior citation against 17.7% of uncited ones, a real signal (the citation-only arm sits
3.2 standard deviations above the random-prior null) that is nonetheless far too weak to
improve on current fusion. This is the crux: only 52 of 187 cited items had any prior
citation at all, and 49 had no prior usage signal whatsoever. A usage prior cannot help
an item it has never seen used.

## Contamination

`context_pack_eval` writes exposure rows into the store it measures (Sibyl task
1592d234), and the mechanism is confirmed: the eval posts to `/context/pack` without
`record_exposure: false` (`sibyl_core/evals/runtime.py:302`) against a schema whose
default is `True` (`apps/api/src/sibyl/api/schemas/context.py:82`).

Exact separation is impossible, because no column marks an event as benchmark-origin and
the eval reuses the `context_pack` surface with the same organization and principal as
real work. `project_id` cannot rescue it either, since the event carries the *item's*
project rather than the request's (`tools/usage_exposure.py:489-494`).

Burst detection flags 32 of 923 sessions, and that number is a detection count rather than
a bound in either direction. It can over-count, because interactive agent work also
bursts. It can equally under-count: `flag_eval_suspect_sessions` buckets on
`(source_surface, item_count, minute)` and needs six members, so a sweep that varies pack
size, runs slower than six a minute, or straddles a bucket edge is invisible. Dropping
`item_count` from the key alone lifts the count from 32 to 38, and 135 further sessions sit
in sub-threshold buckets of two to five. No honest ceiling is available from this schema,
which is the strongest argument for the provenance flag in gap 3 below. What can be said
is that the what-if result is unchanged when the flagged sessions are excluded, so
whatever the true contamination is, it is not carrying the verdict.

## Instrumentation gaps that block better labels

These are named for a later lane. **Nothing in this list was changed here**, per the
lane's no-production-edits constraint.

1. **Query text is not persisted, and this is the blocking gap.** Both emitters already
   receive the query: `search` passes `"query": query` and `context_pack` passes
   `"goal": goal` into `request_metadata` (`tools/search.py:1315`,
   `tools/context.py:1609`). It is consumed only to build a digest and then dropped. The
   fix site is the metadata dict at
   `packages/python/sibyl-core/src/sibyl_core/tools/usage_exposure.py:377-380`, where
   `request_metadata` is already in scope one frame up (`:102`). Until this lands, 0% of
   exposures are query-recoverable and no query-conditioned reranker is trainable or
   offline-evaluable.
2. **Rank and score are not persisted.** Recovering rank from timestamp ordering works
   today but is an artifact that any batching change would silently break, and it cannot
   cross item kinds at all. A `rank` and `fused_score` on `_ExposureTarget`
   (`tools/usage_exposure.py:32-38`), populated where the emitter enumerates results,
   would make it a contract and restore global ordering.
3. **No provenance flag distinguishes benchmark traffic.** Adding an origin marker to the
   event, alongside making the eval send `record_exposure=False`
   (`sibyl_core/evals/runtime.py:302`, Sibyl task 1592d234), would replace the burst
   heuristic with an exact filter.
4. **Feedback is single-surface and thin.** 199 of 211 citations come from `cli_cite`,
   so the label distribution reflects one human's explicit citing habit rather than
   broad agent behaviour. Widening citation capture matters more for label quality than
   any weight tuning.

## Proposed next gate, pre-registered

Phase 2 should be instrumentation plus accumulation, not tuning. The gate below is
written before the data exists, per campaign protocol law, and the harness in this
directory computes every number in it.

**Preconditions before the gate may be evaluated at all.** Query text recoverable on at
least 95% of exposure events; rank persisted as a column, so `global_rank_recoverable` is
true; eval-origin events exactly excludable rather than burst-bounded; at least 750
attributed positive labels (roughly 4x today) with at least 250 of them carrying a prior
citation, and citations arriving from at least two distinct surfaces.

**Arms to test.** Carry the four arms here for comparability, and add two the age finding
argues for: an exposure-rate arm using exposures per observable day in place of the raw
count, and a recency arm that models item age directly instead of letting a counter smuggle
it in. `describe_candidate_priors` is what checks whether the age gap has closed.

**GO** requires the best arm to beat the served baseline by an MRR delta of at least +0.05
whose 95% paired-bootstrap interval excludes zero, evaluated on interactive-only sessions
with a point-in-time prior, holding on a held-out time split (train on the first 70% of the
window, evaluate on the last 30%).

The against-zero interval is the gate, not the permutation null's 95th percentile. That
percentile measures distance from a same-strength random prior and stays near 0.06 to 0.09
regardless of sample size, because it reflects how much reordering perturbs a good baseline
rather than how much data there is. Requiring an arm to clear it would silently raise the
real bar to roughly +0.09 and could reject a genuine +0.05 win, which is the outcome phase
2 exists to detect. The null stays in the report as a secondary diagnostic: an arm that
beats zero but not the random prior is suspicious, and an arm that beats the random prior
but not zero, which is where `citation_only` sits today, carries information without being
worth shipping.

**NO-GO** if the best arm's MRR delta is below +0.02, or its interval includes zero, or it
fails to reproduce on the held-out split. An interval straddling zero is NO-GO by default,
and this lane's +0.0037 at [-0.0372, +0.0435] is exactly that.

**Kill the usage-prior direction entirely** if, with 750 or more positives, neither the
citation-only arm nor the age-normalized arms clear +0.02. That would say the usage loop's
value is retention and forgetting, where it already demonstrably works, and not ranking. A
query-conditioned reranker trained on the recovered labels would then be the direction to
test instead, which is precisely what instrumentation gap 1 unblocks.

## Reproducing

```bash
uv run python benchmarks/usage_rerank/extract.py          # read-only, writes out/
uv run python benchmarks/usage_rerank/whatif.py           # all sessions
uv run python benchmarks/usage_rerank/whatif.py --no-true-age  # skip the created_at lookup
uv run python benchmarks/usage_rerank/whatif.py --interactive-only
uv run pytest tools/tests/test_usage_rerank_harness.py -q # 63 tests, no live store
```
