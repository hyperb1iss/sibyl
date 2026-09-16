# Frozen proposal for the next 48 development cells

Prepare the two memory checkpoints first, then run the fixed 48-cell schedule under one cumulative solver reservation. Checkpoint 0 means memory prepared before the one accepted consolidation cycle. Checkpoint 1 means memory prepared after that cycle. Solver execution happens after both preparations are frozen. This ordering keeps test traces out of the training corpus and avoids a new partial-reservation protocol.

The proposal is unarmed. Every final pack, manifest, request, complete request count, runtime-launch receipt and checkpoint acceptance is explicitly missing in `schedule.json`. No provider call, database operation, reservation, solver invocation, task-solution inspection or source change occurred while preparing these artifacts. The frozen schedule is a proposal for independent review, not an executable manifest or an approval.

## Fixed experiment

Use the accepted recall policy (`b09ab77f`) on exact source commit `324cd673a88f479ec0d2205f026cd751d6ec99fe`, tree `0704578d1da04888a0584a45e93fadbd9dcbf579`. The policy is an older immutable proposal snapshot; its historical source/readiness fields are not silently rewritten. The separate current324 native-adoption acceptance binds the source now intended for this screen.

The six tasks are the already selected authored development tasks. The task-identity correction (`dcfc5f6c`) confirms that none appeared in the previous 24 cells. Their no-memory solver performance is unmeasured. Only their public prompts, workspace hashes and existing catalog metadata were inspected. Private oracle hashes were copied from the retained material freeze; no oracle or reference implementation body was read.

| Family | Related tasks | Applicability contrast |
| --- | --- | --- |
| interval_capacity | venue-capacity-report; rack-power-windows | inclusive-slot-occupancy |
| utf8_framing | hex-stream-journal; record-separator-channel | independent-message-decoder |

The four arms retain their policy names: `native`, `raw_retrieval`, `strong_summary`, `no_memory`. Each task has one cell per arm at each checkpoint, with repetition 0. The denominator remains 48, including every missing pack, unstarted cell, unknown outcome and failed attempt. A missing or untrusted result is not a demonstrated task failure. No failed or missing cell receives a replacement attempt.

The execution order is fixed without solver outcomes: checkpoint 0 then checkpoint 1; interleave task families in policy index order 0,3,1,4,2,5; rotate the policy arm order left by `(policy_task_index + 2 * checkpoint) % 4`. Every arm has six cells per checkpoint and appears in each within-task position exactly three times over both checkpoints. Fresh UUID5 attempt IDs derive from a fresh study namespace and the checkpoint/task/arm/repetition tuple. All 48 IDs differ from the previous 24 IDs and all 240 training-attempt IDs. The full order and IDs are in `schedule.json`.

Keep Qwen `qwen/qwen3-coder-next`, seed 0, 2,000,000 cumulative input tokens, 8,000 output tokens, 20 tool calls, $2 per solver cell, 600 controller seconds, 120 checker seconds, 30 seconds per tool and 256 MiB tool memory. The existing JSON checker additionally has its own 10-second invocation timeout. Tool count is a ceiling, not a promise that all 20 calls fit the token or time allowance. Keep the shared 90,000-token memory allowance and require actual initial request plus workspace plus reserved output strictly below 204,800 Qwen tokens. Character geometry cannot substitute for those actual counts.

## Training and checkpoint contract

The fixed training identity is the exact 233 admitted original captures across 20 families, with all 240 original attempt slots retained for accounting. The 233 include six operational outcomes. The remaining seven controller-failed slots are not invented captures. The accepted native baseline contains 233 raw captures, zero entities, zero episodes and zero relationships. These are dated accepted observations, not live authorization for a future process.

The next entry must requalify all 233 identities, bytes, durable observations, current membership, issuer and signature authority through the accepted original-source owners. The catalog, source observations and membership must remain equal before and after memory preparation and each solver cell. Original capture IDs are fixed in `schedule.json`; native derived publications may appear only through the existing protected publication and retrieval owners. Every derived item must trace to the fixed original corpus. Do not insert solver traces, task prompts, test outcomes, edited workspaces, checker output or summary-construction output into training or the native retrieval corpus.

The exact cycle policy is deliberately unbound. Checkpoint 1 requires one actual accepted consolidation cycle, not a dry run, a fabricated checkpoint label, or a second attempt selected because the first produced no gain. The production job (`apps/api/src/sibyl/jobs/reflection.py:100`) defaults to 20 sources and 50 candidates and clamps source selection at 100 (`:114`). One invocation therefore cannot be called a complete 233-source pass. The cycle definition and parameters must be frozen before preparation. Its receipt must retain selected sources, complete/failed/pending proposal-page coverage, candidate dispositions, actual applied publications and all durable model execution IDs. If the intended cycle is a complete pass, the current single-invocation contract is insufficient; that is an explicit remaining integration decision, not an excuse to label a partial pass complete.

