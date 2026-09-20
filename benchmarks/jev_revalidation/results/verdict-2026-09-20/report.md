# Explicit verdicts and the Jev integration decision

Keep Jev disabled by default in the current memory-validation path. Jev remains
useful as a bounded source-support adviser, but these experiments do not qualify
it to replace the full critic or make interactive memory validation reliably fast.
Stop pursuing this verdict format as the current Jev integration strategy: the
combined path is slower and less accurate than the simpler critic with Jev.
Standalone verdict-format value remains inconclusive across fresh and retained
cases; this decision does not reject structured verdicts generally.

The strongest fresh result is the existing assertion-prefix Haiku critic with
live Jev: 29/32 strict semantic passes at a 2.393-second median. Explicit verdicts
with Jev pass 27/32 at 3.400 seconds. Known cost per validation is approximately
$0.0038 for prefix plus Jev and $0.0062 for verdicts plus Jev (62.51% more). The positive adviser
signal is real within this cohort. Retained harms, wrong-hint failures and a
30-second Jev deadline prevent default activation. No product runtime changes.

## Fresh confirmation

Sixteen new synthetic cases form eight minimal pairs, repeated twice in six arms.
The prefix baseline is the fixed instruction intervention from the preceding
assertion study, not that study's original critic. Direct versus direct compares
schema plus instructions. Each live arm independently acquires Jev labels.

| Path | Whole-output passes | Consumer-output passes | Valid concerns | Median | p95 | Known cost |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Prefix critic | 22/32 | 22/32 | 14/16 | 2.534s | 7.174s | $0.126257 |
| Prefix + live Jev | 29/32 | 29/32 | 15/16 | 2.393s | 3.091s | $0.121305 |
| Prefix + wrong hints | 14/32 | 14/32 | 7/16 | 2.839s | 4.435s | $0.133720 |
| Explicit verdicts | 26/32 | 26/32 | 14/16 | 3.371s | 5.596s | $0.193004 |
| Verdicts + live Jev | 27/32 | 27/32 | 14/16 | 3.400s | 4.259s | $0.197137 |
| Verdicts + wrong hints | 22/32 | 22/32 | 13/16 | 3.451s | 5.195s | $0.202887 |

Explicit verdicts improve direct passes from 22 to 26, with six paired
improvements and two regressions. Median latency rises 33.06% and cost 52.87%.
Median critic output grows from 267.5 to 470 tokens. The format makes every
assertion's judgment inspectable, but inspection does not make the judgment true.

Adding Jev to the prefix critic produces seven semantic improvements and no
pass-to-fail regression. Fully valid concern coverage rises from 14/16 to 15/16.
A newly missed concern and new defective finding occur on a path that already
failed overall, so the unchanged pass/fail count does not establish no-new-harm.
Median latency falls 5.55% and cost 3.92%; the original 20% median-speed target is
not met. The observed cost difference is only $0.005 over 32 paths; neither
that saving nor the latency difference is established as a stable effect. Small
tail samples do not establish a production latency guarantee. The frozen
mechanical comparison named jev_increment tests verdict direct versus verdict
live, not prefix direct versus prefix live. The latter comparison is a descriptive
analysis from archived arm metrics and independent semantic joins, not a separate
predeclared activation gate.

Adding Jev to explicit verdicts produces three improvements and two regressions,
for a net gain of one pass. Median latency rises 0.87% and cost 2.14%; p95 falls
23.89%. Switching from the prefix-plus-Jev path to verdicts-plus-Jev produces no
semantic improvement and two regressions. The simpler live path is the measured
choice among these experimental variants, with activation still unqualified.

All 128 fresh Jev assertion labels match the frozen fixture, and the two live
contracts receive identical labels on all 32 paired paths. Even correct advice
cannot repair the critic's interpretation reliably. In both repeats of one safe
case, the verdict critic calls a recorded 22-to-12-second comparison an
unsupported causal claim. The assertion names only the measured difference; it
assigns no cause to the plugin or cache change. The source and exact outputs are
preserved in the [independent root spot check](root-source-spot-check.json).

Wrong hints remain a separate robustness test. Verdicts improve stress passes
from 14/32 to 22/32, but the verdict stress arm still creates five regressions
against its direct counterpart and loses two previously covered concerns. Wrong
hints are an authored intervention, not a measured natural Jev error rate.

## Retained diagnostics

The retained cohort contains sixteen already inspected cases in eight pairs,
with one repeat. It combines the prior eight diagnostic cases with four pairs
from the preceding fresh study. Selection was frozen before these new outcomes.
These results measure known failure behavior and are not held-out confirmation.

