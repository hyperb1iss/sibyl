# Claim-kind context experiment

Supplying the correct claim kind improved temporary-overlay recall on fresh cases from 17/24 to 20/24 (70.8% to 83.3%). Overall policy agreement improved from 87/96 to 89/96 (90.6% to 92.7%). The same prompt with an unknown kind is the controlled comparison: only the supplied state value differs. The gain supports providing reliable interpretation context, but its size on fresh cases is modest and temporary changes remain imperfect.

The experiment ran 624 synthetic calls on `stef-gradial-com-main`, costing $0.033456780. Every arm made zero false retirement or overlay proposals under the supplied policy metadata. No production memory was read or changed.

## Fresh cases

A separate agent authored 48 cases without reading the prompts or results. Eight matched pairs share identical evidence and differ in claim kind, producing 40 evidence clusters. Each case ran twice. The corpus contains 12 expected overlays, 20 retentions, eight retirements, and eight reviews. Neither the corpus nor the prompt was tuned after observing these results.

| Arm | Correct dispositions | Temporary overlays captured | Permanent replacements captured | Failed measurements |
| --- | ---: | ---: | ---: | ---: |
| Frozen source prompt, v3 | 85/96 (88.5%) | 18/24 (75.0%) | 16/16 | 1 |
| Kind-aware prompt, unknown kind | 87/96 (90.6%) | 17/24 (70.8%) | 16/16 | 1 |
| Same prompt, correct supplied kind | 89/96 (92.7%) | 20/24 (83.3%) | 16/16 | 3 |

On the 12 standing-rule cases with expected retention, v3 and unknown kind retained 24/24 observations. Correct kind retained 23/24; one response failed validation. The broader standing-rule temporary subset has 13 cases because it also includes the missing-end-time review case. Its raw retention rate is not a correctness rate.

The correct-kind arm scored 45/48 and 44/48 across repeats; the unknown-kind arm scored 43/48 and 44/48. Requiring every member of an evidence cluster to be correct gives 37/40 and 36/40 for the correct-kind arm, versus 35/40 and 36/40 for unknown kind. These repeats and matched cases are not independent safety samples.

Among observations completed in both v4 arms, correct kind produced three improvements and one regression. That regression is the disputed missing-end-time case. Comparisons involving failed measurements add one improvement and one regression, leaving a net gain of two observations overall.

The correct-kind arm still misread a temporarily muted clock as compatible with the tracked audible state in both repeats. Two other overlay observations failed response validation. A standing-rule case with an exception lacking an end time also exposed an annotation disagreement, described below. The remaining failed measurement concerns a standing-rule exception.

## Diagnostic replication

The earlier 56-case corpus provides a diagnostic comparison, separate from the fresh test:

| Arm | Correct dispositions | Temporary overlays captured | Permanent replacements captured | Failed measurements |
| --- | ---: | ---: | ---: | ---: |
| Frozen source prompt, v3 | 95/112 (84.8%) | 13/28 (46.4%) | 28/28 | 5 |
| Kind-aware prompt, unknown kind | 100/112 (89.3%) | 19/28 (67.9%) | 26/28 | 4 |
| Same prompt, correct supplied kind | 107/112 (95.5%) | 27/28 (96.4%) | 27/28 | 3 |

The larger diagnostic gain did not transfer at the same size to fresh cases. The frozen control is a new run; it does not replace the earlier 98/112 result or erase its original failures. Added wording alone improved the diagnostic overlay score but slightly reduced it on the fresh set.

## Controlled inputs and limits

The new prompt delegates to the frozen source prompt and preserves its two questions, answer options, and relation/witness composition. It adds one constant explanation of claim kinds and one state field. The unknown-kind and correct-kind variants have identical question text and differ only in that field's value. The v3 comparison also changes wording and is reported separately.

Every arm receives the original passage. Dates, authority, validity intervals, gold labels, and rationales remain outside model input. All arms use the original oracle metadata for offline policy, even when the model receives unknown kind. The experiment therefore measures the benefit of correct supplied context, not the quality or cost of extracting that context.

