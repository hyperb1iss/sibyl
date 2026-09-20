# Assertion study failure diagnosis

The two critic errors are provider output schema violations. The third failed stage is a Jev probability vector rejected by the existing receipt normalization invariant. The harness preserved all three costs and applied its frozen failure behavior. No response was repaired, retried, or reclassified as successful.

| Cohort | Path | Stage | Exact cause | Retained stage cost |
|---|---|---|---|---:|
| Retained | case-5-repeat-0-baseline_misleading (hf-018) | Critic | findings[0].basis = unsupported_universality, outside the transmitted enum | $0.003603 |
| Fresh | case-11-repeat-1-assertion_misleading (af-012) | Critic | findings[0].basis = unsupported_certainty, outside the transmitted enum | $0.004178 |
| Fresh | case-4-repeat-1-assertion_live_jev (af-005) | Jev | /content probabilities sum to 0.99: 0.93 + 0 + 0.05 + 0.01 | $0.00005775 |

Both critic responses had HTTP 200, the expected Anthropic Haiku model/provider, one correctly named CriticOutput tool call, and finish_reason=tool_calls. The submitted tool schema exactly matches the product CriticOutput schema. Actual augmented prompt, contract, full request and request digest bindings pass. The exact product parser raises a single literal_error at findings[0].basis for each response. Re-running the frozen interpreter on the untouched raw receipts reproduces the saved error results. These are model/provider format failures, not parser disagreement with the transmitted schema. Correct-looking prose inside an invalid object does not convert either error into a valid finding.

The Jev response had HTTP 200 and the expected TypeSafe/model route. Its wire envelope parses successfully. Constructing the /content ChoiceAnswer raises 'probabilities must sum to one' because the normalized receipt tolerance is 0.000001. The vector may reflect output rounding, but the frozen contract does not normalize incomplete mass or retain selected labels from an invalid receipt. Offline replay through the actual provider adapter reproduces response_schema_mismatch, empty hints, and one attempted Jev call. The critic subsequently completed and flagged the candidate. The complete path cost is $0.00433475, including the rejected Jev call. This is an output-versus-receipt-contract mismatch; the evidence does not establish a harness implementation bug or the intended missing probability mass.

The raw scan covers all 48 retained critic calls plus 16 Jev calls, and all 192 fresh critic calls plus 64 Jev calls. All 320 calls returned HTTP 200 on their pinned provider/model routes. There were no transport, route, or HTTP failures. The raw response bytes and text agree, and every request digest matches its recorded request. The retained cohort cost is $0.194381488; the fresh cohort cost is $0.695284232. The failed critic stages cost $0.007781, and all three failed stages cost $0.00783875. Costs remain in the experiment denominators.

Evidence: /tmp/jev-assertion-failures.json contains raw paths, SHA256 digests, exact validator errors, usage and path accounting. The independent offline probe is /tmp/test_jev_assertion_failures.py. The successful log is /tmp/failure-probe-2.log (1 passed, 391 deselected). The initial /tmp/failure-probe.log preserves an out-of-tree import failure; the rerun supplies the explicit repository PYTHONPATH and changes no experiment source.

Reproduce on the devbox:

```sh
cd /home/dev/dev/worktrees/sibyl/nova/jev-assertion-critic
PYTHONPATH=/home/dev/dev/worktrees/sibyl/nova/jev-assertion-critic moon run root:jev-revalidation-test -- /tmp/test_jev_assertion_failures.py -k assertion_raw_failure -s
```

No semantic correctness judgment is made here. No blind reviewer received arm outcomes from this diagnosis.