| Path | Whole-output passes | Consumer-output passes | Valid concerns | Median | p95 | Known cost |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Prefix critic | 11/16 | 11/16 | 8/8 | 2.557s | 4.141s | $0.062104 |
| Prefix + live Jev | 13/16 | 13/16 | 7/8 | 2.184s | 3.303s | $0.059772 |
| Prefix + wrong hints | 6/16 | 6/16 | 4/8 | 3.453s | 5.586s | $0.070964 |
| Explicit verdicts | 7/16 | 9/16 | 7/8 | 3.533s | 5.689s | $0.098675 |
| Verdicts + live Jev | 11/16 | 12/16 | 7/8 | 3.186s | 33.063s | $0.095705 + unknown |
| Verdicts + wrong hints | 6/16 | 7/16 | 6/8 | 3.490s | 5.229s | $0.099450 |

The verdict format produces zero direct improvements and four whole-output
regressions. Two regressions affect consumer-facing output; two additional
failures appear only in the new internal rationale surface. Comparing the two
live paths yields zero improvements and two whole-output regressions, including
one consumer-output regression. The verdict stress arm also falsely accepts one
unsafe candidate. Fresh aggregate gains do not erase these retained failures.

The simpler prefix critic also has a retained Jev harm: live advice produces
three paired improvements and one regression, losing the valid concern on af-008
and adding an overstated finding against its content. Concern coverage drops
from 8/8 to 7/8. That regression survives the contextual alternatives and directly
supports leaving activation off. The same configuration covered that concern in
the preceding study; a single retained repeat cannot establish a stable failure
frequency.

One live Jev dispatch reaches its 30-second deadline before empty-hint fallback.
The complete path takes 33.063 seconds, which is also the retained live-verdict
p95 under this small sample's percentile rule. The episode demonstrates a real
serial latency exposure, not its long-run frequency. The run's unknown Jev bill
prevents claiming complete retained cost.

## Prior results and action outcomes

The preceding assertion study measured the same prefix-plus-Jev configuration at
31/32 fresh semantic passes with 16/16 valid concerns, versus 29/32 and 15/16 here.
The change across small source-family cohorts and repeated model calls cautions
against treating either score as a stable deployment estimate.

Natural classifier errors also exist outside this fresh set. The
[held-out study](../heldout-fast-2026-09-20/report.md) recorded four disagreements
among 84 assertions. The [assertion study](../assertion-2026-09-20/report.md)
recorded 56/60 and 54/58 agreement among completed assertions in its two live
variants, with repeated failures on temperature transfer and insufficiently
precise bounds. The present retained prefix live arm agrees on 29/30 completed
assertions, and verdict live on 27/27 completed (27/30 scheduled). These selected,
dependent cohorts do not justify a pooled production error rate or multiplying
that rate by the deliberately adversarial wrong-hint damage. Perfect agreement
on this fresh fixture is not a universal classifier claim.

Fresh prefix plus Jev accepts 14/16 supported candidates, versus 12/16 direct;
retained acceptance is 6/8 versus 5/8. No fresh arm falsely accepts an unsafe
candidate. Fresh action matches are 27/32 for prefix direct, 30/32 for prefix
live, 30/32 for verdict direct and 29/32 for verdict live. Action agreement alone
cannot detect a wrong finding attached to the right flag decision, so strict
semantic review is the primary comparison. All action metrics remain archived.

## Raw reasoning and consumer output

Every explicit verdict must bind a unique prepared assertion path and hash.
Supported verdicts carry evidence references and a rationale. Concern verdicts
carry source-bound findings. Unable verdicts preserve their reasons as abstention;
existing findings remain present. Missing, repeated or foreign assertions reject
the entire response. Only concern verdicts project into the existing CriticOutput.

The review separately scores the complete raw verdicts and the consumer-facing
projection. A factual error in a supported rationale is a raw-output failure,
not an invented consumer-finding regression. Four retained responses pass the
consumer review but fail raw reasoning. No fresh response has that divergence.
These four divergences are annotation-contingent: all disappear under the
reviewer's frozen contextual alternatives. Original raw output is preserved even
when mechanical validation rejects it.

