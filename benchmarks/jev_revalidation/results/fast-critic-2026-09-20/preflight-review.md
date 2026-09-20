# Independent fast-critic preflight review

Verdict: PASS for the frozen seven-file experiment snapshot listed below, based on dfffd2133. The bounded 128-call synthetic experiment is ready for the parent's authorized live execution. This verdict is not model quality evidence, a production recommendation, or permission to expand the cohort. No reviewer provider calls, credentials access, product edits, or production mutations occurred. Review independence is contextual within the same model family.

## Scope and finding closure

Reviewed fast_critic.py, fast_critic_study.py, fast_critic_analysis.py, fast_critic_rubric.json, both new test files, the Moon task, and reused original-archive replay, raw transport, usage extraction and product mechanical validation. No existing product code changes.

One confirmed preflight gap was found and closed: fresh replay initially ignored the archived rubric.json scoring copy. An altered or missing archived rubric could therefore pass replay despite the rubric being part of the frozen protocol. The final code persists the rubric's exact original bytes and checks their SHA-256 against the frozen manifest during replay. Independent corrupt-rubric and missing-rubric regression runs pass. No blocking findings remain.

## Binding, leakage and execution

Preparation regenerates and fully replays the original quality-speed archive before extracting any Jev labels. Exact original request identity, model, complete answer set, source/candidate preparation and the original completion marker are checked. New hints contain only claim_path, claim_sha256 and one of the four source-support labels. The request builder independently checks each hint against actual prepared assertion bytes and rejects extra fields, duplicates and invented values. The real prepared schedule has exactly 64 pairs and 128 calls: 64 direct and 64 hinted. All 92 hinted assertion observations are represented across the two repeats.

Both arms use the same full product source evidence, CriticOutput schema, advisory instructions and forced tool. After removing only the final advisory slot value, every real paired request is identical. Direct requests contain an empty list. No confidence, original gold, rubric, prior critic finding or prior critic action is inserted into the model prompt. Same-model interleaving is seeded, with both arms present for every case and repeat. The changed intervention includes the additional hint tokens and their labels; it does not isolate label semantics from that extra context.

The fresh response must match the pinned Haiku model aliases and Anthropic provider. Unexpected routes, truncated/malformed tool results and missing token counts do not become successful reviews. The recorded extractor checks the actual augmented invocation and retains the unchanged product assertion-hash, citation and submission validation. Invalid mechanics map to an error rather than earning abstention credit. The parent separately verified current public route metadata for the exact model slug and provider tag; the review verifies the code's fail-closed enforcement, not endpoint availability.

Every completed run must contain all scheduled identities, including transport failures. Fresh known costs, unknown-cost calls and separately attributed prior Jev cost remain distinct. The prior Jev attribution in the real plan is 0.006124356 USD; those calls will not be billed again by this experiment. Fresh timing covers dispatch and local interpretation of the critic stage, with scheduling queue separately recorded. It excludes original Jev acquisition and an actual background scheduler. Cancellation preserves dispatch/raw artifacts and scheduled/missing identities; a canceled run has no success summary or completion marker. Potentially paid canceled attempts remain unknown and are not silently zeroed.

The live gate requires the exact frozen manifest before dispatch. The manifest binds source archive files, source program/prompt state, cases, rubric, final schedule, transport controls and new analysis code. Fresh replay validates raw and dispatch bindings, derived rows, timing containment, exact artifact coverage, summary and exact rubric bytes before producing replay output. Input evidence is expected to remain immutable during the run.

## Rubric and design review

The rubric covers all 32 cases and all 46 assertions exactly once: 20 material concerns and 26 supported assertions. Its action allowances match the original frozen fixture. The reviewer checked the source target paragraphs, including arithmetic, prompt injection, condition boundaries, causal overstatement, partial supported candidates, and missing/conflicting evidence. No substantive rubric disagreement was found.

The rubric separates valid finding coverage from valid limitation abstention, does not treat missing evidence as an observed contrary fact, protects supported subclaims, and distinguishes correct core concerns with overstated bases from fully supported findings. Case qs-021 explicitly distinguishes the invented evidential status from unknown physical contents. The six flag-or-abstain cases preserve their original action allowance. Historical gold and some historical output exposure are disclosed; the rubric is a synthetic diagnostic reference, not independent human truth.

