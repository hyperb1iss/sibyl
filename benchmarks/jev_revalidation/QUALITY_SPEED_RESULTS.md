# Jev validation quality and speed comparison

Jev made the cases it bypassed much faster, but the complete routed workload missed the speed target. On 32 fresh synthetic candidates run twice through each path, the baseline and routed paths accepted the same 24 supported candidate evaluations and withheld all 40 unsafe or insufficient evaluations. Median validation service time remained about 4.46 seconds; the routed p95 increased from 6.93 to 7.82 seconds. The experiment does not qualify a production bypass.

## Measured outcomes

Both primary arms used the same clarified critic prompt and unchanged output schema. The routed arm first called grouped Jev at the previously selected 0.99 threshold with the unchanged numeric guard, then called the complete critic sequentially on fallback. Every candidate ran through both arms. No baseline response was reused by the routed arm.

| Measurement | Complete critic | Jev then critic on fallback |
| --- | ---: | ---: |
| Candidate evaluations | 64 | 64 |
| Supported candidates accepted | 24 / 24 | 24 / 24 |
| Unsafe or insufficient candidates accepted | 0 / 40 | 0 / 40 |
| Rubric-acceptable actions | 64 / 64 | 64 / 64 |
| Strict original action labels matched | 52 / 64 | 52 / 64 |
| Supported assertions available after acceptance | 42 | 42 |
| Unsafe assertions exposed | 0 | 0 |
| Median validation service time | 4.464 s | 4.456 s |
| Mean validation service time | 4.348 s | 3.796 s |
| p95 validation service time | 6.932 s | 7.823 s |
| Reported provider cost | $1.773265 | $1.236339356 |
| Critic calls | 64 | 43 |
| Jev calls | 0 | 64 |

The observed mean improved 12.7%, while the median improved only 0.2% and p95 worsened 12.9%. The routed arm cost 30.3% less. Both arms were actually executed, spending $3.009604356 altogether; the arm difference is not a production savings claim.

The frozen engineering target required at least 20% lower median service time with no worse p95, no unsafe acceptance, no paired loss of supported candidates, no regression in rubric-acceptable actions, no final execution failures, complete cost accounting, and no cost regression. Both speed gates failed. Every other gate passed on this corpus. The target remains failed; neither cheaper calls nor a better mean rescues it.

## Where the time went

Jev bypassed 21 of 64 routed evaluations. Those paths took a median of 149 ms, versus 1.813 seconds for their paired baseline critic paths. The median paired reduction was 1.606 seconds.

The remaining 43 evaluations required the critic. Those routed paths took a median of 5.332 seconds, versus 4.890 seconds for their paired baseline paths. The median paired increase was 391 ms. The routed design adds a sequential decision call before fallback; the measured increase is consistent with that overhead, but separate critic-call variability also contributes. The result does not isolate a fixed routing penalty.

| Evidence bundle | Baseline median / p95 | Routed median / p95 | Bypasses |
| --- | ---: | ---: | ---: |
| Around 1 KB | 4.464 / 6.739 s | 4.696 / 7.132 s | 11 / 40 |
| Around 8 KB | 4.763 / 6.932 s | 4.318 / 7.823 s | 6 / 16 |
| Around 24 KB | 3.459 / 11.107 s | 2.563 / 11.938 s | 4 / 8 |

The longer-evidence strata are small. Repeats are correlated, and p95 is descriptive. The prospective corpus deliberately contains many difficult or unsupported candidates; its bypass fraction is not an estimate of production traffic. Faster supported cases are a useful signal, but typical and tail latency need measurement on representative workloads.

## What quality means here

The fixture author labeled 12 candidates acceptable, 14 requiring a finding, and six insufficient for a definitive assessment. A separate reviewer labeled the evidence before seeing model output. All 46 source-support assertion labels agreed. Strict action labels agreed on 31 of 32 candidates; the disagreement concerned whether conflicting inspections warranted a finding or an abstention.

Before calls, the six insufficient cases were assigned a rubric accepting either a valid concern or a genuine abstention. Accepting those candidates remained a failure. Both original labels and blind annotations were retained. Both live arms returned findings for all six cases on both repeats, explaining their 52/64 strict score and 64/64 rubric score. Mechanical rejection or provider failure never earns abstention credit.

Evidence availability is deterministic accept/withhold accounting. The experiment did not run a downstream question-answering task, retrieval, correction generation, or publication. Matching action labels does not establish perfect critique quality. Independent review inspected all 81 findings across 80 flagged responses. Every candidate-level flag had a justified core concern, but one baseline response added an unjustified finding against a supported subclaim. Thirteen findings mislabeled missing or ambiguous evidence as factual contradiction even though withholding the claim was justified. Original scores remain unchanged; the finding ledger records these defects. The sample does not establish a Jev advantage in critique quality.

## Critic contract change

The product instructions now explicitly require a null abstention reason after a completed assessment and citation-map keys in evidence references. The output schema, payload version, and mechanical validator are unchanged. Existing bindings remain compatible; changed prompt bytes produce a different input hash. Affirmative prose in the abstention field is still an abstention, and invalid citations are still rejected.

No malformed abstentions or invalid-citation submissions occurred in this run. Both arms use the clarified prompt, so this experiment cannot attribute the improvement over the earlier selected sample to that change. The earlier failed experiment remains unchanged.

## Controls, verification, and limits

The run used `stef-gradial-com-main`, a frozen seeded paired schedule, eight concurrent candidate paths, two repeats, synthetic evidence only, and no retries or replacement calls. The 171 provider calls completed with known spending. Original response bytes, dispatch records, path receipts, code hashes, gold annotations, and the pre-call protocol were retained. Gold and rationales never entered provider inputs.

Service timing starts with prepared evidence and ends at the final action. It includes the actual decision request, any sequential fallback, mechanical interpretation, and raw receipt writes. Evidence acquisition is outside this boundary. Harness queue delay and queue-inclusive totals are recorded separately; neither is an interactive recall benchmark. OpenRouter forced-tool transport and generation settings differ from the direct Anthropic production runtime.

Devbox verification passed 257 benchmark tests, lint, and typecheck. Product checks passed 103 focused tests with one existing native test skipped, plus lint and typecheck. An independent reviewer passed six product tests and 26 harness tests. Root reran 15 adversarial benchmark checks and six product checks. Raw timing and accounting received a separate audit; rows, summary, and schedule replayed byte-for-byte without provider calls. Reviewers share the model family.

The implementation deliberately keeps production qualification false. A fast individual classifier is insufficient evidence for a faster product. The next useful comparison should test a design that avoids paying for serial routing on the latency-sensitive fallback path, while retaining the quality gates and measuring the intended workload. Background preparation of source-bound decisions is one candidate; its freshness, cache misses, and time until memory becomes usable must be measured rather than assumed.

Committed compact evidence lives under `results/quality-speed-2026-09-20/`. Complete raw receipts, paired requests, and logs remain at:

```text
stef-gradial-com-main:/home/dev/dev/eval-runs/jev-quality-speed-20260920
```

Reproduction requires the frozen code and inputs. Preparation makes no provider calls. Live execution requires the reviewed manifest; replay validates the original requests and raw artifacts before creating its destination.

```sh
moon run root:jev-quality-speed -- \
  --cases benchmarks/jev_revalidation/quality_speed_cases.json \
  --output-dir /absolute/prepared
moon run root:jev-quality-speed -- \
  --cases benchmarks/jev_revalidation/quality_speed_cases.json \
  --freeze /absolute/prepared/manifest.json \
  --replay /absolute/live --output-dir /absolute/replay
```
