# Verdict study failure diagnosis

The frozen parsers reproduce all four critic failures and both failed Jev stages from unmodified raw receipts. No evidence indicates a harness repair is required. The output failures remain failures, and the timed-out Jev dispatch retains unknown billing.

| Cohort | Path | Failure | Recorded failed-stage cost |
|---|---|---|---:|
| Retained | case-4-repeat-0-verdict_direct (hf-017) | Two concern verdicts repeat /content; exact-once coverage requires one | $0.006646 |
| Retained | case-4-repeat-0-verdict_live_jev (hf-017) | Jev /content probability vector sums to 0.99 | $0.000039606 |
| Retained | case-11-repeat-0-verdict_live_jev (af-008) | Jev dispatch exceeded its 30-second deadline; no response received | Unknown |
| Fresh | case-3-repeat-0-baseline_misleading (vf-004) | findings is a string instead of an array | $0.004081 |
| Fresh | case-15-repeat-1-baseline_direct (vf-016) | findings[1].basis is unsupported_certainty, outside the enum | $0.004713 |
| Fresh | case-2-repeat-0-verdict_live_jev (vf-003) | verdicts is a string instead of an array | $0.005838 |

The four critic responses had HTTP 200, the pinned Anthropic Haiku route, the expected forced tool name and finish_reason=tool_calls. All 288 critic responses finished with tool_calls; none reported length truncation. Their raw request schemas match the frozen model schemas, and complete request/prompt/contract bindings pass. The actual frozen interpreters reproduce the original failed results and usage. The fresh string fields are not decoded again, and the invented enum value is not mapped to a valid one.

The retained duplicate output passes the VerdictOutput structural type schema, then fails the explicit coverage check with 'verdicts must cover every assertion exactly once'. The prepared input contains one assertion, /content, while the response contains two concern verdict objects for that same path and hash. The frozen instruction explicitly prohibits duplicate targets and allows multiple findings inside one concern verdict. Combining the objects or dropping one would repair the response after generation and change the registered outcome. The experiment performs neither operation. The raw critiques remain available for separate semantic review; this diagnosis makes no semantic correctness claim about them.

The rejected Jev vector is 0.93 + 0 + 0.05 + 0.01 = 0.99. The current receipt contract requires a normalized vector within 0.000001. The adapter preserves its known cost and returns no advisory labels; the full critic then completes independently. Possible rounding does not authorize post hoc renormalization under the frozen contract.

The timed-out Jev call dispatched at 19:51:36.878036Z and ended at 19:52:06.884538Z (recorded monotonic elapsed 30005.703 ms). The raw transport records cancelled, while the adapter records deadline_exceeded and attempt_count=1. There is no HTTP status, response body, provider response ID, usage, observed route, cost, or finish reason. The internal request digest is not a provider billing receipt. No observed amount can be recovered from these artifacts. The provider may or may not have completed or charged the request. Billing stays unknown, never zero. Empty-hint fallback completed with a critic finding and $0.006009 known critic cost; that path's total cost remains unknown.

Accounting independently sums raw provider usage across every scheduled stage and matches each path receipt. Retained has 96 critic calls and 32 Jev attempts, with $0.486670070 known cost plus one unknown charge. Fresh has 192 critic calls and 64 Jev calls, all billed amounts observed, totaling $0.974310400. Known failed-stage spend is $0.021317606 plus the unknown Jev charge. Across both cohorts, known spend is $1.460980470 and total spend is unknown. The raw inventories are complete, including the cancelled call's dispatch and outcome receipts.

The raw timestamps show no provider-call overlap between cohorts. The final retained response completed at 19:52:09.932411Z; the first fresh dispatch began at 19:52:10.809242Z, a gap of 876.831 ms. Exhaustive interval comparison finds zero intersecting retained/fresh call pairs. Process startup overlap or unrelated devbox load cannot be inferred from these call receipts, and no claim about their timing impact follows.

The report /tmp/jev-verdict-failures.json contains complete raw accounting, SHA256 bindings, failure details and exact parser proofs. The stdlib reconstruction is /tmp/jev_verdict_failure_audit.py. The offline parser probe /tmp/test_jev_verdict_failures.py passed through Moon (1 passed, 425 deselected); the log is /tmp/jev-verdict-failure-probe.log. Raw receipts, source, outputs, and semantic labels were not modified. No paid calls or response repairs were made.

Reproduce the raw reconstruction locally:

```sh
python3 /tmp/jev_verdict_failure_audit.py
```

Reproduce the exact parser probe on the devbox:

```sh
cd /home/dev/dev/worktrees/sibyl/nova/jev-verdict-critic
PYTHONPATH=/home/dev/dev/worktrees/sibyl/nova/jev-verdict-critic moon run root:jev-revalidation-test -- /tmp/test_jev_verdict_failures.py -k verdict_failure_raw_parser -s
```
