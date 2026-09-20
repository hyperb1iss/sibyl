# Quality/speed semantic interpretation review

The candidate-level flag actions are justified in all 80 flagged responses, but the findings are not uniformly correct. The review found one unsupported extra concern and 13 instances where a valid unsupportedness concern was given an overly strong factual-contradiction basis. These observations do not change gold labels or the preregistered scores. The experiment still fails its speed target and does not qualify production activation.

Coverage: every raw finding in all 80 flag responses across 20 distinct candidates, both arms, and both repeats was read against the candidate and relevant original source text. There are 81 findings, comprising 41 baseline findings and 40 routed findings. Seventy-nine responses contain one finding; one baseline response contains two. Repeated paraphrases of the same concern are correlated observations, not independent evidence of semantic correctness. The reviewer is a native same-family agent who previously annotated this cohort before seeing author gold or results.

## Unsupported additional finding

The baseline call case-13-repeat-0-critic (qs-014, repeat 0) correctly flags the main claim that the Dovetail clamp works regardless of its liner. The same response adds finding index 1 against /claim_records/0/content: “The lined test held the panel without slipping.”

The extra finding says the subclaim omits the liner dependency and fails to name the record. The word “lined” preserves the condition, and the surrounding candidate identifies the clamp and test. The archive scope establishes distinct records; it does not require every subclaim to repeat its object's name. Both the original fixture and the frozen blind annotation label this subclaim supported. The additional concern is unnecessary and unsupported. The main contradiction remains real, so the candidate-level flag action and its existing score remain valid. The other three responses for this case flag only the defective main claim.

## Missing evidence is not factual contradiction

Thirteen findings use factual_contradiction where the evidence instead leaves the claim unsupported or ambiguous:

- The missing color annex case, qs-027: all four responses. No available color evidence does not establish that the mural was not blue.
- The unresolved restoration referent, qs-030: the routed repeat-1 response. The source does not identify the carved box, but does not establish that the box was not restored.
- The missing evening door log, qs-031: all four responses. No evidence about that interval does not establish the door was unlocked. Several rationales explicitly claim the gap is contradiction rather than underdetermination, which is incorrect.
- The instruction-only weather-vane record, qs-032: all four responses. The injected instruction supplies no directional evidence and no recorded inspection; it does not establish the opposite vane direction.

Every one of these responses still identifies a relevant, supported concern about asserting an unresolved fact. Removing an unsupported assertion from a source-bound candidate, or qualifying its uncertainty, can be justified without proving it false. The criticism of the basis must not be converted into an assertion that the full candidate should have been accepted. Nor should a future consumer treat these factual_contradiction tags as verified evidence for the opposite world state.

The other three restoration responses and all four unresolved-name responses use misleading_certainty, which fits the evidence better. All four conflicting-hatch responses, qs-029, cite both s0 and s1 and explicitly preserve the unresolved conflict rather than selecting a winning source. The conflict criticism is justified and fits the acceptable-action rubric frozen before calls.

## Remaining findings and interpretation

Across qs-013 through qs-026, the primary concerns correctly identify the documented contradiction, missing condition, unsupported causality or generalization, arithmetic discrepancy, instruction/evidence confusion, scope substitution, superseded record, or broken-seal subclaim. The two count objections are consistent with deterministic arithmetic: six times eight is forty-eight; three times fourteen plus five is forty-seven. No other wholly unsupported core concern was found. Some rationales loosely call an unsupported generalization or causal inference a contradiction, but their typed basis and central concern remain appropriately bounded; the 13 cases above are the clearer typed-basis defect.

Perfect rubric-level accept/flag decisions therefore do not establish perfect critique quality, successful correction generation, or downstream task quality. The recorded output discrepancies also do not demonstrate that Jev improved finding quality: both arms use the same critic, no finding-quality improvement was preregistered, and the one extra baseline concern is one repeated-case observation.

The narrower conclusion is useful: on this frozen synthetic cohort, the routed path retained candidate-level acceptable decisions while reducing calls/cost. The path did not meet its speed target. Avoiding 21 critic calls cannot substitute for timing the 43 sequential fallbacks, and an affordability result must not be presented as a FAST result. No threshold, labels, citations, or outputs were repaired after execution.

The full per-finding review is retained in semantic-findings-review.json. The source rows SHA256 is 80ebcb053977532cfb09f55d51407b79ef79c6abf3a40c3b07de267b56104105. Mechanical bindings, costs, and timing are independently audited in the separate raw-receipt lane; this document is the semantic interpretation review. No paid calls or production mutations were performed by this reviewer.
