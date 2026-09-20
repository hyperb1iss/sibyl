# Fresh cases, live Jev acquisition and misleading hints

Live Jev removed six false accepts and improved action matches on fresh synthetic
cases, but did not improve complete-path speed or cost. Deliberately wrong hints
caused additional rejection of supported candidates. The experiment supports
further critic-quality work, not activation of Jev-assisted validation.

## Fresh measurements

| Measure | Direct Haiku | Live Jev + Haiku | Misleading hints + Haiku |
| --- | ---: | ---: | ---: |
| Scheduled critic paths | 48 | 48 | 48 |
| Actual Jev calls | 0 | 48 | 0 |
| Acceptable-action matches | 32/48 | 40/48 | 29/48 |
| Unsafe candidates accepted | 6/24 | 0/24 | 2/24 |
| Supported candidates accepted | 16/24 | 17/24 | 8/24 |
| Invalid critic outputs | 3 | 1 | 1 |
| Complete-path median | 2,465.65 ms | 2,500.73 ms | 3,034.47 ms |
| Complete-path p95 | 4,954.37 ms | 4,963.48 ms | 5,146.46 ms |
| Provider-reported cost | $0.170856 | $0.171059092 | $0.193404 |

The run made 192 calls and cost $0.535319092. All costs and attempts were known.
The live arm includes $0.002542092 for its 48 fresh Jev calls. Its median rose
1.42%, p95 rose 0.18%, and cost rose 0.12% versus direct Haiku. The predeclared
speed and cost gates fail; these tiny differences do not establish a general
slowdown or price effect beyond this run.

The six direct false accepts covered three cases in both repeats: an accepted
job submission treated as completed work, a warm-cache result transferred to a
cold-cache trial, and an explicitly unperformed digest check claimed as done. Actual Jev hints caused
Haiku to flag those paths. Every previously accepted supported path remained
accepted in the live arm; one schema failure became a supported acceptance.

Wrong hints reduced supported acceptance from 16/24 to 8/24. Their lower false
accept count does not offset that loss. The deliberately adversarial intervention
measures susceptibility, not the frequency of such mistakes in natural Jev
traffic. Natural Jev labels disagreed with the fixture on four of 84 assertions:
ambiguous report conflicts and insufficient cross-encoding evidence were called
contradictions in both repeats.

Some critics explicitly acknowledged that an assertion was supported and still
flagged a hypothetical implication. Examples include treating a report of a
worker-count change as an unsupported causal claim, or questioning a directly
reported upload total while acknowledging the arithmetic was correct. The full
semantic review below distinguishes real concerns from these extra criticisms.

## Finding-level review

The [frozen blind annotations](semantic-review.json) cover all 144 outputs and
138 findings, including one diagnostic finding embedded in a malformed string.
The five mechanical failures receive no semantic pass or valid concern coverage.
The [joined summary](semantic-summary.json) and [arm map](blind-map.json) retain
per-path outcomes and paired changes. Paired defect counts include mechanically
valid outputs only; rejected-output diagnostics remain in the arm-level totals.

| Semantic measure | Direct Haiku | Live Jev + Haiku | Misleading hints + Haiku |
| --- | ---: | ---: | ---: |
| Complete semantic passes | 22/48 | 38/48 | 15/48 |
| Material concerns covered by fully valid findings | 13/24 | 22/24 | 15/24 |
| Fully valid findings / mechanically valid findings | 14/38 | 22/36 | 15/57 |
| Unsupported criticisms, including invalid-output diagnostics | 24 | 13 | 41 |
| Valid abstentions | 0 | 0 | 0 |

A duplicated valid finding cannot increase concern coverage. Live Jev produced
17 paired semantic improvements and one regression. The regression correctly
challenged an unsupported causal claim but also criticized a separately supported
worker-count observation. The aggregate semantic improvement is substantial in
this small diagnostic set, yet the predeclared no-new-harm condition fails.
Ten live-arm outputs still fail semantic review.

The reviewer flagged one ambiguity before unblinding: an export containing no
recorded web-editor events does not necessarily establish that no actual writes
occurred. Five critiques dropped the recorded-event qualification. Treating that
channel as a closed world changes passes to 24/48 direct, 39/48 live and 17/48
misleading. The original strict scores remain primary, and the live-arm regression
and decision remain unchanged under the alternative.

The stress arm has three paired semantic improvements and ten regressions. In one
response, the critic explicitly says a supported claim is accurate and that the
advisory contradiction label is wrong, yet still emits a contradiction finding
with a remove disposition. Recognizing a bad hint in prose did not prevent a
harmful structured output. Misleading-hint robustness fails.

## Invalid outputs and accounting

The [failure diagnosis](failure-diagnosis.md) reproduced all five rejections with
the unchanged parser: four unrecognized finding-basis labels and one string where
the schema requires a findings array. All were paid HTTP 200 responses on the
expected model/provider route. Their $0.021280 critic cost remains included, along
with the affected live path's Jev cost. No response was repaired or retried.
The [independent raw audit](numerical-audit.json) verified all 192 calls, receipt
inventories, source and invocation bindings, hint isolation, timing containment
and observed costs.

## What was frozen

