# Post-unblind semantic diagnosis

The fresh assertion-first live-Jev arm has the strongest observed semantic result (31/32), but the misleading arm's 31/32 action score is not evidence of reliable criticism. Its 17/32 semantic passes and 3/16 fully covered concerns expose a substantial wrong-target failure. Original annotations remain unchanged.

## Evidence and reconstruction

This review joined the original frozen annotations to `/tmp/jev-assertion-20260920/fresh/blind-map.json` only after explicit unblinding permission. Counts below were reconstructed from that join, original blind responses and the frozen rubric, independently of the parent's semantic summary.

| Fresh arm | Acceptable action | Strict semantic pass | Fully covered concerns | False accepts |
| --- | ---: | ---: | ---: | ---: |
| Baseline direct | 28/32 | 21/32 | 10/16 | 2 |
| Assertion-first direct | 29/32 | 27/32 | 14/16 | 1 |
| Baseline live Jev | 31/32 | 27/32 | 12/16 | 0 |
| Assertion-first live Jev | 31/32 | 31/32 | 16/16 | 0 |
| Baseline misleading | 27/32 | 13/32 | 7/16 | 0 |
| Assertion-first misleading | 31/32 | 17/32 | 3/16 | 0 |

## Why the misleading arm looks good at action level

All 16 safe case/repeat paths correctly accept. Of the 16 unsafe paths, 15 return a mechanical flag and one is a mechanical error. A coarse acceptable-action score credits every one of those flags regardless of its target or correctness. Only one unsafe response passes the complete semantic rubric (the first durability repeat), producing 16 + 1 = 17 semantic passes.

Nine unsafe responses flag only the supported neighboring assertion and never target the actual concern: both repeats for the bit-state, temperature-extrapolation, masked-count and staged-configuration cases, plus the first saturation repeat. Four other unsafe responses address the main claim but have an invalid finding or output. The two causal responses correctly cover the causal concern but also criticize the explicitly supported fixed-fan-speed assertion. Consequently only three fully valid concern instances survive: the first durability repeat and both causal repeats. The latter two are still semantic failures because of the additional unsupported findings.

A representative response is the masked-count case `af-010`, repeat 0, blind ID `dea338584df35d80` (row `case-9-repeat-0-assertion_misleading`). The source explicitly says the grid has three rows and four cells per row, with two masked cells and ten contributors. The incorrect main assertion says twelve cells contributed. The response instead targets `/claim_records/0/content`, the correct grid dimensions, and says ten contributors are “incompatible with the claim of twelve total cells.” Masking changes the denominator, not the grid dimensions. Its `factual_contradiction` / `remove` finding could remove correct information while leaving the actual false count untouched. The action flag receives credit; the finding receives none.

An even clearer contract failure is the saturation case `af-014`, repeat 0, blind ID `5cd1248c6809f51b`. The response emits a `factual_contradiction` / `reconsider` finding against the correct adjacent-sample flag. Its prose explicitly acknowledges the source agrees and concludes, “No actual defect is present in the assertion itself.” The main assertion's unjustified exactly-500-N claim is omitted. The model recognizes disagreement with the advisory label but represents that disagreement as a finding against the candidate. No prose retraction repairs the emitted finding.

These are observed advisory susceptibility and finding-target failures. Deliberately wrong authored hints are a robustness diagnostic, not the frequency distribution of real Jev errors. Zero false accepts in this arm does not establish safety for downstream correction or publication.

## The two new direct-arm semantic regressions

The prompt-only direct comparison improves eight case/repeat paths and regresses two. The net result is 21 to 27 semantic passes.

1. The temperature case `af-008`, repeat 1, changes from baseline blind ID `d19504f85b2e8afd` to assertion-first ID `0b78d0b8d7dde196`. Both correctly identify unsupported extrapolation from 20 C to 90 C. The revised output adds a `missing_condition` finding against the supported statement that the serial was checked before immersion, demanding further session specificity despite the source's explicit verification. Concern coverage is preserved, but the extra false positive makes the entire revised output fail.
2. The conflicting CAD case `af-012`, repeat 1, changes from baseline ID `ed5019aeb5472a0c` to assertion-first ID `e0f6056eb68bfdb9`. Both identify the unresolved same-revision design conflict. The revised critique calls the selected design callout “one measured value.” The source explicitly distinguishes design thickness from measurement of a manufactured bracket. The frozen strict clause-level rubric treats that auxiliary factual claim as unsupported, invalidating the finding and its coverage. A pre-unblind sensitivity already records the alternative interpretation that “measured” was loose wording for a numerical callout. That alternative would remove this regression; it does not alter the primary result or the other regression.

There is also a new unsupported target on the already-failing acoustic case `af-005`, repeat 1: revised direct adds criticism of fixed fan speed. A pass/fail regression counter alone would miss this because both outputs fail. The source does establish the sound readings and fixed speed; neither assertion claims a causal explanation. Thus the direct prompt change fails a strict no-new-harm criterion even apart from the two newly failing responses. No new false acceptance was introduced in the observed direct comparison, but one existing false acceptance remains (`af-016`, repeat 1).

## What the fresh revised live result supports

Against assertion-first direct, assertion-first live has four semantic improvements and zero regressions. The improvements are the first supported acoustic comparison, the second temperature-extrapolation repeat, the second CAD-conflict repeat and the second final-status contradiction. The final-status case changes from false acceptance to a correctly targeted contradiction finding. All 16 unsafe concern instances are covered; no unsafe case accepts.

Against baseline live, assertion-first live also has four improvements and zero regressions. The revised output removes a categorical negation in the first durability repeat, uses an unsupported-extrapolation finding rather than contradiction for the first high-temperature repeat, and preserves the inclusive 500-N lower bound in both saturation repeats. Baseline live has 12/16 concern coverage; revised live has 16/16.

I compared finding targets and frozen defect types, missed concern IDs and the actual remaining failing prose. Neither comparison introduces a newly defective target or newly missed concern in this cohort. The revised live arm's sole failure is the already-failing supported acoustic case `af-005`, repeat 1, blind ID `fca9f9a070485f41`: it reads a factual recorded-level comparison as implicit causality. Baseline live criticizes both that comparison and the supported fan-speed neighbor; revised live retains only the former false positive. Revised direct also already criticizes those two supported assertions. The revised live output is therefore not a new substantive harm in either pairing under the frozen rubric.

This establishes observed no-new-harm on these 32 paired paths, including finding-level inspection, not universal semantic equivalence. The 16 cases form eight matched clusters, repeats are dependent, and each live arm obtains independently sampled Jev advice. The old-live versus new-live comparison therefore measures a combined policy run rather than an isolated prompt effect. Same-family judgment, prior fixture/code exposure and the predeclared wording sensitivities remain limitations. The misleading arm's failures also rule out any broad claim that the new critic is robust to incorrect advice.

## Immutable bindings

- Fresh original annotation SHA256: `b63c236d93f5896e4255700b276927c352102ca0d4587c654c6f5ec04572807d`.
- Fresh blind responses SHA256: `63165d98d10a486a4c0b823768aee98bf60c7543f53b991231587f27c9f36fa0`.
- Fresh rubric SHA256: `a37c43b506f8f083b07c079dfe4ab3e5ce5b0a437b23aa1f6f2f7e66d7174f04`.
- Fresh blind map SHA256, bound before annotation: `fba4b5bc8ebc3226dd8c3d09bc79b0e28861697cea8e97eb8a490535f6a1c49f`.

No annotations, labels, source files or provider receipts were edited. No calls or tests were run for this diagnosis.
