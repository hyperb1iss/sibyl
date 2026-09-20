# Post-unblind semantic diagnosis

Recommendation: defer production integration of this explicit-verdict critic. Keep the experiment and its strict validation machinery as evidence, not as an activated review policy. The schema improves some fresh direct and deliberately misleading-hint results, but it loses against the existing assertion-prefix critic with live Jev in both cohorts. The evidence does not justify replacing the current critic, bypassing it, or promoting Jev labels to authority. No new experiment is proposed here.

This diagnosis follows frozen, contract-visible, hint-mode-blind judgments. The reviewer is the same model family, has prior fixture/code exposure, and is not human gold. All primary and alternative judgments were frozen before maps were opened. Original annotations remain unchanged. Repeated outputs and minimal pairs are dependent synthetic observations, not independent production trials.

## Independently checked comparison

I joined the frozen annotations to the released maps and independently matched every arm's primary projected/raw/overall pass count and concern coverage, plus every reported paired overall improvement/regression count.

| Cohort and arm | Overall pass | Consumer projected pass | Valid concern coverage |
| --- | ---: | ---: | ---: |
| Retained baseline direct | 11/16 | 11/16 | 8/8 |
| Retained baseline live Jev | 13/16 | 13/16 | 7/8 |
| Retained baseline misleading | 6/16 | 6/16 | 4/8 |
| Retained verdict direct | 7/16 | 9/16 | 7/8 |
| Retained verdict live Jev | 11/16 | 12/16 | 7/8 |
| Retained verdict misleading | 6/16 | 7/16 | 6/8 |
| Fresh baseline direct | 22/32 | 22/32 | 14/16 |
| Fresh baseline live Jev | 29/32 | 29/32 | 15/16 |
| Fresh baseline misleading | 14/32 | 14/32 | 7/16 |
| Fresh verdict direct | 26/32 | 26/32 | 14/16 |
| Fresh verdict live Jev | 27/32 | 27/32 | 14/16 |
| Fresh verdict misleading | 22/32 | 22/32 | 13/16 |

The extra raw rationale surface explains two retained direct failures and one retained live failure beyond consumer projection. Those are not finding-level regressions. Applying every predeclared contextual sensitivity gives retained overall scores of 11/13/6 for baseline and 9/12/8 for verdict (direct/live/misleading), and fresh scores of 22/30/14 versus 28/29/23. The live schema comparison remains unfavorable even under those alternatives.

## Schema regressions are concrete

Against baseline direct, the new schema has zero retained overall improvements and four regressions (two projected regressions). The retained acoustic case af-006 acquires an invented objection to the supported fixed-fan-speed assertion and calls absent causal attribution a contradiction. The retained quoted-input case hf-017 becomes mechanically invalid through duplicate verdicts. The other two overall regressions are the separately recorded raw chronology/causal-language defects on hf-005 and hf-006.

Fresh direct has six improvements and two regressions. The response for vf-010 repeat 1 (`4bc8664a475b32a71b2d5f4f`) correctly rejects eleven complete messages, but also criticizes the independently supported assertion that all complete messages in this capture use S6. Its objection imports the other assertion's wrong count into this correct assertion. The response for vf-012 repeat 1 (`1ec22b383496f4b62ece423a`) identifies conflicting digests but turns missing authority into direct contradiction of an authoritative digest. That second regression has a frozen contextual alternative; the neighbor-contamination regression does not.

Against baseline live Jev, the verdict schema has no paired improvements in either cohort. Retained has two overall regressions: a raw-only chronology claim on hf-006, and a projected false criticism of the 24-KiB frame payload on hf-024. The latter response (`48f71b7b291c9c4f4095b0d9`) calculates 4 x 6 = 24 and says the assertion is technically accurate, yet still emits factual_contradiction because the complete upload includes a separate manifest. The exact assertion never claimed the complete-upload total.

Fresh live has two regressions: vf-003 repeat 0 returns a string instead of a verdict array (`e3dafa38959f78e9b64ff437`); vf-016 repeat 1 (`25953364db1766c01edc484c`) misstates the candidate as saying the SQL string was deleted by the preview. Its later execution-versus-preview reasoning is useful, but the strict clause rule rejects the false paraphrase. The latter is explicitly sensitivity-bound and becomes a pass under the already frozen contextual reading. The mechanical regression remains under both readings.