Raw and summary checkpoint-1 memory bytes must equal their checkpoint-0 bytes after current authority validation. The summary uses the complete 20-reference corpus recipe, never only the task-retrieved subset. Native checkpoint 1 uses a freshly produced authorized inventory and ordinary ranking over actual current publications. Compare actual selected items and pack hashes before interpreting outcomes. Identical native treatment gives no evidence that a learned change affected the solver.

## Minimal execution sequence

1. Qualify one owned runtime and database epoch on source324 with the final memory-processor settings already fixed. Reuse the accepted intake, private runtime, source checks, `CurrentAuthority`, `CurrentOwners`, `qualify_originals` and `OriginalCatalog.check`. Public configuration and epoch must not drift between checkpoints; the existing raw/summary reuse check compares their owner receipts exactly.
2. Construct the independently accepted complete summary once under its separate memory ledger. A successful count-only preflight is not a summary. Produce current native inventory 0 and prepare the 24 checkpoint-0 cells using `RecallAdapter.prepare`. Require the actual baseline inventory to remain raw-original-only. Freeze complete pack receipts or explicit missing-pack reasons.
3. Execute exactly the separately reviewed cycle on the fixed original corpus. Preserve its terminal receipt and all partial-page and model-usage evidence. No task solver runs before or during this phase.
4. Requalify all originals, produce native inventory 1 and prepare checkpoint 1. Raw and summary use their exact bound checkpoint-0 receipts through the existing reuse branch. Native uses the new inventory. Freeze both checkpoint records and the completed treatment comparison.
5. Materialize at most twelve ordinary manifests, one per checkpoint/task. Each manifest contains its one task and the arms with actual prepared packs. A missing arm has no invented pack or runner identity and stays in the outer 48-row schedule. A task with no prepared arms needs no invalid empty manifest. Derive all prepared rows' expected runner identities from the actual loaded manifest and runner receipt owner. Freeze exact actual request/count bindings and declared source provenance before any solver claim.
6. Independently qualify the concrete entry, its manifests, private runtime, Linux controller image/Unix socket, authority callbacks, current cumulative budget and exact output/claim paths. Only an accepted entry may create the new exclusive whole-schedule $96 reservation. Reuse the existing exclusive claim, fsync, atomic receipt publication and per-cell begin/outcome pattern. Execute prepared cells in frozen order; retain missing cells without dispatch or replacement. Any started cell with an uncertain outcome stays unknown and retains its hold.
7. Emit the complete observation report and accounting terminal even on interruption. Keep physical calls/usage, controller-reported spend, held reservations and unknown usage separate. No final missing cell disappears from the 48-row report.

Checkpoint-0 native inventory is historical after the cycle. Do not call `NativeCheckpoints.verify(0)` against a changed current database and pretend the result should pass. That verifier belongs at checkpoint-0 preparation. The solver consumes the frozen checkpoint-0 pack, with current source authorization checked again. The proposed shortcut is narrow: the actual checkpoint-0 inventory and pack must contain only the original raw captures. All233 requalification then covers its current source validity without requiring the entire pre-cycle database to remain unchanged. Checkpoint-1 derived items still require current protected publication validity. If baseline inventory contains a derived item, or an original observation changes, this shortcut is invalid and affected cells remain unqualified. A negative test must enforce that condition.

## Exact owner reuse and required changes