The deterministic policy already protects temporary standing rules from retirement when metadata is complete. Zero false retirement proposals therefore does not establish that the model alone learned that distinction. Claim-kind extraction, incorrect-kind robustness, retrieval quality, and downstream answer quality remain unmeasured. Expected dispositions apply the same policy to authored semantic labels; policy agreement is not independent proof of correct memory behavior.

The three arm processes ran concurrently within each corpus, launched in a recorded seeded order. Their execution windows overlap. Each used singleton requests, two repeats, the same route and concurrency, and no replacement calls. The prompt and primary metrics were frozen before the prompt author opened the fresh corpus.

## Annotation sensitivity

A separate native agent annotated all 48 fresh cases without seeing gold labels, model prompts, or results. The annotator shared the model family and prior design context. Claude's session limit prevented the planned cross-family annotation and static review; their failed attempts remain in the evidence directory.

The annotator agreed with 47/48 dispositions and 35/48 source relations. Thirteen standing-rule exceptions were labeled compatible rather than conflicting; twelve differences leave the retain disposition unchanged. The remaining case, `kind_case_042`, lacks a bounded end time. Original gold routes its conflict to review; the alternate annotation treats the exception as compatible and retains the rule. Both interpretations are preserved. Scoring against the alternate dispositions gives v3 87/96, unknown kind 87/96, and correct kind 91/96. Original labels and headline denominators remain unchanged.

## Verification and evidence

The devbox passed 123 tests, lint, and typecheck through the standalone Moon experiment tasks. A separate same-family agent reviewed the implementation and independently ran 68 targeted tests; root spot-checked seven tests. The numerical audit independently reconstructed all 624 policy outcomes. All six runs replayed byte-for-byte for predictions and summaries. Failed measurements count as incorrect: 17 calls were rejected by the current response schema, with all cost fields observed and no retry replacement. Existing probability acceptance was not relaxed.

The committed evidence in `results/claim-kind-2026-09-19/` includes per-case outputs, original-call accounting, manifests, the blind annotation, and content hashes. Full request/observation receipts, launch schedules, prompt-freeze record, failed review attempts, and gate logs remain at:

```text
stef-gradial-com-main:/home/dev/dev/eval-runs/jev-claim-kind-20260919
```

The fresh corpus SHA-256 is `f600fb4f46a07556a4d8e1cb98ce203aacd28cfbae2b0afb6786a4994107f484`. The frozen v4 prompt SHA-256 is `3805f8a563888051a9b01891b08da7de121471ec74ec0e0d8718e186525f9857`.

## Reproduce

Use new output directories. Live calls require the dedicated Decisions credential. Run the three provider commands concurrently to preserve the overlapping-arm design, then analyze after all finish.

```sh
moon run root:jev-kind-study -- prepare \
  --cases benchmarks/jev_revalidation/kind_holdout.json --out /absolute/prepared
moon run root:jev-revalidation -- --cases /absolute/prepared/v3.json \
  --out /absolute/v3-run --prompt-version v3 --arms direct --repeats 2 --live
moon run root:jev-revalidation -- --cases /absolute/prepared/unknown.json \
  --out /absolute/unknown-run --prompt-version v4 --arms direct --repeats 2 --live
moon run root:jev-revalidation -- --cases /absolute/prepared/typed.json \
  --out /absolute/typed-run --prompt-version v4 --arms direct --repeats 2 --live
moon run root:jev-kind-study -- analyze \
  --cases benchmarks/jev_revalidation/kind_holdout.json \
  --v3-run /absolute/v3-run --unknown-run /absolute/unknown-run \
  --typed-run /absolute/typed-run --out /absolute/analysis
```

The next useful test is reliable claim-kind acquisition and deliberately incorrect-kind inputs. Further tuning against these cases would not answer whether the metadata can be trusted in actual retrieval and correction flows.
