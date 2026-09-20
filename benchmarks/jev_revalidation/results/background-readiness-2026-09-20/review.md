# Independent background readiness review

Verdict: PASS for the five-file offline experiment at base 83fa2ce0395d7f65299ccc241747b4df9e61074d plus the frozen uncommitted snapshot below. No blocking defect was found. This is an independent same-family contextual review, not a cross-family review. The reviewer made no source edits, paid provider calls, or production changes.

## Checked scope

Reviewed background_policy.py, background_readiness.py, both new test files, and the Moon task, including their dependencies on quality_speed replay, exact request generation, receipt parsing, original guarded routing, and strict DecisionObservation validation. Existing product code is unchanged.

The policy probe is synchronous: it checks done/cancelled/result without awaiting or cancelling background work. Pending work stays pending for the current decision even if it later completes. A ready result must match both exact request digest and semantic identity, must survive fresh strict deserialization, and must validate its model and complete answer set against the current request. Source generation, revision, incarnation, authority-view fingerprint, route policy, project, and execution changes cannot borrow an old observation. Authorization is an explicit caller-supplied input, not authority conferred by this policy. The caller must freshly authorize its request. No production caller, receipt store, publication decision, or asynchronous authorization protocol is implemented here.

The analysis regenerates the original schedule and full manifest, fully replays raw responses through the existing adapters and mechanical critic validation, checks dispatch bindings and exact artifact sets, compares derived rows and summary, and requires the original complete live completion marker before writing output. It rejects an output inside the immutable archive and uses an exclusive output directory. Each comparison uses an exactly equal paired critic request. The output binds all retained archive files and case/program hashes. The input archive is assumed stable during the read, as the retained immutable evidence contract requires.

Finite scenarios use an explicit assumed lead. A receipt that misses the consumer boundary cannot later change its foreground route. Missing, stale, unauthorized, failed, or semantically ineligible observations fall back. Every scenario charges all background Jev calls, including late and unused calls, plus its paired direct critic calls. Foreground, candidate-origin, and completion-of-all-work lower bounds are distinct. The all-ready scenario leaves origin/settlement timing unset rather than inventing a lead. Known costs use Decimal aggregation; unknown, negative, and nonfinite costs are rejected.

## Independently executed verification

All execution used Moon on stef-gradial-com-main in /home/dev/dev/worktrees/sibyl/nova/jev-background-readiness. No local tests or builds ran.

1. `moon run root:jev-revalidation-test -- -k "background or full_live_mock_replay or deadline or cancellation"`
   Result: 47 passed, 255 deselected in 3.02 seconds; Moon run 0452e74f; exit 0. This includes all 45 new readiness/accounting tests plus the existing actual adapter-deadline replay and scheduled cancellation accounting tests.
2. `moon run root:jev-background-readiness -- --cases benchmarks/jev_revalidation/quality_speed_cases.json --archive /home/dev/dev/eval-runs/jev-quality-speed-20260920/live --output-dir /tmp/jev-background-review-analysis-20260920`
   Result: completed in 4.306 seconds; Moon run 188efb3a; exit 0. Full original 128-path archive validation succeeded before the 64 paired paths were analyzed. No live transport is selected in this entry point.
3. Independent Python standard-library arithmetic reconstructed the ideal result directly from retained original rows and checked every archive file digest in the output. All 476 file hashes matched. Local and remote frozen source hashes matched. `git diff --check` passed.

The parent separately reported all 302 benchmark tests, lint, and type checking passing. Those author gates are additional evidence, not the independent command results above.

Root spot-check commands are the two Moon commands above; use a fresh exclusive output directory for a second analysis run. A smaller test spot-check is `moon run root:jev-revalidation-test -- -k 'background and (pending or corrupt or paired_request or full_replay)'`.

## Independently reconstructed result and claim boundary

The original paired baseline has 64 paths, median 4463.8864675 ms, p95 6931.711617 ms, and cost 1.773265 USD. All 64 background Jev calls cost 0.006124356 USD.

The ideal scenario bypasses the fixed 21 eligible paths and reuses the 43 paired baseline critic samples. Its foreground median lower bound is 4245.971881 ms, a 4.881723316% reduction; p95 is unchanged at 6931.711617 ms. Its conditional modeled cost is 1.229164356 USD. The zero-lead scenario has no bypasses, baseline latency, and cost 1.779389356 USD. These numbers match the generated analysis.

