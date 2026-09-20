# Frozen source-support fallback qualification

The frozen Jev policy bypassed 33 of 128 simulated source-support checks (25.8%) on fresh synthetic cases, with zero unsafe candidates bypassed. The grouped calls cost $0.007106484. Both request geometries together cost $0.020270040 across 384 completed calls on `stef-gradial-com-main`.

The result supports further evaluation of a narrow source-support route. It does not establish savings on a complete eval run or justify skipping the semantic critic. All 40 unsafe candidates already had a non-supported assertion before applying the confidence cutoff, so this holdout did not demonstrate that confidence prevented a candidate-level error.

## Frozen selection and held-out cases

Calibration used only the earlier source-support study's 48 cases and two grouped repeats. The selector tested thresholds from 0.00 through 1.00 in steps of 0.01. It required zero unsafe candidates bypassed in either repeat, maximized supported candidates bypassed in both repeats, and chose the highest tied threshold. With no qualifying supported candidate, the policy would reject all bypasses.

The selected threshold was **0.99**. The numeric filter left only two supported calibration candidates eligible, and both bypassed in both repeats. The selection artifact, code, and held-out fixture were frozen before live calls. The threshold was not retuned after observing the holdout.

The new fixture contains 64 candidates and 128 assertions: 24 wholly supported candidates, including 16 with multiple assertions, and 40 unsafe candidates. Six supported and six unsafe candidates exercise explicit quantities. A separate agent authored the fixture without reading the policy or results. A blind same-family annotator disagreed on one assertion's label, contradicted versus insufficient, with no disagreement about whether any candidate was wholly supported. Original labels remain unchanged.

A candidate can bypass only when every indexed assertion completes, every label is supported, and the minimum reported confidence reaches the threshold. The primary policy also defers explicit numeric tokens in candidate text, claims, or original sources. Gold labels and categories never enter provider input. Eligibility ignores fixture identifiers and categories; source and assertion identifiers remain available for evidence binding. The confidence-only ablation removes the numeric filter while retaining the same frozen threshold.

## Held-out results

Each arm evaluated 64 candidates twice. Repeats share evidence and are not independent safety samples.

| Measurement | Grouped, numeric filter | Grouped, confidence only | Singleton, numeric filter | Singleton, confidence only |
| --- | ---: | ---: | ---: | ---: |
| Bypassed candidate evaluations | 33/128 | 35/128 | 34/128 | 36/128 |
| Unsafe bypasses | 0/80 | 0/80 | 0/80 | 0/80 |
| Supported candidate coverage | 33/48 (68.8%) | 35/48 (72.9%) | 34/48 (70.8%) | 36/48 (75.0%) |
| Distinct supported candidates bypassed both repeats | 16/24 | 17/24 | 16/24 | 17/24 |
| Supported multi-assertion evaluations bypassed | 20/32 | 22/32 | 20/32 | 22/32 |
| Distinct supported multi-assertion candidates bypassed both repeats | 10/16 | 11/16 | 10/16 | 11/16 |
| Bypasses by repeat | 16, 17 | 17, 18 | 17, 17 | 18, 18 |

The primary policy retained 95 checks: 80 had a non-supported assertion, 12 had numeric text, and three fell below the confidence threshold. Reasons reflect that evaluation order; numeric candidates with an unsupported assertion are counted under unsupported.

Both geometries labeled 246/256 assertions correctly. Each falsely supported four unsupported assertion evaluations, covering two arithmetic claims across both repeats: four tickets at seven credits allegedly totaling 27, and five packets of six masks allegedly containing thirty-two masks. Other non-supported assertions kept both candidates from clearing even without the numeric filter. Candidate-level success therefore conceals arithmetic failures. These false-support answers carried reported confidence from 0.52 to 0.91, below the frozen threshold but higher than some errors in the calibration study.

The numeric filter detects Unicode digits and a fixed list of number words. It is conservative lexical deferral, not arithmetic verification: it can reject harmless numerical context and miss implicit quantities. Provider confidence is not a calibrated probability. Neither mechanism grants publication authority.