The experiment uses 24 new synthetic cases arranged as 12 matched pairs. Each
pair shares its evidence and changes one assertion. There are 42 assertions:
30 supported, seven contradicted, four insufficient and one ambiguous. Two
repeats produce 48 paths per arm. Repeats and paired cases are dependent; the
12 matched pairs are the analysis clusters, not 48 independent examples.

The critic instructions, schema, model and provider controls are unchanged from
the prior fast-critic experiment. Every arm receives its case's complete original
evidence. A separate author wrote the
fixtures without viewing the current prompt or implementation. Historical error
types informed the challenge-set design, so the cases are prospective synthetic
diagnostics rather than an independent public benchmark. The implementers froze
the runner before opening fresh case contents. Independent source review corrected
one ambiguous candidate and one copied rubric note before calls; original files
and both freezes are retained.

Three arms answer separate questions:

- The direct arm invokes Haiku with an empty advisory slot.
- The live-Jev arm prepares a fresh source-bound Jev request, interprets its actual
  response, then invokes the complete Haiku critic with those labels. A failed
  Jev attempt falls back to an empty advisory slot, preserving its cost and time.
- The misleading arm invokes Haiku with deliberately incorrect labels written by
  the fixture author. Only labels differ from ordinary hints. This diagnostic
  makes no Jev call and does not measure real-world Jev error frequency.

No original labels, rationale, rubric, probabilities or confidence enter the
natural-Jev or direct provider requests. The stress intervention deliberately
uses offline labels to construct wrong advisory data; it never enters the other
arms. All model results remain advisory experiment outputs without publication
or memory-lifecycle authority.

The [Haiku route](https://openrouter.ai/anthropic/claude-haiku-4.5) pins Anthropic.
The [Jev route](https://openrouter.ai/typesafe/jev-1.13) pins TypeSafe and the dated
resolved model version. Public catalogs were checked before dispatch. Both routes
disable fallback and retries; raw request and response receipts retain observed
route and billing metadata.

## Measures and gates

The primary comparison is live Jev versus direct Haiku. Quality requires increased
complete semantic passes with no new paired harmful acceptance, unsupported
finding, missed concern or lost supported acceptance. The diagnostic speed target
is at least 20% lower median with no higher p95. Cost includes every actual Jev
and critic attempt, and unknown charges preclude a cost-success claim. Misleading
hint harm is a separate robustness veto, not pooled with normal-route timing.

Service timing begins before candidate preparation and ends after the full critic
and mechanical validation. Live-Jev timing includes request construction, network
acquisition, interpretation and fallback. Queue delay is recorded separately.
Source retrieval, authorization, durable publication, downstream answer quality
and interactive recall are outside this experiment.

The source-derived semantic rubric was frozen before calls. Every explicit
unsupported contradiction or invented clause fails even if later wording softens
it. This is stricter than the earlier study's contextual primary rubric, so raw
semantic-pass rates across the two studies are not directly comparable.

A separate same-family reviewer receives original evidence, the frozen rubric,
mechanical outcomes and randomized opaque response IDs. Arm identities, hints,
route metadata, timing, costs and scheduling order remain hidden until annotation
is frozen. Wording may still suggest an arm; this is not human adjudication.

## Verification and reproduction

All code gates and live calls ran on `stef-gradial-com-main`. Offline artifact
audits also ran locally. The complete revalidation suite passed 367 tests, and
lint and type checking passed. Independent preflight ran all 21 new tests and
four additional adversarial probes covering an actual adapter deadline, altered
valid Jev labels, dispatch corruption and a missing path receipt. Root repeated
those four probes and compared independently prepared manifest and schedule bytes.
Full live-archive replay passed without additional provider calls. The
[final independent review](final-report-review.md) recomputed the blinded join,
concern coverage, paired changes and sensitivity from all original outputs.

The [frozen manifest](manifest.json), [source review](fixture-review.md) and
[preflight review](preflight-review.md) bind the experiment. The
[compact outcomes](outcomes.json) retain every path and critic output; the
[raw artifact hashes](raw-artifact-hashes.json) bind the complete devbox archive.
The initial fixture freeze is preserved separately from the two pre-call wording
clarifications. The original machine summary remains unchanged, including its
pending semantic-review field; completed semantic judgments are separate artifacts.

The authoritative raw archive is
`/home/dev/dev/eval-runs/jev-heldout-fast-20260920/live`.

```sh
moon run root:jev-revalidation-test
moon run root:jev-revalidation-lint root:jev-revalidation-typecheck
moon run root:jev-heldout-fast-critic -- \
  --cases benchmarks/jev_revalidation/heldout_fast_cases.json \
  --rubric benchmarks/jev_revalidation/heldout_fast_rubric.json \
  --output-dir /absolute/path/to/new-replay \
  --replay /home/dev/dev/eval-runs/jev-heldout-fast-20260920/live
```

## Decision

Keep Jev as an experimental advisor. The fresh challenge set supports a useful
quality contribution to a cheap complete critic, while the speed target, cost
non-increase threshold and no-new-harm gate all fail. Wrong hints cause significant
supported-claim rejection in this deliberately adversarial setting.

The next priority is an assertion-first critic contract that distinguishes an
actual unsupported proposition from a hypothetical stronger claim. Test that
change against the retained failure cases, then freeze another untouched set for
confirmation. Preserve Jev as a separate intervention so any improvement can be
attributed to the critic change rather than assumed to come from hints. No
production activation, automatic retirement or Opus replacement is qualified.
