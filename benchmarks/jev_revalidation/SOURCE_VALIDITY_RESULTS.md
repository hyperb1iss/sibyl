# Original-source and validity experiment

The original-source assessment plus typed validity policy scored 98/112 simulated dispositions (87.5%), captured all 28 eligible permanent replacements, and made no false retirement or overlay proposals. Requiring the earlier summary to agree reduced accuracy to 64/112 (57.1%) and missed 12 permanent replacements. A lossy summary should not veto stronger original evidence in the next shadow design.

These observations come from 56 new synthetic cases, repeated twice on `stef-gradial-com-main`. They do not qualify automatic forgetting. Only half the temporary changes were recognized, and claim kinds, source authority, and validity times were supplied by the fixture. No production memory was read or changed.

## Comparison

The summary pass used the frozen v2 decomposed prompt. A separate v3 request received the memory and original passage, excluding the summary. The v3 prompt asks for a relation and an explicit-counterevidence judgment; a proposed conflict without that judgment becomes uncertain. The source pass is isolated from the summary's text and answers, but uses the same Jev model and is not an independent model opinion.

The scorer applies four policies to the same recorded outputs. An overlay represents a temporary change during a bounded validity interval while preserving the original evidence; it is only a simulation label here.

| Policy | Correct dispositions | False retirements | Permanent replacements captured | Temporary overlays captured | Failed measurements |
| --- | ---: | ---: | ---: | ---: | ---: |
| Summary with previous policy | 38/112 (33.9%) | 47 | 16/28 | 0/28 | 2 |
| Summary with typed validity | 71/112 (63.4%) | 14 | 16/28 | 20/28 | 2 |
| Original source with typed validity | 98/112 (87.5%) | 0 | 28/28 | 14/28 | 3 |
| Summary gated by original source, typed validity | 64/112 (57.1%) | 0 | 16/28 | 7/28 | 4 |

The previous policy cannot produce an overlay, making it an intentionally incomplete control for this expanded task. That row uses Jev predictions; the actual pairwise reflection heuristic returned no signal and retained all 56 cases, matching 12/56 typed dispositions. Neither control describes production retirement behavior. Summary-only errors also include cases where a summary overstated or omitted the source evidence. The source comparison changes both the input and the prompt, so it does not isolate a causal benefit from source access alone.

The agreement gate prevents observed false retirements but also preserves summary omissions. Its review rate is 45.5%, versus 28.6% for source-only assessment. A successful summary retention needs no source result under that gate; a proposed retirement or overlay needs both results. Failed required measurements count as incorrect, not as successful review decisions.

## What improved and what did not

The source pass captured every permanent replacement in both repeats. Its zero false retirement count covers only 42 unique non-retirement cases. Repeated observations are not independent safety samples.

All 14 incorrect source outcomes were missed overlays across eight unique cases: eleven completed responses classified the temporary conflict as compatible, and three measurements failed. Temporary-state handling remains weak. For example, an original passage says headphones replace a listening station's speaker during a defined quiet-study interval. The model classifies the claims as compatible and retains the old state. Similar misses occur for a shutter lockout, a clock's trial setting, moved records, and a blocked passage. The supplied current-state type remains outside model input, so these results do not establish that the model received enough context to distinguish current status from a standing rule. Testing that distinction explicitly is the next diagnostic.

The typed policy keeps chronology and authority out of model discretion. Eligible conflicts require an authoritative, strictly newer event. Future or expired temporary events retain the original state; active bounded current-state changes propose overlays. Missing metadata routes eligible conflicts to review. Historical corrections also require review, preserving the evidence without treating the old assertion as established truth.

## Labels and amendment

A separate agent authored the corpus without reading the prompts or results. The prompt was frozen before its author opened the cases. The original corpus contained 14 cases per disposition. Inspection exposed an overly broad historical-retention rule, so two expected dispositions (`source_case_046` and `source_case_050`) changed from retain to review before any live calls. Their original labels remain in the fixture; the exact original file and amendment record remain in the evidence directory. Source texts and semantic labels did not change. This policy amendment was not blind validation.

The amended corpus has 14 retirement, 14 overlay, 16 review, and 12 retention cases. A second model independently agreed with all 56 summary relations and 55/56 source relations, marking ten cases ambiguous. The sole semantic disagreement is compatible versus unrelated, with the same retain disposition. Applying the amended policy to those alternate source labels leaves all disposition scores unchanged. The annotator used the earlier historical-retain rule; its raw disposition fields are preserved but are not presented as independent validation of the amended policy.

Expected dispositions apply the same policy to the source annotations. The reported accuracy therefore measures agreement with that policy, not independent downstream correctness, metadata extraction quality, retrieval coverage, or historical-answer preservation.

## Cost, failures, and checks

The study made 224 Jev calls and reported $0.012256440: $0.006974772 for summaries and $0.005281668 for sources. Every cost field was present. Five requests were rejected under the current response schema; no diagnostic retry replaced those observations. The four scored policies reuse those calls and do not multiply cost.

The devbox passed 96 tests, lint, and typecheck through the standalone experiment tasks. Those tasks are not part of the default aggregate checks. Corpus consistency was validated by preparation and analysis; the unit suite uses toy fixtures. Both input passes replayed exactly, preserving predictions, summaries, failures, and original-call accounting. Independent static review and separate numerical auditing supplement the executed checks. The lint task now stops when Ruff fails; a failing control verified that a later successful formatting check cannot mask the failure.

A correction to the earlier transport interpretation matters: the [official TypeSafe schema](https://github.com/typesafe-ai/typesafe-sdk-python/blob/2ce5c65f13646cab6e6f782328194c9d85f3300a/src/typesafe_sdk/_schemas/models.py#L25-L29) describes probability sums as approximate. It supplies no numerical precision or tolerance bound. The previously observed 0.99 vector establishes rejection by Sibyl's strict normalized-receipt policy, not a proven provider defect. Transport acceptance remains unchanged pending a documented normalization contract.

## Reproduce

Run the preparation, two provider passes, and analysis through Moon. Each output directory must be new. Live calls require the dedicated Decisions environment credential.

```sh
moon run root:jev-source-study -- prepare \
  --cases benchmarks/jev_revalidation/source_cases.json --out /absolute/prepared
moon run root:jev-revalidation -- --cases /absolute/prepared/summary.json \
  --out /absolute/summary-run --prompt-version v2 --arms decomposed --repeats 2 --live
moon run root:jev-revalidation -- --cases /absolute/prepared/source.json \
  --out /absolute/source-run --prompt-version v3 --arms direct --repeats 2 --live
moon run root:jev-source-study -- analyze \
  --cases benchmarks/jev_revalidation/source_cases.json \
  --summary-run /absolute/summary-run --source-run /absolute/source-run --out /absolute/analysis
```

The committed compact evidence is in `results/source-validity-2026-09-19/`. Full request/observation receipts, original corpus, amendment and prompt-freeze records, annotation outputs, review transcripts, and logs are retained at:

```text
stef-gradial-com-main:/home/dev/dev/eval-runs/jev-source-validity-20260919
```
