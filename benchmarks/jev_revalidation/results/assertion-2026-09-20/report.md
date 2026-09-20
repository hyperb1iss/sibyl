# Assertion precision and Jev advice

The revised critic plus live Jev passed 31/32 fresh semantic reviews, compared
with 21/32 for the original direct critic, 27/32 for the revised direct critic
and 27/32 for the original critic with Jev. The combination shows a useful
quality gain at roughly two-second median latency. The instruction change alone
introduced two fresh regressions, and wrong hints still damaged finding quality.
The experiment does not qualify activation.

## Fresh confirmation

Sixteen newly authored synthetic cases form eight minimal pairs. Each case ran
twice in each of six arms. The original and revised critics each received no
hints, freshly acquired Jev hints, or deliberately incorrect hints.

| Critic path | Semantic passes | Action matches | False accepts | Supported accepts | Median | p95 | Cost |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Original, direct | 21/32 | 28/32 | 2 | 14/16 | 2.040s | 4.452s | $0.105113 |
| Original + live Jev | 27/32 | 31/32 | 0 | 15/16 | 2.139s | 3.728s | $0.108284 |
| Original + wrong hints | 13/32 | 27/32 | 0 | 11/16 | 3.211s | 5.822s | $0.126337 |
| Revised, direct | 27/32 | 29/32 | 1 | 14/16 | 2.036s | 4.567s | $0.115659 |
| Revised + live Jev | 31/32 | 31/32 | 0 | 15/16 | 2.088s | 3.364s | $0.117673 |
| Revised + wrong hints | 17/32 | 31/32 | 0 | 16/16 | 1.483s | 5.066s | $0.122218 |

The fresh cohort made 192 critic calls and 64 Jev calls for $0.695284232. The
retained cohort made 48 critic calls and 16 Jev calls for $0.194381488. Total
observed spend was $0.889665720 across 320 unique provider responses, with no
unknown costs or attempts. Cohorts remain separate in all quality comparisons.

The direct prompt change increased fresh cost by 10.03% without materially
changing median latency (2.040s to 2.036s). Adding Jev to the revised critic
increased median latency 2.58% and cost 1.74%, while reducing p95 by 26.35%.
Both primary comparisons fail the predeclared median-speed and cost gates.
Small tail samples support descriptive measurements, not a production SLO.

The revised wrong-hint path accepted all 16 supported candidates, compared with
11 for the original wrong-hint path. Its median fell 53.82% and cost fell 3.26%.
Those stress results measure resistance to deliberately wrong labels; they are
not normal-route economics or the natural frequency of Jev mistakes.

Blind review found eight improvements and two regressions from the direct prompt
change. One regression added a finding against a supported serial-number check;
the other described a design callout as a measured value. Fully valid concern
coverage rose from 10/16 to 14/16, but the no-new-harm gate fails.

Adding live Jev to the revised critic produced four semantic improvements and no
paired semantic regression, new missed concern or new defective target in this
fresh set. Fully valid concern coverage rose to 16/16. The revised live path also
improved four outputs versus the original live path without a new paired harm.
Those are positive within-cohort findings, not an exception to the failed retained
or stress checks. The single revised-live failure followed rejected Jev output:
the full critic received empty hints and criticized a supported acoustic
comparison for a causal claim the assertion did not make.

The wrong-hint action score hides a much weaker result: the revised stress path
matched 31/32 actions but passed only 17/32 semantic reviews and covered just
3/16 material concerns with fully valid findings. Compared with its direct path,
it introduced 12 semantic regressions. Correctly flagging an unsafe candidate
is insufficient when the explanation is unsupported or targets a correct claim.
The apparent supported-acceptance improvement does not satisfy robustness. Nine
unsafe outputs flagged only a supported neighboring assertion. One recommended removing the
correct three-by-four grid statement because ten cells contributed to a mean,
while leaving the false twelve-contributor assertion untouched. The
[post-unblind diagnosis](semantic-diagnosis.md) checks these targets and the
remaining failed live-path prose directly.

