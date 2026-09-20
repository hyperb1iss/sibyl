# Verdict critic independent preflight

PASS for the frozen offline experiment. No unresolved implementation or protocol blocker was found. This verdict authorizes no production behavior: it concerns the bounded synthetic comparison and evidence collection only.

## Scope and disclosure

Reviewed all seven implementation/task files in `/Users/bliss/dev/worktrees/sibyl/nova/jev-verdict-critic`, including the two new test modules, reusable parser and study seams, plus both frozen fixture rubrics and the blind-packet builder. Tests and no-key preparation ran on `devbox-stef-gradial-com-main` in `/home/dev/dev/worktrees/sibyl/nova/jev-verdict-critic`. No paid calls, credentials, production changes or source edits were made by the reviewer. Scratch tests and receipts were written outside the repository.

The reviewer is a same-family agent, independently assigned from the implementer, with prior source/code and historical output exposure. Fixture source review occurred before inference; fresh contents were not relayed to the root or implementer before code freeze. Later review is contract-visible and hint-mode-blind, not fully arm-blind. Raw output schemas expose the intervention and prose may incidentally mention hints.

## Verified properties

- The baseline request is the previous assertion-prefix contract. The intervention adds fixed instructions and a forced verdict schema while retaining full original evidence, existing criticism rules, route controls and advisory cautions. This is a schema-plus-instruction comparison, not an isolated wording experiment.
- Projection requires exactly one bound verdict for every host assertion, rejecting duplicates, unknown/missing targets, stale or inconsistent assertion digests, wrong nested finding targets, invalid citations and extra fields. Supported and unable entries cannot contain hidden finding arrays. All-unable projects to abstention, never acceptance. Concern plus unable preserves both findings and abstention.
- The complete raw response survives alongside valid typed verdicts and projected output. A bad final verdict rejects the whole response rather than salvaging earlier concerns. Mechanical failure, paid attempts and observed cost remain in the denominator. Semantic correctness of a supported rationale is deliberately not assumed by the projector.
- Every live-Jev arm makes its own bound acquisition. Failed acquisitions retain cost and use the established empty-advice fallback. Dynamic critic requests are reconstructed from prepared evidence and observed hints. Replay checks frozen inputs, exact raw artifact sets, invocation identity, regenerated raw/typed/projected results and accounting before creating output.
- Service timing includes preparation, live Jev when applicable, critique and local validation. Queue delay is separate. Direct/live comparison measures whole experimental service paths, not interactive recall or publication latency.
- All scheduled paths survive cancellation in the partial receipt, including queued paths; dispatched attempts keep cancellation receipts. No completion or summary is emitted for the canceled run.

## Independent execution

Reviewer scratch source: `/tmp/test_jev_verdict_independent.py` (same path local and devbox).

Executed on the devbox:

```sh
cd /home/dev/dev/worktrees/sibyl/nova/jev-verdict-critic
PYTHONPATH=$PWD moon run root:jev-revalidation-test -- /tmp/test_jev_verdict_independent.py -k 'verdict or independent' -s
```

Result: 40 passed, 389 deselected, 6.44 seconds pytest; Moon task `63c4a026`, exit 0. The selected tests include four independently written probes: all-unable never accepts, a bad final verdict does not salvage a valid prior concern, raw supported-rationale tampering fails replay despite unchanged consumer projection, and cancellation preserves eight dispatched plus four queued paths without a completion marker. Additional selected tests cover exact baseline behavior, raw rationale retention, malformed/full-coverage verdicts, model/tool/request binding, failed Jev accounting and exact replay.

Root spot-check commands, from the same devbox worktree:

```sh
PYTHONPATH=$PWD moon run root:jev-revalidation-test -- /tmp/test_jev_verdict_independent.py -k 'test_independent_all_unable_is_never_accept or test_independent_bad_last_verdict_does_not_salvage_prior_concern' -s
PYTHONPATH=$PWD moon run root:jev-revalidation-test -- /tmp/test_jev_verdict_independent.py -k 'test_independent_supported_raw_tamper_rejected_even_same_projection or test_independent_verdict_cancel_retains_active_and_queued' -s
```