The reviewer froze five retained and eight fresh contextual alternatives before
seeing arm identities. Applying only those exact alternatives changes retained
prefix direct/live/stress passes to 11/13/6 and verdict passes to 9/12/8. Fresh
prefix passes become 22/30/14 and verdict passes 28/29/23. Primary annotations
remain unchanged. Ten of eleven override-driven pass changes favor verdict arms;
review was contract-visible, and annotation uncertainty concentrates on the
identifiable format intervention. The simpler live path stays strongest by one
pass in each cohort under the alternatives. The narrow Jev-integration choice
holds, while standalone format value remains inconclusive: fresh direct verdicts
reach 28/32 and 16/16 concern coverage, but retained consumer passes remain 9/16
against the prefix baseline's 11/16.
See the separate [retained sensitivity](retained/sensitivity-summary.json) and
[fresh sensitivity](fresh/sensitivity-summary.json), with their exact overrides.

## Failures, spend and reproducibility

The experiment attempted 384 provider calls: 288 critics and 96 Jev requests.
There are 383 unique observed response IDs and one dispatched Jev request without
a response. Known provider-reported spend is $1.460980470 plus that unknown charge.
Fresh spend is completely observed at $0.974310400. Retained known spend is
$0.486670070 plus the unknown amount. Failed calls remain in every denominator.

Four critic outputs fail: one repeated assertion verdict, two arrays emitted as
strings, and one unsupported basis enum. A separate Jev probability distribution
sums to 0.99 and is rejected under the frozen normalization contract. The Jev
deadline and normalization rejection both continue with empty-hint full critique.
No response was repaired or retried. All critic responses finished with tool_calls;
none reported token truncation. The [failure diagnosis](failure-diagnosis.md)
reproduces each rejection and its accounting.

All builds, tests and provider calls ran on stef-gradial-com-main. The complete
benchmark suite passed 425 tests; lint and type checking passed. Six compatibility
probes match the previous assertion runner's default schedules, manifests and
parser behavior. Independent preflight passed 40 selected tests, including four
adversarial probes that root repeated in two separate commands. Both complete
live archives replay exactly without new calls. Independent numerical audits
performed 5,594 retained and 10,671 fresh checks.

The raw timestamps show no overlap between cohorts: the first fresh path starts
0.866 seconds after the final retained path completes. No replay or task-owned
build ran during inference. The devbox is shared, so unrelated host load remains
uncontrolled. Both manifests fix concurrency at eight. The service medians
exclude queue wait (fresh median 34.769 seconds; retained 18.657 seconds), so
2.393 seconds is a path service measurement under concurrent load, not an
interactive end-to-end response time. The deadline event occurred during that
loaded run; its cause cannot be assigned to Jev alone. Measured service time
covers preparation, Jev when applicable,
critique and validation. Retrieval, authorization, publication and interactive
recall are outside that boundary.

## Scope of the conclusion

The fixtures and implementation were frozen before inference, and the same code
and prompt ran through both cohorts. Fresh fixture content stayed hidden from
root and the implementer until code freeze. The fixture author knew earlier
failure families. The same-family reviewer had prior source/code exposure.
Review was contract-visible and hint-mode-blind, with possible incidental advice
exposure in model prose; the differing schemas prevent a full arm-blind claim.
Both primary reviews and all sensitivity overrides froze before unblinding.

The eight fresh source families and dependent repeats are a diagnostic sample,
not a public benchmark or a bound on rare harmful errors. No downstream answer,
memory retirement, source mutation or publication was performed. No comparison
here establishes that Haiku plus Jev replaces Opus across Sibyl workloads.

The conclusion is specific: retain the classifier and experimental harness, keep
normal-path activation off, and stop this verdict-format branch of Jev integration
optimization. No experimental arm is established here as Sibyl's production
critic baseline.
The simpler critic with optional Jev advice remains the candidate supported by
these synthetic results. Reopening adoption requires downstream evidence on a
representative approved workload and a mechanism addressing the demonstrated
critic errors and serial tail latency. Another prompt-only rerun on these cases
would not resolve those requirements.

## Evidence

The [code freeze](code-freeze.json), [preflight review](preflight-review.md),
[source review](fresh-fixture-review.md) and [cross-cohort audit](cross-cohort-audit.json)
bind the experiment. The [semantic audit](final-report-review.md),
[post-unblind diagnosis](semantic-diagnosis.md) and
[cross-model conclusion review](conclusion-review.md) document interpretation
and its revisions. Cohort folders retain manifests, original annotations,
blind packets, arm maps, summaries, per-path outputs and raw-artifact hashes.

The full immutable transport archives are retained on the devbox at
`/home/dev/dev/eval-runs/jev-verdict-20260920/{retained,fresh}/{plan,live,replay}`.
The corresponding local archive is `/tmp/jev-verdict-20260920`. Join scheduled
IDs by cohort and manifest; local IDs repeat across cohort namespaces. Actual
provider response IDs are unique across all observed calls.
