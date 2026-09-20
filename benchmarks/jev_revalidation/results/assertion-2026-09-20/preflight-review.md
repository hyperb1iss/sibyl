# Assertion-first critic independent preflight

Verdict: PASS for the frozen, bounded synthetic experiment. No remaining blocking implementation defect found. This is contextual same-family adversarial verification, not cross-family or human validation. The reviewer made no provider calls, read no new inference outputs, changed no repository source and accessed no credentials.

The worktree is /Users/bliss/dev/worktrees/sibyl/nova/jev-assertion-critic. All test execution and independent plan preparation occurred on devbox-stef-gradial-com-main at /home/dev/dev/worktrees/sibyl/nova/jev-assertion-critic. The fresh source-only review is separate at /tmp/jev-assertion-fixture-review.md and passed without corrections before inference. Fresh fixture details were not passed to prompt or runner authors before code freeze.

Frozen SHA256 values:

- Assertion contract: c7a934677d6ba55de2ff8ff89dc6f27326db91ff7f0561f95a982b14cbe0c37d
- Shared fast critic: ba9f58c9f3db7b53ba915b045de5b50798071ed0423b6cbbc9b5c4a23ff15a39
- Shared heldout transport: 5d2cd651290f2e88a28b13d05e8f68933985affae50d03b62e575bd7c26dc986
- Assertion runner: 86dbea47b99820caa11f69452b05b33f71f18cc56338e813ab8a9d6e3b323aed
- Assertion contract tests: 97aa89a1ffb5702a84c08e7b1ac6c757d7809d7a53078ba94e31bee24c96dfe3
- Assertion runner tests: c0cd8004a3c1137feabb56db96958c889c4ea43b505bb17e35ae7f05d3c79f2b
- Moon configuration: f7ad4cab0fe7b9f93837c1c4e62716ffb91a519d6e1e3a4cc9d09ab891aae8aa
- Retained manifest: 600e30aacd46b4e2c956ad2d15a4bf1d49953e81cbb0257dc7b65a39ec404cd7
- Fresh manifest: 17645adf7cae33632a341e57777d199784734da8a8aa6333595ca1539e1c7e0c
- Fresh cases: 7498f1415ceb0b678ee315a5cfd6adaa8edccc7a6805de3482ec9383716edf36
- Fresh rubric: a37c43b506f8f083b07c079dfe4ab3e5ce5b0a437b23aa1f6f2f7e66d7174f04

Independent verification:

1. moon run root:jev-revalidation-test -- /tmp/test_jev_assertion_independent.py -k "assertion or heldout"
   PASS: 59 tests, 337 deselected, 9.76 seconds pytest; Moon d39a371c. This includes all new contract/runner tests, existing shared transport and binding regressions, and five independent scratch probes. The scratch probes exercise a real short adapter deadline in both live arms, preserved unknown billing and empty-hint fallback, exact prepare/freeze/live/replay roundtrip, changed valid Jev answer rejected against the derived critic invocation, cross-contract raw critic transplant, missing Jev receipt, and a new unsafe acceptance that cannot be hidden by aggregate action counts.
2. moon run root:jev-assertion-critic -- --cases /home/dev/dev/eval-runs/jev-assertion-20260920/retained/cases.json --rubric /home/dev/dev/eval-runs/jev-assertion-20260920/retained/rubric.json --output-dir /tmp/jev-assertion-independent-retained-20260920 --repeats 1
   PASS: no-key preparation; Moon e26f76ad, 3.191 seconds total.
3. moon run root:jev-assertion-critic -- --cases /home/dev/dev/eval-runs/jev-assertion-20260920/fresh/cases.json --rubric /home/dev/dev/eval-runs/jev-assertion-20260920/fresh/rubric.json --output-dir /tmp/jev-assertion-independent-fresh-20260920 --repeats 2
   PASS: no-key preparation; Moon cfde8fb1, 3.662 seconds total.