| Surface | Existing owner | Minimal change or remaining binding |
| --- | --- | --- |
| Task manifests | `benchmarks/agent_tasks/manifest.py:55,128,137,153,206` | No product schema change needed. `Arm.memory_pack` is shared across all tasks in a manifest, so the materializer must emit one task per checkpoint manifest. Preserve task family IDs and learning-source IDs; family labels used for analysis are not query filters. `native_render_payload` stays unset for this engine-level whole-item policy unless an actually compatible renderer is separately qualified. |
| Request and receipts | `benchmarks/agent_tasks/runner.py:528`; `coding_controller.py` | Reuse `load_manifest`, actual receipt identity, exact attempt IDs and durable partial receipts. Bind the actual Linux runtime/image/evaluator and request counter. Do not use old task manifests or a local runtime fingerprint as a current launch manifest. |
| Memory preparation | Recall v4 `recall_adapter.py:40,51,68,124`; `contract.py:65` | Reuse current all233 authority, complete task query, raw/native whole-item rendering and cp0-byte reuse. A concrete entry still needs to persist complete checkpoint preparations and consume them immutably. Preparation grid alone has no launch order or attempt IDs. |
| Native checkpoints | Recall v4 `native_inventory.py:189,196,213` | Reuse complete inventory production/verification at the matching preparation state. Add the explicit historical raw-only cp0 solver-consumption check and the fresh current cp1 publication check in the entry, not a weaker version of the existing inventory verifier. |
| Cycle | `apps/api/src/sibyl/jobs/reflection.py:100,114,148,180,192` | Bind a concrete one-cycle policy and original233-only source boundary. Retain actual coverage and durable execution IDs. Do not confuse one invocation with a complete pass or allow unrelated captures into selection. |
| Claim and executor | Historical `learning-transfer-launcher-7389a132416841878a2336e6e163bba8/launcher.py:78,210,224` | A versioned successor must parameterize 48 rows, the new six tasks, checkpoint/task manifest lookup, exact arms and the new cumulative ceiling. Existing code hardcodes the old24 acceptance, two old families and old summary receipts. Reuse its atomic claim/per-cell receipt owners; do not run it unchanged or reuse its old claim directory. |
| Report | `benchmarks/agent_tasks/transfer_report.py:15,19,55,111,128` | Existing arm profiles reject `native`; `ScheduledAttempt` lacks checkpoint; pair keys omit it. Add a separate observation-only adapter over the retained 48-row schedule and reuse `_validate_receipt` for prepared rows. Validate exact task/checkpoint/arm/category/attempt and manifest identities, permit an explicitly unprepared row with no expected runner identity, and reject receipts for that row. Keep checkpoint independent of repetition. Do not silently rename native to sibyl_consolidation. No two-family generalization bootstrap is needed for this screen. |

The report should expose, for every row, preparation state/reason, dispatch state, receipt trust, task outcome if known, physical usage, pack identity, request identity and checkpoint identity. Aggregate denominators stay six per arm/checkpoint and 48 overall. Report related tasks and applicability contrasts separately. Matched before/after changes require both trustworthy outcomes and an actual treatment difference. A native improvement with controls stable is a useful exploratory result; a native contrast regression is a concrete applicability problem. Saturation of these new no-memory tasks would show that this screen has limited room to detect gains. Missing cells establish incomplete evidence, never substitute no-memory failures. Retain facts rather than an efficacy verdict inferred from plumbing success.

## Solver budget proposal

| Quantity | USD |
| --- | ---: |
| Accepted historical cumulative held reservation | 755.2186368 |
| Existing ceiling | 762.4700928 |
| Existing unused headroom | 7.2514560 |
| Proposed addition, 48 cells at $2 | 96 |
| Proposed cumulative held reservation after the new claim | 851.2186368 |
| Proposed successor ceiling, old ceiling plus $96 | 858.4700928 |
| Headroom preserved after the new claim | 7.2514560 |
| New reservation made by this preparation | 0 |
| Old hold released by this preparation | 0 |

The exact historical reservation was read from its retained archive member, with both archive and member hashes checked. The later accepted terminal confirms the same cumulative held amount and preserves the original unknown cell without replay. Known old controller usage ($1.08454070) is not a refund. The proposal retains all unknown holds.

This is a solver-only addition. Memory generation, consolidation and retrieval-provider costs retain their separate existing ledger owners and acceptance requirements. The concrete entry must reconcile any intervening cumulative change before claiming. Do not silently reset the prior cumulative total, reuse freed-looking unknown capacity, double count an already held memory envelope or present this ceiling proposal as permission to spend. `budget-proposal.json` contains the exact Decimal arithmetic and original receipt bindings.

## Verification before a concrete launch

The proposed manifest, execution and observation-report changes belong in a fresh artifact harness around the pinned owners. They do not require changing product source324 or either frozen recall adapter. The small implementation delta needs controls for cross-task/checkpoint pack swaps; changed public task bytes; a missing arm without a fake hash; duplicate/replayed attempt IDs; mismatch between runner return and durable receipt; current source or membership drift before/after a cell; a derived item improperly inserted into baseline inventory; changed or unavailable checkpoint-1 publication; raw/summary checkpoint reuse drift; inclusion of a checkpoint-0 solver trace in training; an actual cycle failure or incomplete coverage; a denied reservation with no dispatch; an interrupted cell retaining an unknown hold; and a final report preserving all48 rows.

The local reproducibility script verifies the schedule, all public task bindings, exact source owners, the233/240 catalog census, ID freshness, order balance and the historical budget archive member. Run `python3 -B reproduce.py` from this artifact directory. An optional `--output /absolute/fresh/directory` writes the six reproduced data artifacts into a new directory without changing the frozen originals.

The original routing lesson-removal pair remains open. The later fresh-experience dream0/1/3/10 curves, sealed comparison, conversational nonregression, scale and cross-host release checks remain required. The retained source collection target is met; the next48 is a fixed-corpus development diagnostic and cannot establish general learning efficacy or 1.4 release readiness.
