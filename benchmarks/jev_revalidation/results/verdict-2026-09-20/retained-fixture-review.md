# Retained verdict-study fixture review

PASS for retained fixture coherence. This is a diagnostic cohort selected from known prior cases, not held out.

Verified the exact frozen files at `/tmp/jev-verdict-20260920/retained`:

- Cases SHA256: `313e302ac60669794fb8cb0415cd541847ac12641d07e964dc33ee170e551ae7`.
- Rubric SHA256: `71c7ecc31c26dc40c1689f9ec37a0e35879a53f6c684b4eb65dedf084f797c95`.

All 16 case objects match their corresponding prior retained or fresh assertion-study objects exactly after JSON parsing. All 16 case-level rubric objects also match exactly. The cohort contains eight matched pairs and 30 host assertions. Original source truth and prior ambiguity boundaries therefore remain unchanged.

A read-only Python standard-library audit passed 160 checks covering prior-object identity, exact expected/stress path coverage, every stress label differing from truth, rubric/action agreement, material-concern and supported-assertion partitioning, and original citation keys. The existing `heldout_fast._assemble` consumes rubric cases_sha256 and exact case IDs; the action scorer uses case expected_action and acceptable_actions. These keys are coherent. No build or test was run locally.

The revised top-level rubric correctly states eight pairs and one repeat, retained diagnostic provenance, and explicit review of raw verdict rationales as well as projected findings. None of these selected unsafe cases permits generic abstention.

Review design clarification: schemas reveal whether the intervention uses verdict arrays. The later semantic review must be described as contract-visible and hint-mode-blind, with incidental hint-mode clues in response prose acknowledged. All raw supported rationales and unable reasons remain visible. Projection may not hide unsupported criticism or convert inability to assess into acceptance. The original clause-level and exact-target criteria apply equally to both contracts.

No case, rubric, annotation, source or implementation file was changed. Same-family reviewer has prior fixture, implementation and historical outcome exposure.

Final retained rubric also includes the exact generic raw_and_projected_semantics entry from the fresh rubric. Verified this equality and the final rubric hash before inference. Original case-level semantics remain unchanged.