The ideal scenario cannot reach the original 20% median improvement target on this corpus under the frozen rule and reused baseline samples. That conclusion does not rule out background Jev generally, alternative routing policies, other cohorts, or scheduling/contention changes. The prefix is only a transport/interpretation timing proxy: no protected durable receipt, receipt lookup, fresh-authority lookup, scheduling overhead, or actual pre-validation overlap was measured. The fallback sample is reused under an explicit assumption of unchanged provider contention and queueing. Zero foreground bypass overhead is an optimistic lower bound. No production or interactive-recall speed claim is supported.

Action-level correctness reuses the existing synthetic gold/rubric and previously observed decisions; it does not establish new semantic quality, calibrated confidence, critic finding equivalence, downstream task outcomes, or production readiness. The final report and compact export passed the separate claim check recorded below.

## Frozen SHA-256

- background_policy.py: 05e261b28b1e9e2d22784477bfb646532e3bf8a2b4d3b8118993d5197984072a
- background_readiness.py: 2e3bba6ad683c917fa3e482dc84c0535f54e82af80fb9633967c16081810eb5d
- test_background_policy.py: 3c912813273b17868d3b8cd9be041c53d4fc8876c97400991737ac3aa5a288aa
- test_background_readiness.py: 64c69b83aadbe34aac686003f9a92dcc6d3720693bdcf9bf2498076d83a76c6f
- moon.yml: f8fb8c3d49816eba346cd9bf4f381b6b509af66976ff103dbbbf31cefb61bc42

Python file paths are relative to benchmarks/jev_revalidation in the named worktree.

## Final delta, report and export verification

Final verdict: PASS. The final delta renames the settlement field to all_work_completion_lower_bound_ms and adds the current-authorization and identical-input assumption to LIMITS. The formula and routing behavior are unchanged. The final source hashes above supersede the initial snapshot hashes.

Independently executed `moon run root:jev-revalidation-test -- -k background` on the devbox after the delta: 45 passed, 257 deselected in 2.85 seconds; Moon run 293ad5e6; exit 0. The earlier 47-test command and reviewer full replay remain evidence for the pre-rename snapshot. The parent separately ran the final replay and lint/type checking.

The reviewer compared the final full analysis against the independently generated earlier analysis. After normalizing the field rename and removing the intentionally changed program hashes and limitation wording, every numerical result and input-provenance field is exactly equal. The final retained full analysis SHA-256 is 8a4dd0678ddd989fc8f874157fbb1add0515c059f232150c858d4feac64d435d; local and devbox copies and completion records agree.

The committed compact summary is an exact projection of that full analysis. It removes only scenarios[].rows and archive.files_sha256, then adds full_analysis_sha256 and compact_projection. All aggregate and per-repeat metrics, case bindings, program hashes, and remaining provenance match. The completion file binds the full analysis rather than pretending to hash the compact summary.

The reviewer read BACKGROUND_READINESS_RESULTS.md, the README addition, and /tmp/jev-background-pr.md. Their table values, action counts, 4.88% median reduction, 30.68% modeled cost reduction, unchanged p95, zero-lead cost increase, and original acquisition totals match the evidence. The prose identifies the result as conditional, preserves dependent-repeat and synthetic-cohort limits, disclaims fresh semantic quality and downstream benefit, and does not claim production qualification or actual protected receipt readiness.

The architecture statements were traced to apps/api/src/sibyl/jobs/reflection.py and ordinary_cohorts.py, plus core services/reflection_validation.py and semantic_decisions.py. Source reflection precedes candidate draining; cohorts can persist candidates before later cohorts finish. The existing shadow starts beside the critic and awaits completion before returning. Its protected receipt binds the stored parent and rechecks authorization/snapshots. The post-persistence overlap window is therefore a proposed measurement location, with no guaranteed useful lead or implemented background scheduler. The report presents the seam at that level and makes no activation claim.

Final publication artifact SHA-256:

- BACKGROUND_READINESS_RESULTS.md: 04e03b1188c2fe213ac742b565dc7a23af674bd7b4caf12ce896125e2594dfc0
- README.md: 68d112cac31b64c2d8162d001cd44583b85a2c85e6b26d6c305176cf9415867e
- results/background-readiness-2026-09-20/summary.json: 2265058f45f4a968be20eeb18ecb1e79c2ad859d7fe616362a31883b38a75334
- results/background-readiness-2026-09-20/completion.json: f0afb19a8bad0d41aaa6277fb7e1dabb1c50eee5eac5515567476859e3064d28

Final report-only token paragraph: PASS. Independent standard-library recomputation from the original paired baseline rows confirms input/output medians of 2777/34 tokens for the 21 bypass-eligible paths and 2696/341 for the 43 remaining paths. The added text calls critique length a plausible investigation target and explicitly disclaims causal inference. The report hash above is refreshed; code and numerical artifact hashes remain unchanged.
