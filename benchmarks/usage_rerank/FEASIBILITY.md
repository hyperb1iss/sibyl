# P5 phase 1: usage-signal extraction and offline rerank feasibility

**Verdict: NO-GO on a production scoring change now. Instrument better, then accumulate, then re-gate.**

The usage loop has produced real labeled data, and the volume is enough to measure with.
It is not enough to ship a ranking change on, and the single most useful arm of the
obvious usage prior is statistically indistinguishable from a meaningless prior of the
same strength. Worse, the curve Sibyl already ships for retention is measurably harmful
when applied to ranking, because its retrieval-count term acts mainly as an item-age
proxy and so demotes exactly the freshly written memories that agents cite. The blocker
is instrumentation, not patience: the query that produced an exposure is not recoverable
from any stored field, so no query-conditioned reranker can be trained or even evaluated
offline against this data.

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
exposed, and 12 fall outside the window. The median exposure-to-citation gap is 425
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
(`tools/usage_exposure.py:174` and `:207`), so every raw event in a session precedes every
graph event no matter how the two were actually interleaved on the page. 631 of 923
sessions are mixed-kind, and in every one of them the kinds form contiguous timestamp
blocks, which is the fingerprint of the two-call batching. Global rank is not
reconstructable for those sessions, so all ranking analysis here is scoped within a kind.
This is an implementation artifact rather than a contract, which is why the harness
audits it on every run instead of assuming it.

## The offline what-if

124 sessions are contrastive, meaning they served both an eventually cited item and at
least one item nobody cited. Those yield 187 cited items to study against 1,020 uncited
candidates, averaging about ten candidates per session. Baseline MRR of the cited item
under the served order is 0.447, and the cited item is already at rank 1 in 45 of 187
cases, so roughly a quarter of the population has no headroom to buy at all.

The what-if multiplies a proxy fused score, `rrf_score` of the recovered rank, by a
point-in-time usage prior shaped after the shipped `usage_retention_multiplier`
(`retrieval/temporal.py:196-205`). Counts are strictly causal: every count for an item at
session S comes only from events before S began, so the citation being predicted can
never feed the score that promotes it. A permutation null shuffles the multipliers a
session actually earned among that session's own candidates, which preserves the strength
of the reweighting and destroys only its association with the item, giving a noise floor
for the MRR delta.

| Arm | retrieval w | citation w | mean rank delta | MRR delta | verdict |
| --- | --- | --- | --- | --- | --- |
| citation_only | 0.00 | 0.12 | +0.027 | +0.0035 | indistinguishable from noise |
| production_retention_shape | 0.02 | 0.12 | -1.027 | -0.1432 | harms above noise |
| retrieval_heavy | 0.06 | 0.12 | -1.171 | -0.1551 | harms above noise |
| citation_heavy | 0.02 | 0.36 | -0.861 | -0.1280 | harms above noise |

The permutation null over 200 trials has mean -0.0602, standard deviation 0.0200, and a
95th percentile absolute MRR delta of 0.0914. Restricting to interactive-only sessions
moves nothing of consequence (185 cited items, MRR delta +0.0036 on the citation-only
arm), so contamination is not driving any of this.

## Why the production curve hurts: retrieval_count is mostly an age proxy

Applying the retention curve to ranking is not merely unhelpful, it lands 4.1 standard
deviations below a random prior of the same strength. The retrieval-count term is the
cause, and the reason it is harmful turns out to be age rather than genericness.

The raw gap looks dramatic. Among candidates in contrastive sessions, items nobody cited
carry a mean of 19.5 prior exposures against 7.9 for the cited ones, so a bonus on
retrieval count systematically promotes the wrong candidates. Reading that as "heavily
retrieved memories are generic hubs" would be over-reading it, and the age columns are
what settle the question, because retrieval count also grows simply with how long an item
has existed.

| Candidate group | n | prior exposures (mean) | history days (median) | exposures/day (median) | prior exposures if history > 5d |
| --- | --- | --- | --- | --- | --- |
| cited | 187 | 7.93 | 0.84 | 2.85 | 23.76 (n=25) |
| uncited | 1,020 | 19.50 | 3.34 | 2.93 | 31.11 (n=353) |

Cited candidates are dramatically younger: their median observable history is 0.84 days
against 3.34, and 55.1% of them had half a day of history or less against 24.4% of
uncited candidates. Once age is divided out the difference nearly vanishes, with median
exposure rates of 2.85 and 2.93 per day, and restricting to items with more than five
days of history shrinks the count gap from 2.5x to 1.3x. So the honest reading is that a
raw retrieval-count bonus works as an age penalty, and it demotes exactly the freshly
written memories that agents are citing. What the citation signal mostly tracks in this
window is recency.

That has a direct consequence for phase 2: an age-normalized exposure rate is the term
worth testing, not the raw count, and the recency effect should be modelled explicitly
rather than arriving as a side effect of a counter. Note also that `first_seen_at` uses
the earliest usage event as its age proxy, since creation time lives in the graph rather
than the event table, so items predating usage recording are censored and their history
is understated.

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
project rather than the request's (`tools/usage_exposure.py:489-494`). The harness
therefore reports a two-sided bound from burst detection: 891 sessions are the clean
lower bound and 32 are burst-suspect, so contamination is at most 3.5% of sessions.
Interactive agent work can also burst, so treat 32 as a ceiling rather than an estimate.

## Instrumentation gaps that block better labels

These are named for a later lane. **Nothing in this list was changed here**, per the
lane's no-production-edits constraint.

1. **Query text is not persisted, and this is the blocking gap.** Both emitters already
   receive the query: `search` passes `"query": query` and `context_pack` passes
   `"goal": goal` into `request_metadata` (`tools/search.py:1315`,
   `tools/context.py:1608`). It is consumed only to build a digest and then dropped. The
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

**GO** requires the best arm to beat the served baseline by an MRR delta of at least
+0.05 *and* to exceed the permutation null's 95th percentile absolute delta on the same
run, evaluated on interactive-only sessions with a point-in-time prior, and to hold on a
held-out time split (train on the first 70% of the window, evaluate on the last 30%).

**NO-GO** if the best arm's MRR delta is below +0.02, or falls inside the noise floor, or
fails to reproduce on the held-out split. A sub-noise delta is NO-GO by default, and this
lane's +0.0035 against a 0.0914 floor is exactly that.

**Kill the usage-prior direction entirely** if, with 750 or more positives, neither the
citation-only arm nor the age-normalized arms clear +0.02. That would say the usage loop's
value is retention and forgetting, where it already demonstrably works, and not ranking. A
query-conditioned reranker trained on the recovered labels would then be the direction to
test instead, which is precisely what instrumentation gap 1 unblocks.

## Reproducing

```bash
uv run python benchmarks/usage_rerank/extract.py          # read-only, writes out/
uv run python benchmarks/usage_rerank/whatif.py           # all sessions
uv run python benchmarks/usage_rerank/whatif.py --interactive-only
uv run pytest tools/tests/test_usage_rerank_harness.py -q # 63 tests, no live store
```
