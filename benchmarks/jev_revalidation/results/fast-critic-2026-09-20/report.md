# Cached Jev hints with a fast complete critic

Cached Jev hints improved action-level results from the same fast critic on the
frozen synthetic cohort. The hinted arm made zero false accepts and retained all
24 safe accepts. The direct arm made five false accepts and produced two invalid
outputs. Finding-level review is reported separately below.

The experiment used all 32 earlier quality-speed cases, two repeats and two fresh
Haiku arms. Every path received the complete original evidence and the same critic
schema and advisory instructions. The only intervention was an empty advisory
list versus archived Jev labels bound to assertion paths and hashes. Confidence,
fixture gold and historical critic findings never entered that list.

## Fresh measurements

| Metric | Direct Haiku | Haiku with cached Jev hints |
| --- | ---: | ---: |
| Scheduled critic calls | 64 | 64 |
| Rubric action matches | 57/64 | 64/64 |
| Strict action matches | 47/64 | 52/64 |
| Unsafe or insufficient candidates accepted | 5/40 | 0/40 |
| Supported candidates accepted | 24/24 | 24/24 |
| Invalid outputs | 2 | 0 |
| Critic-stage median | 2,254.75 ms | 2,322.50 ms |
| Critic-stage p95 | 4,305.26 ms | 3,722.28 ms |
| Fresh provider-reported cost | $0.278300 | $0.288742 |
| Cost including attributed prior Jev calls | $0.278300 | $0.294866356 |

The entire fresh run cost $0.567042 for 128 calls. All usage costs were present.
The hinted system view adds $0.006124356 for the 64 archived Jev calls; that amount
was paid in the earlier experiment and was not billed again here.

The hinted median was 3.00% slower, so the predeclared median-no-increase hypothesis
failed. Its p95 was 13.54% lower. The median within-pair difference was -12.56 ms,
and hints were faster in 33 of 64 pairs. Those different summaries do not establish
a general latency effect on this small corpus. Both arms used concurrent requests
on the same provider; scheduling and provider variance remain part of the result.

The two direct-arm failures used the unsupported schema value
`unsupported_universal_claim` instead of a permitted finding basis. Both were paid
HTTP 200 responses. The unchanged validator rejected them; neither response was
repaired or rerun. Their costs and latency remain in the direct-arm totals.

The five false accepts covered transfer to a contradicted condition, a reversed
exception, an outdated location and an unobserved interval (both repeats). The
hinted arm flagged those same paths. Both repeats retained every supported candidate. The result is
a promising validation role beyond bypassing already-easy cases, subject to the
semantic findings and limits below.

## Finding-level review

The frozen [arm-blind annotations](semantic-review.json) cover all 128 outputs
and 76 raw findings. The [joined summary](semantic-summary.json) lists per-path
failures; the [arm map](blind-map.json) binds them to the original annotations.
Error outputs receive no valid coverage.

| Semantic measure | Direct Haiku | Haiku with cached Jev hints |
| --- | ---: | ---: |
| Complete semantic passes | 53/64 | 61/64 |
| Material concerns covered by fully valid findings | 30/40 | 37/40 |
| Fully valid findings / all raw findings | 30/36 | 37/40 |
| Fully valid findings / mechanically valid findings | 30/34 | 37/40 |
| Unsupported extra criticisms | 1 | 0 |
| Findings overstating evidence | 2 | 2 |
| Findings with invented factual additions | 2 | 3 |
| Valid abstentions | 0 | 0 |

Finding defect categories overlap. A critique can identify a real concern and
still fail because it invents a fact or overstates what the source establishes.
The hinted arm has ten paired semantic improvements and two regressions. One
regression asserts that no inspection happened when the packet only lacks an
inspection record. The other invents an empty drawer when the source establishes
only that a particular compass moved. Hints therefore improved this cohort's
aggregate coverage without eliminating critique harm.

