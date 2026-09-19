# Claim revalidation results: 19 September 2026

The revised decomposed Choice prompt is the next candidate for shadow evaluation. On a fresh synthetic set, it matched direct Choice's 87.5% action accuracy and avoided the direct arm's false retirement. Neither arm qualifies for automatic forgetting: temporary changes remain ambiguous, one partial-evidence case caused repeated false retirement proposals, and live responses sometimes fail Sibyl's strict probability-sum policy.

All retirement actions below are simulated. No memory was changed. The experiments ran on `stef-gradial-com-main` against `typesafe/jev-1.13`, using the existing pinned OpenRouter Decisions route. These results test semantic decisions and a deterministic authority/time policy, not downstream answer quality. Expected actions apply that same policy to the annotated relation, so action accuracy is not an independent end-to-end quality measure.

## First experiment: 84 synthetic pairs

Each arm ran twice at each batch size. Every model row below contains 168 observations of 84 unique cases, including eight unique eligible retirement cases. Failed requests count as incorrect.

| Batch size | Prompt arm | Relation accuracy | Action accuracy | Eligible retirements captured | False retirements | Failed cases |
| ---: | --- | ---: | ---: | ---: | ---: | ---: |
| 1 | Direct v1 | 149/168 (88.7%) | 157/168 (93.5%) | 14/16 | 0 | 1 |
| 1 | Decomposed v1 | 98/168 (58.3%) | 128/168 (76.2%) | 6/16 | 0 | 1 |
| 7 | Direct v1 | 131/168 (78.0%) | 140/168 (83.3%) | 15/16 | 0 | 28 |
| 7 | Decomposed v1 | 89/168 (53.0%) | 115/168 (68.5%) | 9/16 | 0 | 21 |

Direct v1 beat decomposed v1 in both repeats and both batch geometries. The decomposed scope question frequently interpreted a changed value as a different condition, allowing incompatible statements to coexist. Both singleton arms missed a current health-state contradiction during an incident.

The production pairwise heuristic returned no signal on all 84 cases. Its retain action matched 52/84 expected actions and captured no eligible retirements. The fixture does not contain the explicit prior-source IDs required by its supersession heuristic. This measures a coverage gap on these natural-language pairs; it is not evidence of a deployed baseline's retirement accuracy.

## Prospective prompt test: 56 fresh synthetic pairs

The v2 wording was frozen before its author opened the second dataset. It clarifies that a changed property value is not a different condition, and that an incident can contradict a current-state claim without revoking a durable rule. The original v1 prompt, labels, composition rule, and policy were preserved. V2 adds no confidence threshold. Both arms receive the clarification, while only decomposition also receives revised scope/effect option descriptions; this is a comparison of complete prompts, not an isolated test of decomposition itself.

Each model row below contains two repeats of 56 unique cases, including 11 eligible retirement cases. Both versions used singleton requests.

| Prompt arm | Relation accuracy | Action accuracy | Eligible retirements captured | False retirements | Failed cases |
| --- | ---: | ---: | ---: | ---: | ---: |
| Direct v1 | 88/112 (78.6%) | 95/112 (84.8%) | 16/22 | 2 | 3 |
| Decomposed v1 | 70/112 (62.5%) | 86/112 (76.8%) | 8/22 | 0 | 0 |
| Direct v2 | 89/112 (79.5%) | 98/112 (87.5%) | 16/22 | 2 | 2 |
| Decomposed v2 | 84/112 (75.0%) | 98/112 (87.5%) | 16/22 | 0 | 1 |

The scope clarification improved decomposition on new cases. Its action accuracy rose 10.7 percentage points, with seven unique cases improving in both repeats and one regressing in both. The result does not establish that decomposition generally beats direct classification. Direct v2 retained higher relation accuracy and used fewer tokens, while decomposition avoided the observed false retirement.

The false retirement is concrete. The memory says a woven panel is entirely linen. The event confirms linen in one sampled corner and leaves the unsampled repairs unidentified. Both direct versions labeled that incomplete evidence as contradiction and proposed retirement in both repeats (`holdout_022`). Both independent annotations called for review. Missing evidence for part of a claim does not refute the claim.

All arms missed three temporary-state changes under the original labels: an access route blocked during repairs, a temporarily closed bridge deck, and a shuttle detour. These cases also divided the annotators. A future study needs an explicit distinction between current state, standing rule, and validity interval. Prompt wording alone did not resolve that distinction here.