The old 32-case cohort and two dependent repeats support a within-corpus comparison only. They cannot establish generalization, powered noninferiority, downstream task quality, actual protected receipt readiness, or a contemporaneous comparison against the older Opus results. Later semantic review must be frozen before revealing arm mappings or aggregate results. Its packet should omit arm, hints, augmented request prompt, cost/token/latency metadata and scheduling order, retaining only randomized opaque IDs, original evidence/assertions, rubric, mechanical status and raw findings/abstention. Output wording can still reveal its own reliance on hints; record that unavoidable limitation rather than pretending perfect blinding.

## Independently executed checks

All tests and harness execution ran through Moon on stef-gradial-com-main in /home/dev/dev/worktrees/sibyl/nova/jev-assisted-fast-critic. No local builds or tests ran.

1. `moon run root:jev-revalidation-test -- /tmp/test_jev_fast_independent.py -k "fast_critic or review_fast"`
   Final result: 45 passed, 302 deselected in 5.58 seconds; Moon run bf2b448f; exit 0. Includes all 44 new tests and a separately authored cancellation probe with eight active and two queued calls. The probe verifies all ten remain scheduled/missing, exactly eight dispatch/raw receipts survive with canceled/unknown outcome, queued calls never dispatch, and no success summary is written.
   The first attempt had a scratch-test import-path collection error, before tests executed. Adding the workspace root to that scratch file's import path fixed the reviewer harness; no repository source changed.
2. `moon run root:jev-fast-critic -- --cases benchmarks/jev_revalidation/quality_speed_cases.json --source-archive /home/dev/dev/eval-runs/jev-quality-speed-20260920/live --rubric benchmarks/jev_revalidation/fast_critic_rubric.json --output-dir /tmp/jev-fast-preflight-independent-20260920`
   Result: complete no-key offline preparation, Moon run 4072012a; exit 0, 4.480 seconds. Validated the retained 128-path original archive and generated the complete fresh schedule without live calls.
3. Independent standard-library inspection of the actual plan verified all 128 calls, 64 exact request pairs, all 92 hints, exact rubric byte binding, sole hint-slot intervention and 0.006124356 USD prior attribution. The preparation manifest SHA-256 is 4e1f77300089be6582c182bc018c466cb22b41a7330334559b28446168235e6d. Local/remote code hashes match. `git diff --check` passed.

The parent separately reported 346 tests, lint and type checking passing. Those author gates are separate from the independent results above.

Root spot-checks:

- `moon run root:jev-revalidation-test -- /tmp/test_jev_fast_independent.py -k test_review_fast_cancel_active_and_queued`
- `moon run root:jev-revalidation-test -- -k 'fast_critic and (prepare_live_replay or corrupt_fresh_archive or bad_route)'`
- The no-key preparation command above with a new exclusive output directory.

## Frozen SHA-256

- fast_critic.py: 62c01244c44098459079933fbdca848a9daa91eb45b293692b84721adea4c347
- fast_critic_study.py: 6b8da09d9809ce769dffd053d68eb1adb60b4ef6d981918c6eff06514bcac284
- fast_critic_analysis.py: d91eb57d010a1661487938d0ca934c3cd7c44a3e067f42744600248a3579b122
- fast_critic_rubric.json: a3e96b87d0d25e66de2b3ce126c97e282aa696e96314384f6b4421971ffebe22
- test_fast_critic.py: d7f50333ffc1471d82dd520f79398227178cbc290e04102f5e68207fe4260e90
- test_fast_critic_study.py: 1ce1a4d98aca57ad09dfb2894bd5a0653ff3de48ec3a71d5af4e9cdf222ad03d
- moon.yml: bf27bd14c70c2467a1a87c1b34ac65274d8f05b64030f8a59f3997da77de26a9

Experiment file paths are relative to benchmarks/jev_revalidation except moon.yml at the worktree root.