## Jev increment is promising but not harmless

Within the verdict contract, live Jev gains five retained paired overall passes and loses one; fresh gains three and loses two. Fresh Jev removes the false stream-S6 neighbor findings on both vf-010 repeats and improves the authority wording on vf-012 repeat 1. Fresh losses are the same malformed array and strict preview-paraphrase defect described above. No additional unsupported-concern target appears in that fresh comparison, but an invalid response and an invalidating clause still prevent a no-harm conclusion.

The retained lost concern is af-008: an unmeasured 90-degree probe error becomes factual_contradiction rather than uncertainty. Crucially, the verdict-live Jev observation is unavailable and its hints are empty. This is a regression of the complete live policy arm, not evidence that a supplied Jev label caused it. The separate baseline-live invocation for af-008 did receive an incorrect contradicted label. Independent invocations, failures and output sampling mean these are policy comparisons, not isolated causal estimates of individual labels.

Even the fresh baseline direct-to-live comparison's seven pass gains and zero pass regressions is not complete no-harm proof: vf-012 repeat 0 changes a finding into the stronger claim that no authoritative selection exists, losing strict concern coverage in an already failing output. Binary pass transitions and sets of defect categories do not establish equivalence of the actual prose.

## Correct hints are sometimes ignored; wrong hints are not reliably vetoed

The fresh case vf-005 provides a decisive source check. The candidate says only that the recorded second link invocation finished ten seconds faster. The source gives 22 seconds and 12 seconds, with no causal allocation between plugin and cache changes. Both verdict-live repeats receive supported hints for both the timing comparison and worker count. The critic nevertheless calls the plain comparison an unsupported causal attribution. The actual responses are `a549211275430dca827c7e65` (repeat 0) and `cf0260b4310e38c0dc02a17b` (repeat 1). Both cite the correct source and preserve the worker assertion; both invent causality in the timing assertion. Jev's correct evidence label did not protect the supported claim. This is also a failure shared by the direct verdict and live baseline samples, not a newly introduced live-label defect.

Explicit verdicts improve fresh deliberately wrong-hint performance from 14/32 to 22/32 (nine paired improvements, one regression), and concern coverage from 7/16 to 13/16. That is useful robustness evidence, but does not establish a veto against bad advice. Retained misleading verdicts produce an actual false accept on af-010 (`295f33d820b366f2d8fa5b9c`): the source explicitly names ten contributing cells after masking; the critic labels the twelve-contributor assertion supported by redefining contributors as the total acquired grid. Other retained arms have no false accept. No fresh arm falsely accepts an unsafe candidate in these samples.

The fresh misleading case vf-014 demonstrates why even a flag is inadequate. Both repeats mark the wrong 900-second TTL assertion supported while their supported rationales explicitly explain that the source says 300 seconds. The only emitted concern attacks the supported nameserver-delegation neighbor. The consumer receives the wrong finding and loses the real TTL defect. The raw verdict reveals the defect, but the projection hides that particular concern. Neither response is counted as projection_hides_defect because the consumer projection already fails; that field describes whole-response pass divergence, not every hidden concern. The original annotations separately record contains_hidden_concern on these supported rationales (`43ec43f22c26eb622cc9b2f1`, `6f9dd6dcb22a96df257f0d86`).

## Decision boundary

The narrow positive result is better fresh direct precision and better resistance to some injected labels. The required broader result, better grounded findings with no new harm against the existing live-Jev path, was not met. Structural completeness does not force semantic consistency between a verdict, its rationale and its projected findings. Keep publication authority and fallback behavior unchanged. The semantic evidence is sufficient to defer integration without inventing a cost or speed justification, and this review does not independently audit latency or cost.

Evidence read: frozen annotations and sensitivity overrides; both released blind maps and semantic summaries; original blind sources/rubrics and raw outputs; selected live rows for actual hint/failure bindings. No source, rubric, annotation, provider call or production memory was changed.