## Annotation sensitivity

Both datasets were authored by a separate agent without reading the prompts or Jev outputs. A second model annotated the cases without the original labels or Jev outputs. These are agent-authored diagnostic labels, not human gold or a public benchmark.

On the first set, the annotators agreed on 80/84 relations and 83/84 actions; 11 cases were marked ambiguous. Using all alternate labels preserves direct v1's lead. No retirement label changed.

On the second set, agreement was 52/56 relations and 53/56 actions; 16 cases were marked ambiguous. The three disputed retirement cases are the temporary-state changes above. Using all alternate labels yields 103/112 action accuracy for direct v2 and 104/112 for decomposed v2. Both capture 16/16 retirement observations under those labels, but direct's two false retirements remain. The original tables are unchanged; alternate scoring is sensitivity analysis, not a replacement of inconvenient labels.

## Transport and cost

The main experiments made 832 calls. Fifteen returned responses rejected by the strict schema, invalidating 57 case observations. Seven failed batch requests account for 49 of those cases. Request latency reflects the chosen geometry; it is not end-to-end search latency.

A separate diagnostic reran the nine failed requests from the first experiment, preserving the original failures. Eight diagnostic responses passed; one reproduced a distribution summing to 0.99 (`c3.relation`: 0.81, 0.09, 0.05, 0.03, 0.01, 0, 0). The [Choice documentation](https://docs.typesafe.ai/primitives/choice) says probabilities sum to one. The adapter rejected that response under its strict normalization policy. A subsequent check of the [official TypeSafe schema](https://github.com/typesafe-ai/typesafe-sdk-python/blob/2ce5c65f13646cab6e6f782328194c9d85f3300a/src/typesafe_sdk/_schemas/models.py#L25-L29) found an approximate-sum contract without a numeric tolerance. The rejection alone does not establish a provider defect. No normalization or relaxed tolerance was added, and the exact causes of the other original failures remain unconfirmed.

| Run | Calls | Reported Jev cost |
| --- | ---: | ---: |
| First set, both batch sizes | 384 | $0.021758352 |
| Fresh set, both prompt versions | 448 | $0.018735360 |
| Separate response diagnostics | 9 | $0.001355676 |
| Total | 841 | $0.041849388 |

Every call had a reported cost. These figures exclude subscription-backed annotation/review work and earlier transport qualification calls. Replays made no provider calls and do not add cost. Full token counts and latency distributions are in the accompanying summary and call records.

## Engineering decision

Keep J11 in shadow mode and use v2 decomposition as the next candidate for a source-backed review path. Preserve the old evidence and expose proposed changes for review. The [Invalidate project](https://github.com/chopratejas/invalidate/tree/d6ade60108b8064bafaee425fd8f9e78683dbd82) motivates event-triggered revalidation and isolated rechecks; these experiments use Choice and do not reproduce its Noul thresholds.

Before automatic retirement can be considered, qualification must cover partial claims and temporary validity with independently adjudicated cases, resolve the provider probability contract, and measure historical-evidence loss plus downstream answer quality. Zero observed false retirements for v2 decomposition covers only 45 unique non-retirement cases in the fresh set. Repeats do not turn them into independent safety evidence.

## Reproduction and receipts

The harness, immutable v1 prompt, v2 prompt, and both datasets are committed alongside this report. The compact evidence in `results/2026-09-19/` includes manifests, pooled/per-repeat summaries, per-case predictions, call accounting, and blind annotations. Full request/observation receipts, diagnostic raw-response captures, review transcripts, and logs remain at:

```text
stef-gradial-com-main:/home/dev/dev/eval-runs/jev-revalidation-20260919
```

The summary records SHA-256 hashes of the original run artifacts. Each manifest binds the dataset, prompt, runner, route policy, and git state. V2 additionally binds the original prompt module it imports. The preregistration records distinguish the frozen wording from a subsequent typed-object construction fix found by toy tests before live execution.

The devbox gates passed 43 tests, lint, and typecheck. Offline replay reproduced the first singleton run and fresh v2 run's predictions and summaries exactly, including original failures and original-call accounting. Independent static reviews and a separate read-only results audit supplement those executed checks; they do not establish semantic correctness.