The reviewer marked the inspection judgments as sensitive before unblinding.
Reading all three categorical clauses narrowly as statements about the supplied
record changes semantic passes to 54/64 direct and 63/64 hinted.
A separate [still-blinded sensitivity pass](semantic-sensitivity.json) also treats
contradiction wording literally across all four inspection responses and one
causality response. That stricter reading yields 52/64 direct and 60/64 hinted,
with concern coverage of 29/40 and 36/40. Both alternatives preserve the aggregate
direction; the narrow inspection wording distinction is not the basis for the
conclusion. The frozen scores remain the primary result. These are agent judgments
on synthetic cases, not human adjudication or downstream answer-quality gains.

## Scope and retained evidence

The [frozen manifest](manifest.json) binds the route, prompt code, fixture, rubric,
request schedule and prior archive. The source-derived rubric was written before
fresh calls. Its author used existing fixture gold and had seen some historical
review output; provenance records that exposure. Semantic review is by a separate
same-family agent, with arm identity, model metadata, hints, timings and costs
hidden until the annotations were frozen.

The selected [OpenRouter Haiku route](https://openrouter.ai/anthropic/claude-haiku-4.5)
was verified against the public model and endpoint catalogs before dispatch.
Requests pinned Anthropic, disabled provider fallbacks and retries, and retained
full evidence and the complete critique schema. The runner has no production
publication authority.

The comparison measures fresh critic service time with hints already available.
It does not measure Jev acquisition, background readiness, publication, retrieval
or downstream answer quality. The prior Opus arm had a 4,463.89 ms median and cost
$1.773265 for 64 paths. That is a historical reference with different invocation
instructions, not a contemporaneous replacement or speed comparison.

The hints disagreed with existing assertion gold on three of 92 labels, across
only two unique cases. The small number of wrong hints cannot establish robustness
to misleading advisory data. The reused corpus is exposed diagnostic data, and two
repeats of each case are dependent observations. A positive result needs fresh
held-out cases and actual Jev acquisition timing before product integration.

## Verification and reproduction

All code gates and live calls ran on `stef-gradial-com-main`. Offline receipt
audits also ran locally on retained artifacts. The complete
revalidation suite passed 346 tests; lint and type checking passed. Independent
preflight ran the 44 new tests plus an additional cancellation test (45 passed).
The author spot-checked queued cancellation and 17 route/replay/corruption tests.
The full fresh archive replay passed. A separate two-case probe reproduced both
schema-invalid outputs against the unchanged validator.

The [numerical audit](numerical-audit.json) independently checked all 128 raw
calls, route and request binding, hint provenance, complete cost accounting and
frozen source hashes. The [preflight review](preflight-review.md) and
[failure diagnosis](failure-diagnosis.md) retain their narrower conclusions.
The live [summary](summary.json) still says semantic review is pending because
that original artifact remains unchanged; the separate semantic artifacts above
record the completed review.

The raw devbox archive is `/home/dev/dev/eval-runs/jev-fast-critic-20260920/live`.
Its source archive is `/home/dev/dev/eval-runs/jev-quality-speed-20260920/live`.
The [raw file hashes](raw-artifact-hashes.json) bind those fresh receipts; the
[compact outcomes](outcomes.json) preserve every response used in semantic review.
The source archive is needed to replay cached-hint provenance.

```sh
moon run root:jev-revalidation-test
moon run root:jev-revalidation-lint root:jev-revalidation-typecheck
moon run root:jev-fast-critic -- \
  --cases benchmarks/jev_revalidation/quality_speed_cases.json \
  --source-archive /home/dev/dev/eval-runs/jev-quality-speed-20260920/live \
  --rubric benchmarks/jev_revalidation/fast_critic_rubric.json \
  --output-dir /absolute/path/to/new-replay \
  --replay /home/dev/dev/eval-runs/jev-fast-critic-20260920/live
```

## Next experiment

Continue with Jev as an advisor to a fast complete critic. Freeze fresh held-out
cases containing absent evidence, explicit contradiction, mixed supported claims
and misleading hints. Compare the same critic arms while measuring actual Jev
acquisition through the final decision. The decision to integrate requires
finding quality and complete-path latency together; the current result justifies
that experiment, not product activation or an Opus replacement claim.