Author-reported broader gates are 425 tests, lint and typecheck, plus six historical-default compatibility probes. Those are supplemental author evidence, not the basis of this independent PASS.

## Independent no-key plans

Prepared both cohorts independently through Moon's `root:jev-verdict-critic` task using the canonical frozen cases/rubrics and repeats 1 for retained, 2 for fresh. The independent output directories are `/tmp/jev-verdict-independent-retained-20260920` and `/tmp/jev-verdict-independent-fresh-20260920` on the devbox. Moon preparation tasks `c0e584ed` and `d4ec5f0e` passed.

For both cohorts, manifest.json, schedule.json and rubric.json match the parent's canonical `/home/dev/dev/eval-runs/jev-verdict-20260920/{retained,fresh}/plan` bytes exactly. Independently checked complete unique six-arm schedules, one identical prepared evidence payload and Jev wire state per case/repeat, identical controls and full common prompt suffix across paired contracts, and correct call inventories.

- Retained plan: 96 critic calls and 32 Jev calls. Manifest SHA256 `ef6dc701138df91cd833a41051576dac7a7e64c355a67b6947d98adfc1bd4c01`.
- Fresh plan: 192 critic calls and 64 Jev calls. Manifest SHA256 `6c8aba9ae624e267e9b81ab01b2876c1fcb97847fc480e9c09da387f47828aa1`.
- Total scheduled acquisition: 288 critic plus 96 Jev calls, 384 calls. Repeats and minimal-pair cases are dependent observations.

## Pre-inference review contract

Separate `projected_semantic_pass` (consumer findings/abstention), `raw_semantic_pass` (complete verdict/rationale fidelity) and overall conjunction are frozen in both rubrics. Baseline raw/projected results coincide. `projection_hides_defect` records projected pass with raw failure; raw-only rationale defects must not be presented as consumer-finding regressions. Concern coverage requires a fully valid exact-target finding; a permitted grounded inability is reported separately, never invented finding coverage. Invalid responses earn zero credit without repair. Original judgments must freeze before mapping unblind, with any sensitivity recorded beforehand.

Two rubric clarity fixes and one generic raw/projected scoring clarification were verified before inference; source and truth labels did not change. Source-review receipts are `/tmp/jev-verdict-retained-fixture-review.md` and `/tmp/jev-verdict-fresh-fixture-review.md`.

The blind builder now catches malformed outer JSON, rejects duplicate keys, retains exact arguments text for malformed arguments, and does not silently repair output. Its valid packet excludes mapping, hint payload, timing, cost and repeat identifiers. Unparseable outer-body text is retained only as necessary diagnostic evidence and may incidentally expose otherwise hidden response metadata; this does not earn semantic credit.

## Final source hashes

All seven hashes independently matched local and devbox files:

| File | SHA256 |
| --- | --- |
| verdict_critic.py | `1148b88f79303ed469043d574bd1788331125a459bac531e941f7607f386b9d5` |
| verdict_study.py | `10a994bc24120d05ae4f8c0a070c0ca078095edd0ebd3e1fe780e238908453fc` |
| assertion_study.py | `835eb197fbf511ba6c86a4e7df055d79511e647ba6bc190eb790bbc60577b0ab` |
| fast_critic.py | `80aaec139e0d00bf3693d6b00574e51fbd857851b3d1835422e52f63ecdf2102` |
| test_verdict_critic.py | `6fddf9afc488ddd2967a2f498876a8df40726d4686f3f98708b17711b36ee44e` |
| test_verdict_study.py | `03abbce6b1d231e6dc61d15f39aca6ab04ce48c6e743b4ff281ad25186048237` |
| moon.yml | `c5d0f4cb569218569d675eabca3f2765e05f8baed1d4c28cf6cd73b1605a28ab` |

Any changed source or fixture after this freeze requires explicit revalidation. No semantic quality, speed benefit or production qualification has been established by preflight tests.
