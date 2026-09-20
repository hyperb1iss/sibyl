# Full-critic comparison for frozen Jev bypass decisions

The cost difference warrants further work, but the proposed bypass did not qualify. All 128 grouped Jev calls in the preceding study cost $0.007106484. Running the complete critic on the 33 decisions that policy would skip cost $0.469620. The critic returned 24 valid no-findings results and nine abstentions. No critic calls have been removed from production.

The comparison exposed a practical boundary: source support is part of one critic call that also checks conditions, causality, counts, and universal claims. Skipping only the support check saves nothing in the current implementation. A useful Jev integration must avoid an entire call while preserving the critic's other duties.

## Observed outcomes

The frozen plan selected every grouped, numerically guarded bypass from the previous holdout: 33 candidate/repeat decisions across 17 distinct candidates. The plan retained the 0.99 threshold without refitting. The remaining 95 candidate evaluations were not rerun through the critic; the counterfactual assumes their critic and correction behavior would remain unchanged.

| Measurement | Result |
| --- | ---: |
| Selected critic calls | 33 |
| Distinct selected candidates | 17 |
| HTTP successes with known cost and token usage | 33 |
| Valid no-findings results | 24 |
| Affirmative no-concern prose placed in the abstention field | 7 |
| Finding-bearing responses rejected for invalid citations | 2 |
| Input / output tokens | 78,149 / 3,155 |
| OpenRouter-reported critic cost | $0.469620 |
| All prior grouped Jev routing cost | $0.007106484 |
| Arithmetic difference, unqualified | $0.462513516 |
| Qualified conditional avoided cost | Not established |

The first repeat had 11 no-findings results and five abstentions; the second had 13 and four. Ten distinct candidates returned no findings in both repeats. Four split between no findings and abstention. Two abstained in both repeats, and one candidate was selected only once and abstained. Repeated decisions are not independent candidate samples.

## Why qualification failed

Seven responses returned an empty findings list but filled `abstention_reason` with prose affirming that the candidate was supported and no concern warranted a finding. The product treats any nonempty abstention reason as an abstention. Those responses are not equivalent to the product's successful no-findings outcome, even when their prose appears affirmative.

The other two responses proposed a `misleading_certainty` finding on both repeats of the same candidate. The evidence says Pema reported that a rehearsal felt rushed; the candidate says Pema described the rehearsal as rushed. The critic claimed the candidate dropped attribution and subjective framing. Both responses copied the correct claim hash but cited the source identifier instead of the supplied citation key (`s0`), so product validation rejected the finding and returned abstention.

An independent reviewer judged the concern unpersuasive because the candidate already attributes the description to Pema. The earlier blind annotation also labeled the assertion supported. That interpretation does not erase the raw findings, establish human gold, or change the preregistered failure. All nine abstentions remain failures for this comparison. Neither automatic citation repair nor reinterpretation of abstention prose was used.

The raw responses matter: mechanically rejected findings disappear from the resulting submission. Counting only accepted submissions would incorrectly report that the critic proposed no findings at all. The retained audit distinguishes 31 empty raw findings lists from two invalid-citation findings.

## What the economics establish

The measured costs show that routing is inexpensive relative to the critic calls being considered for removal. The difference of $0.462513516 subtracts every grouped Jev routing call from all 33 critic calls, including the nine abstentions. The difference remains unqualified potential because the required outcome agreement failed. Actual savings are zero while the critic continues to run.

The experiment does not measure unchanged fallback or correction costs, complete evaluation cost, or downstream answer quality. It therefore reports no whole-run savings percentage. Historical `ExtractionUsage.cost_usd` values use SDK pricing applied to observed tokens; this experiment separately retains OpenRouter's original `usage.cost` values rather than presenting SDK estimates as provider billing.

The next useful change is to clarify the critic's output contract: a completed review with no concerns must return an empty findings list and a null abstention reason, while evidence references must use citation-map keys. A separately frozen paired run must test that clarification without weakening validation or relabeling these results. Jev still needs fresh difficult cases and representative procedure/evidence sizes before any critic bypass is activated.

## Controls and limits

The new harness reconstructs the existing product's complete `PreparedMemoryValidation` input and checks its semantic state against the retained Jev request. It sends the unchanged product critic prompt and `CriticOutput` schema, then passes returned output through `run_memory_validation` to verify claim hashes, citation references, and status semantics. Gold labels and rationales never enter provider input.

Calls used OpenRouter's `anthropic/claude-opus-5` model, the normal `anthropic` provider endpoint only, no provider fallback or retries, a forced `CriticOutput` function, and a 4,096-token output allowance. Temperature was omitted because the endpoint did not advertise support. The checked [endpoint catalog](https://openrouter.ai/api/v1/models/anthropic/claude-opus-5/endpoints) is retained with the evidence. The transport, tool framing, retry policy, and generation settings differ from direct Anthropic production, so this is a comparison using the product prompt/schema and validator, not a reproduction of the complete production transport.

Selected prompts were 2,194 to 3,551 UTF-8 bytes before tool-schema overhead. This retrospective sample consists of already-inspected synthetic plain reflections that Jev passed. It has no independently adjudicated gold for all critic duties, no unselected critic control arm, and no representative long-context procedure cohort. Critic agreement cannot establish factual truth, recall quality, or safe production routing.

Raw response bytes were persisted before interpretation. Failed or invalid responses retain known spending; missing usage remains unknown. Output directories are exclusive, and replay verifies every request and dispatch binding before creating output. No private screen48 evidence, production mutation, threshold adjustment, or replacement call was used.

## Verification and reproduction

The devbox gates passed 231 tests, lint, and typecheck. Independent code review ran 38 focused tests; root spot-checked 15 and reran lint and typecheck. A separate audit reconstructed selection, original evidence bindings, raw output schemas, mechanical findings, and costs without importing the scorer or reading its summary. Root reproduced that audit. Calls, requests, and summary replayed byte-for-byte without provider calls. Reviewers share the model family, so their agreement is not independent-family validation.

Committed evidence lives under `results/critic-pair-2026-09-19/`. Complete raw receipts, dispatch records, and logs remain at:

```text
stef-gradial-com-main:/home/dev/dev/eval-runs/jev-critic-pair-20260919
```

Run preparation with no provider key; add `--live` only for a separately authorized execution. Replay uses the frozen plan and original code. Each output directory must be new.

```sh
moon run root:jev-critic-pair-plan -- \
  --cases benchmarks/jev_revalidation/support_fallback_holdout.json \
  --run /absolute/previous-support-live \
  --selection /absolute/previous-selection/selection.json \
  --out /absolute/plan.json
moon run root:jev-critic-pair -- --plan /absolute/plan.json \
  --output-dir /absolute/prepared
moon run root:jev-critic-pair -- --plan /absolute/plan.json \
  --replay /absolute/live --output-dir /absolute/replay
```

Plan validation reopens the original calibration and holdout receipts. Archived absolute paths identify the devbox evidence; reproduction elsewhere requires rebuilding the plan against identical relocated inputs and recording its new path-dependent hash.
