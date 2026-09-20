# Fresh verdict-study fixture review

PASS for source truth, exact assertion targeting and scoring coherence on the final pre-inference freeze. No source, claim, truth label or acceptable action needed correction.

Final artifacts at `/tmp/jev-verdict-fresh`:

- Cases SHA256: `e89abc3648c94d32cc5cd5f1c5a9ae93ee1a5f61ae09e231246c3e3e6dea3109`.
- Rubric SHA256: `3333baa06403c31cb8a1a22f19f721c4ff0a4b1977559ad87ba0fc299f09fc6d`.
- Author audit SHA256: `2ff3ebe772577eb64e06770cacfabac8fe5fe3999b86cd28557c0c8a7f22a5e0`.

The reviewer independently read every source, assertion, truth label and case-level rubric before any inference. The cohort contains 16 cases in eight matched pairs and 32 assertions. Truth labels partition into 24 supported, three insufficient, four contradicted and one ambiguous. Every pair preserves one supported neighbor. Exactly four changed targets are at /content and four at /claim_records/0/content. Evidence text and provenance match exactly within pairs. All 32 diagnostic stress labels differ from their corresponding truth labels.

A read-only standard-library audit passed 155 structural checks covering frozen case binding, unique case IDs, exact assertion paths, stress coverage and incorrectness, action agreement, supported/concern partitioning, citation keys, pair membership, identical paired evidence and exactly one changed assertion per pair. No test or build was run locally.

Source review preserves the important distinctions: an unmeasured property is insufficient rather than disproven; a directly observed incompatible postcondition supports contradiction; a bounded observation stays supported despite unrelated limits; report disagreement supports neither authority selection nor invented reconciliation. One unsafe case explicitly permits a specific grounded abstention. No other unsafe case allows generic inability to substitute for an assessable finding. No subject-specific content was shared with the root or implementation author before code freeze.

Three rubric-only amendments were requested and verified against the preserved original freeze:

1. Mechanical validation now names each frozen bound schema and unchanged downstream hash/citation validation. Every raw rationale and inability reason is reviewed. Missing or malformed verdicts earn no credit; projection cannot hide unsupported criticism.
2. One safe-case ambiguity note no longer repeats its unsafe pair's flag/abstain guidance. The note explicitly protects the two supported assertions and requires completed no-concern review. No action or truth label changed.
3. A scoring-contract entry freezes separate projected_semantic_pass and raw_semantic_pass, overall conjunction, and projection_hides_defect. Additional internal rationale failures must not be mislabeled as consumer-facing finding regressions. Baseline raw/projected results coincide.

Only those three rubric paths differ from the original. Case bytes are identical. Original and intermediate freezes are retained under the fixture directory.

Later semantic annotation must be described as contract-visible and hint-mode-blind: raw schema reveals the intervention, and response prose can incidentally reveal advisory use. The reviewer is a same-family agent with prior fixture, implementation and historical outcome exposure, not human gold or cross-family validation. Freshness applies to these prospective cases and outcomes, not to unknown failure families or public-benchmark generalization. Both contracts receive the same strict clause-level criteria, exact-target protection and unrepaired mechanical failures.

No repository files, output annotations or model receipts were changed by this reviewer. No provider calls occurred.
