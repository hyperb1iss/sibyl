# Heldout fast-critic independent preflight

Verdict: PASS for the frozen bounded synthetic experiment. No blocking implementation defect found. This is contextual same-family independent verification, not a cross-family or human review. No production source changes, provider calls or credentials were used by this reviewer.

The reviewed worktree is /Users/bliss/dev/worktrees/sibyl/nova/jev-heldout-fast-critic, with tests and independent preparation executed only on devbox-stef-gradial-com-main in /home/dev/dev/worktrees/sibyl/nova/jev-heldout-fast-critic.

Frozen SHA256 values:

- Runner heldout_fast.py: 3b8c04a9b690f73cc94ccf86d1f73a9f16ed5bfc7c1fde2dd566745d0728aaec
- Analysis heldout_fast_analysis.py: 3fd716199c70ba5e57a9d535d19fda0007d9f8b0103c41fe438ec86084b612fa
- Tests test_heldout_fast.py: 501af7eea0dae1883bb7a2ee93236faab35ad4d515752b1ca1bfa67ff37f94ed
- Moon configuration: 4604d6f9aa5cc4d5d4ffa958e54b04c53f46d7788ed46dce38c6730fb3636f46
- Cases: 0f1f757c0d2a905676a7f3ae5e17eb65dcaa69b30d16cfb44822d1db44c07208
- Rubric: 974f899458a4af4932fb00c06950049c2dc3f39cac9ff9ebeda35e287cc1796f
- Frozen plan manifest: e3207f9ad9dc097fdf196a25f47ea13bc2d6614ad9347703daf2e5833b2d3b9d

Local and remote reviewed Python hashes match. Independent no-key preparation produced manifest and schedule byte-identical to the parent's /home/dev/dev/eval-runs/jev-heldout-fast-20260920/plan artifacts.

Independent commands and receipts (run from the remote worktree):

1. moon run root:jev-revalidation-test -- -k heldout
   PASS: 21 tests, 346 deselected, 5.72 seconds pytest; Moon 5bacb4a8.
2. moon run root:jev-revalidation-test -- /tmp/test_jev_heldout_independent.py -k review_heldout
   PASS: 4 additional independently authored scratch tests, 367 deselected, 3.42 seconds pytest; Moon f4c31807. These exercise an actual short adapter deadline, preserved unknown billing and empty-hint fallback with exact replay, changed valid Jev answers against the derived critic invocation, dispatch corruption, and a missing path sidecar. Replay corruption rejects before output creation.
3. moon run root:jev-heldout-fast-critic -- --cases benchmarks/jev_revalidation/heldout_fast_cases.json --rubric benchmarks/jev_revalidation/heldout_fast_rubric.json --output-dir /tmp/jev-heldout-independent-preflight-20260920
   PASS: no-key preparation, Moon b19a802b, 3.184 seconds total. Both manifest.json and schedule.json pass cmp against the parent's frozen plan.

Static review covered all three new Python files, Moon task, reused request construction, hint validation, raw recording and replay helpers, product decision failure handling and critic mechanical validation. The independent fixture review is recorded separately in /tmp/jev-heldout-fixture-review.md.

Request and data boundaries: all 48 case/repeat triples have identical prepared evidence, frozen direct/stress requests and source-support requests across arms. The plan has 144 scheduled critic paths, 48 per arm, and 48 actual Jev acquisitions. The full critic receives only the unchanged product prompt/schema plus the existing advisory instructions and path/hash/label slot. Gold, rationale, rubric and stress metadata are not sent. The live arm derives its hints from the actual validated observation, then checks those labels against prepared assertion identities. A failed Jev observation yields an empty slot and continues the full critic; its billing remains counted. Stress hints are isolated to the misleading arm.

Replay and failure behavior: replay regenerates the schedule and manifest, requires exact completion and artifact sets, verifies raw requests plus dispatch bindings, recomputes Jev observations and dynamic critic requests, re-applies the unchanged mechanical validator, and compares rows and summaries. Valid-label corruption cannot silently alter the archived critic input. Transport and mechanical failures stay in completed-run denominators and do not receive abstention credit. External cancellation retains active raw receipts and a scheduled/missing ledger and does not emit a completed summary. The independent real-deadline test confirms a cancelled Jev transport receipt becomes an unavailable decision, retains unknown cost and replays identically without provider calls.

Timing and economics: service timing starts before candidate preparation and includes real Jev acquisition when applicable, the full critic, local validation and relevant local bookkeeping. Harness queue is reported separately. Exact serial stage containment and nonnegative finite timing are checked on replay. Costs include all actual Jev and critic observations, preserve unknown costs and attempts, and keep misleading-arm economics separate. No paid stage is skipped by an advisory label.

Interpretation limits: the 12 matched pairs are clustered synthetic units and the two repeats are dependent. The author had prior error-type exposure; the rubric discloses it. The stricter clause-level rubric prevents direct comparison to the earlier contextual primary semantic score. Action-level counts cannot establish finding quality; the summary leaves semantic review pending and production_qualified false. A quality gain can coexist with a failed speed gate. The fresh primary gate requires semantic improvement without new specified harms, median at least 20 percent faster, nonworse p95 and nonworse fully known cost. Wrong-label harm remains a separate robustness veto. No source retrieval, fresh authorization, durable publication or interactive recall latency is measured. Mixed-arm randomized concurrent traffic and small tail samples support descriptive within-run comparisons only, not a production SLO or significance claim.

The frozen plan may proceed to the authorized synthetic calls. Any semantic conclusion still requires the predeclared arm-blind review, original failures and all scheduled denominators.