The reviewer froze ten sensitivity items before unblinding. Applying only those
alternative readings changes original direct/live/stress passes to 22/28/15 and
revised passes to 28/31/18. Original strict scores remain primary. The revised
live result stays strongest; the direct-prompt regression and stress veto remain.
See the separate [sensitivity receipt](fresh/sensitivity.json).

## Retained diagnostics

Eight previously inspected cases form four pairs, with one repeat per arm. The
selection covers causal inference, deployment scope, recorded-event scope and
arithmetic, including supported neighboring assertions. These are development
cases, not held-out evidence.

| Critic path | Semantic passes | Action matches | False accepts | Supported accepts | Median | p95 | Cost |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Original, direct | 2/8 | 5/8 | 0 | 1/4 | 3.868s | 4.969s | $0.033372 |
| Original + live Jev | 5/8 | 6/8 | 0 | 2/4 | 2.836s | 5.392s | $0.030748 |
| Original + wrong hints | 2/8 | 4/8 | 0 | 1/4 | 2.988s | 5.724s | $0.032742 |
| Revised, direct | 2/8 | 6/8 | 0 | 2/4 | 3.049s | 4.110s | $0.033771 |
| Revised + live Jev | 5/8 | 6/8 | 0 | 2/4 | 2.274s | 3.264s | $0.030577 |
| Revised + wrong hints | 4/8 | 6/8 | 0 | 2/4 | 2.945s | 5.360s | $0.033171 |

The instruction change left semantic passes unchanged for both direct and
live-Jev paths. The revised direct critic still emitted a contradiction finding
whose own explanation said the production release assertion was accurate and
identified no defect. Another finding acknowledged that four frames carried
24 KiB, then criticized the assertion for omitting a separate manifest even
though the assertion explicitly concerned the frames.

The revised live path lost one previously covered concern. Aggregate action
matches and pass totals therefore cannot establish no-new-harm. The frozen
review also preserves the existing recorded-versus-occurred ambiguity: treating
the web-editor channel as a closed world could change two valid outputs, while
the third remains a mechanical error. Original strict scoring stays primary.

## Intervention and controls

The revised critic adds one fixed instruction prefix. It requires objections to
address the assertion's actual proposition, protects supported neighbors,
distinguishes absence from contradiction, names the existing basis enums, and
requires every factual clause of a finding to have original support. No output
schema, parser, evidence, model, route, retry policy or publication authority
changes. The prefix remained unchanged through both cohorts.

Direct versus direct isolates the instruction intervention. Live versus live
compares combined policies with independently acquired Jev labels. Fresh live
labels agreed on 31/32 paired paths; the remaining path had a rejected Jev
response and empty-hint fallback. Wrong-hint comparisons use identical authored
labels across critic contracts. No Jev call is made in either stress arm.

The [source review](fixture-review.md), [author audit](fixture-author-audit.json)
and [code freeze](code-freeze.json) precede inference. Fixture authorship was
separate from prompt implementation, and fresh contents stayed hidden from the
implementers until code freeze. Historical error types informed the fixture
author, so this is prospective synthetic confirmation rather than a public
benchmark. Eight pairs are clusters; repetitions are dependent. All fresh source
packets are under 1KB and do not establish long-context or downstream task quality.

Service timing begins before candidate preparation and ends after mechanical
validation. Each live arm includes its own fresh Jev acquisition and fallback;
queue delay is separate. Source retrieval, authorization, durable publication
and interactive recall are excluded. Calls ran on the shared devbox at the
frozen concurrency of eight. Retained offline replay overlapped a short portion
of fresh dispatch; the host was not a dedicated performance environment.