## Cost implications

The grouped arm made 128 calls; the singleton arm made 256. Grouped cost was $0.007106484 versus $0.013163556, a 46.0% reduction for this source-support workload. The ablations reuse the same calls and incur no additional provider cost. Every scheduled call completed with observed cost, tokens, and attempt count. Failed or fallback work would remain charged rather than being replaced or excluded.

For the primary policy, dividing all grouped Jev cost by 33 bypasses gives a conditional break-even source-support check cost of **$0.000215348**. Savings would require the avoided source-support work to cost more than that, assuming equivalent downstream behavior. The experiment did not execute an expensive fallback critic or measure marginal support-check cost, so actual savings remain unmeasured. The existing critic also checks scope and causality and supplies correction findings; a source-support bypass does not replace those duties.

The next integration step is a paired evaluation of the source-support component with actual fallback costs and downstream correction outcomes recorded separately. Keep the full critic active while establishing whether that component can be omitted without losing findings. Numerical work continues to require deterministic validation or fallback.

## Verification and retained evidence

The implementation passed 193 tests, lint, and typecheck on the devbox. An independent implementation reviewer passed 39 focused tests. Root spot-checked 13 tests and reran lint and typecheck. Calls, predictions, and summary replayed byte-for-byte without provider calls. A separate auditor reconstructed all 384 held-out and 256 calibration receipts, including the threshold grid, accounting, and routing outcomes. Root reproduced the audit and reconciled every reported method against its independent metrics. Review and blind annotation used the same model family, not human or independent-family validation.

The compact evidence directory is `results/support-fallback-2026-09-19/`. It includes the selection grid and bindings, calibration metrics, held-out manifest and schedule, call accounting, predictions, evaluation, and annotation comparison. Full requests, raw receipts, and execution logs remain at:

```text
stef-gradial-com-main:/home/dev/dev/eval-runs/jev-support-fallback-20260919
```

The inherited runner's immutable `summary.json` contains a stale limitations sentence saying the fixture has only mixed-label multi-assertion candidates. That sentence describes the previous fixture and does not apply here. The current fixture and fallback evaluation establish 16 supported multi-assertion candidates. The original output is retained unchanged for exact replay.

Calibration explicitly allowlists the earlier executed runner hash because that study predates the replay-integrity repair. The current checker revalidates all historical raw receipts. The allowance does not relax prompt, product, route, schedule, or receipt integrity. Held-out evaluation revalidates the frozen selection and rejects changed inputs or overlapping case identities and normalized evidence content; the overlap check does not detect paraphrases.

Use fresh output directories. Preparation and replay make no provider calls. Live runs require the dedicated Decisions credential.

```sh
moon run root:jev-support-study -- \
  --cases benchmarks/jev_revalidation/support_fallback_holdout.json \
  --output-dir /absolute/prepared --repeats 2 --seed 1729 --concurrency 8
moon run root:jev-support-fallback -- calibrate \
  --cases benchmarks/jev_revalidation/support_cases.json \
  --run /absolute/calibration-live --out /absolute/selection \
  --holdout-cases-sha 3c0fa1d0050751f8ea96a84f38e8d5aa5daaf04527bbe3d3c6006430957f7b08 \
  --historical-runner-sha 8020c00ba15f487695961ffa5ad8761d015a52b331ef6e3ad78c66c44a37f96a
moon run root:jev-support-fallback -- evaluate \
  --cases benchmarks/jev_revalidation/support_fallback_holdout.json \
  --run /absolute/heldout-live --selection /absolute/selection/selection.json \
  --out /absolute/evaluation
moon run root:jev-revalidation-test root:jev-revalidation-lint \
  root:jev-revalidation-typecheck
```

The archived selection contains absolute calibration paths on the devbox. Reproduction elsewhere requires rebuilding selection from the same unchanged calibration receipts and code; record the resulting path-dependent selection hash separately.