Independent standard-library checks compared manifest.json, schedule.json and rubric.json byte-for-byte against both parent's frozen plans. Every comparison passed. The retained schedule has 48 critic calls and 16 Jev calls; the fresh schedule has 192 critic calls and 64 Jev calls. All six arms are complete and balanced. Every case/repeat group has identical prepared evidence and semantic Jev wire input across contracts, while the two live arms have distinct physical decision request identities. Both cohorts bind identical code hashes, protocol and critic controls.

Closed pre-call failure: the author gate initially reported nine failures because tuple-valued protocol comparisons became arrays after JSON reload and failed exact freeze equality. The final implementation stores JSON-native lists. Independent prepare -> frozen JSON -> mock live -> exact replay probes now pass. Earlier failures are not provider outcomes and no paid call preceded the fix. The author's final full 391-test suite, lint and typecheck are reported separately in /home/dev/dev/eval-runs/jev-assertion-critic-20260920/gates-final.log; the independent results above do not rely on that verdict.

Review conclusions:

- The intervention is an added instruction prefix. The baseline request is unchanged; schema, full source evidence, host assertion hashes, citation namespace, route controls and advisory slot are preserved. The added instructions protect actual bounded propositions and supported neighbors while still requiring every material unsupported claim to be examined. No parser relaxation, response repair, partial acceptance or publication authority was introduced.
- Interpretation reconstructs the declared contract and exact request before unchanged product mechanical validation. A changed contract, prompt, schema or invocation cannot become a successful result. Raw provider billing is retained even when the output fails parsing or validation.
- The six-arm schedule independently acquires advice in each live arm. A failed acquisition uses an empty advisory slot and continues the complete critic, retaining the failed attempt's time and known/unknown billing. No advice is reused across those two paid paths. Direct and misleading arms make no Jev call. Every critic still sees complete original evidence.
- Replay validates the frozen manifest, schedule, rubric and complete artifact inventory before creating output, rederives observations and dynamic critic requests, then compares archived rows and summary. Corrupt valid-looking advice, cross-contract raw data and missing stage receipts fail closed. Cancellation retains dispatched raw receipts plus the complete scheduled/missing ledger and does not publish a completed summary.
- Timing includes candidate preparation, Jev acquisition when applicable, full critique and mechanical validation; queue is separate. Accounting includes all stages with unknown costs preserved. Source acquisition, authorization, durable publication and interactive recall remain outside the measured path.
- Semantic quality is explicitly pending. Action counts cannot prove finding correctness; failures remain failures, and production_qualified stays false. New unsafe acceptance and supported-acceptance loss are reported per paired path. Concern coverage and spurious-finding harms still require the frozen arm-blind review.

Interpretation boundary: direct versus direct isolates the prompt intervention. Live versus live compares combined policies with independently sampled Jev labels, so it cannot identify a pure prompt effect when advice differs. The final protocol states that limitation and reports per-pair hint-label agreement. Misleading versus misleading uses identical authored wrong hints and separately tests prompt robustness. Retained diagnosis and fresh confirmation must remain separate; no tuning between cohorts is permitted under this freeze. Small matched-pair synthetic cohorts and dependent repeats support descriptive diagnostics, not a production speed or quality guarantee.

Root spot-check commands:

- moon run root:jev-revalidation-test -- /tmp/test_jev_assertion_independent.py -k review_assertion
- moon run root:jev-revalidation-test -- -k "injected_executor_keeps_cancellation or live_factorial_replays or baseline_interpretation_is_identical"
- Compare /tmp/jev-assertion-independent-{retained,fresh}-20260920/{manifest,schedule,rubric}.json with /home/dev/dev/eval-runs/jev-assertion-20260920/{retained,fresh}/plan/ using cmp.

The exact frozen experiment can proceed to the authorized synthetic calls. Results must retain all original failures and undergo arm-blind semantic review before any quality conclusion.