Both routes were checked against current catalogs: [Haiku with Anthropic](https://openrouter.ai/anthropic/claude-haiku-4.5)
and [Jev with TypeSafe](https://openrouter.ai/typesafe/jev-1.13). Requests retain the
same pinned route controls as the prior experiment. Actual raw responses bind
observed routing, token usage and charges.

## Failures and verification

Two critic outputs violated the unchanged basis enum: one retained original
wrong-hint response used unsupported_universality, and one fresh revised
wrong-hint response used unsupported_certainty. Both remain paid errors with
zero semantic credit. Their combined critic cost was $0.007781.

One fresh Jev response supplied probabilities totaling 0.99 for one assertion.
The unchanged adapter rejected the response, retained its $0.00005775 charge and
continued the full critic with empty hints. No response was repaired or retried.
Natural-label agreement was 56/60 for the original live path and 54/58 completed
assertions for the revised path, or 54/60 scheduled assertions including the two
unavailable labels. Both variants called unsupported temperature transfer and
an insufficiently precise bound contradictory in both repeats.

All builds, tests and provider calls ran on stef-gradial-com-main. The suite
passed 391 tests; lint and type checking passed. Independent preflight passed
59 selected tests, including five adversarial probes; root reran those five and
spot-checked independent plan bytes. Both complete live archives replayed without
provider calls. The [preflight receipt](preflight-review.md) records exact hashes
and the pre-call manifest serialization fix.

Independent raw audits passed [2,961 retained checks](retained-numerical-audit.json)
and [10,190 fresh checks](fresh-numerical-audit.json). Every provider response ID
was unique. Local scheduled IDs repeat between cohorts, so archive joins must
include cohort or manifest identity. The [identity audit](cross-cohort-identity-audit.json)
verifies this distinction. Arm-blind semantic annotations remain separate from
the original machine summaries, whose pending-review field is preserved.

The [retained annotations](retained/semantic-review.json) and
[fresh annotations](fresh/semantic-review.json) cover all 240 critic responses and
180 findings. A same-family reviewer saw original sources, the frozen rubric and
randomized opaque response IDs, while arms, contracts, hints, routes, timing and
cost remained hidden until both reviews were frozen. The reviewer had prior code
and source-review exposure, and wording could suggest an arm. No human or
cross-family adjudication is claimed. Machine errors earn zero semantic credit.
The separate [retained join](retained/semantic-summary.json) and
[fresh join](fresh/semantic-summary.json) retain paired changes. Defect-target
sets are diagnostics, not a full equivalence test of critique wording.

The [final independent review](final-report-review.md) verifies both complete
semantic joins, sensitivity results, failed gates and report claims.

The [failure diagnosis](failure-diagnosis.md) reproduces all rejected outputs
against the unchanged validators without repairs or retries.

The authoritative archives are
`/home/dev/dev/eval-runs/jev-assertion-20260920/{retained,fresh}/live`.

```sh
moon run root:jev-revalidation-test
moon run root:jev-revalidation-lint root:jev-revalidation-typecheck
moon run root:jev-assertion-critic -- \
  --cases benchmarks/jev_revalidation/assertion_fresh_cases.json \
  --rubric benchmarks/jev_revalidation/assertion_fresh_rubric.json \
  --repeats 2 --output-dir /absolute/path/to/new-replay \
  --replay /home/dev/dev/eval-runs/jev-assertion-20260920/fresh/live
```

## Decision

Keep the revised-plus-Jev combination as an experimental candidate. It earns a
fresh quality improvement, including complete material-concern coverage on this
set, but does not meet the median-speed or cost targets. Retained failures and
wrong-hint susceptibility still prevent activation or replacing the full critic.

The next experiment should change the output contract to explicit per-assertion
verdicts, then derive findings only from concern verdicts. A supported assessment
should have a direct representation rather than appearing inside a contradiction
finding. Test whether that structural change reduces the observed contradiction
between the critic's explanation and its action, while preserving every real
concern and measuring the extra cost. Keep Jev as a separately measured advisor;
typed verdicts alone do not establish semantic correctness.
