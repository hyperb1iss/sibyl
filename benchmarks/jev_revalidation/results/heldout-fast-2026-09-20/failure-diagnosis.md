# Held-out Haiku failure diagnosis

All five critic failures are provider output schema violations. The unchanged parser correctly rejected four unrecognized `basis` values and one `findings` value supplied as a string instead of an array. No request-binding, route, or parser defect was found.

| Call | Validator location | Cause | Critic cost |
| --- | --- | --- | --- |
| `case-15-repeat-1-direct` | `findings.1.basis` | `unsupported_certainty` | $0.004443 |
| `case-10-repeat-0-direct` | `findings.1.basis` | `unsupported_certainty` | $0.005251 |
| `case-11-repeat-1-misleading` | `findings` | `findings is a string, not an array` | $0.003758 |
| `case-21-repeat-1-live_jev` | `findings.0.basis` | `unsupported_universality` | $0.003993 |
| `case-21-repeat-1-direct` | `findings.0.basis` | `unsupported_universal_claim` | $0.003835 |

Each response was HTTP 200 from Anthropic, reported the pinned Haiku model, finished with `tool_calls`, and contained one `CriticOutput` tool call. The outer response and tool arguments were valid JSON. Valid JSON is insufficient here: each response violated the schema actually sent in its request.

The devbox probe confirmed that all five transmitted schemas exactly equal the frozen product's `CriticOutput.model_json_schema()`. The probe also verified each complete augmented request and its digest against the saved invocation. The four enum violations each produced exactly one Pydantic `literal_error`. The malformed `findings` value produced exactly one `list_type` error at `findings`.

The shared schema permits `factual_contradiction`, `unsupported_generalization`, `unsupported_causality`, `missing_condition`, and `misleading_certainty` as basis values. The returned labels `unsupported_certainty`, `unsupported_universality`, and `unsupported_universal_claim` are not in that enum. The stress-arm response placed list-like text, including an additional field fragment, inside a string-valued `findings` property. The parser must not coerce that string into a valid review.

The failures occur at `CriticOutput.model_validate(...)` in `fast_critic.py:189`, before the later product mechanical validation. The interpreter records `invalid_critic_output` and `result=null`. Preserve all five original `error` outcomes. Relabeling enums, repairing nested JSON, accepting a valid subset of findings, or retrying would change the experiment. This diagnosis does not grade the semantic correctness of any critique.

All five usage receipts remain intact and each records one known attempt. Their critic costs total **$0.021280**. The live-Jev path separately retains its preceding decision cost; the machine-readable receipt includes both that cost and the full path total. Schema rejection did not remove spending or failures from the denominator.

## Reproduction and receipts

The scratch probe ran on `devbox-stef-gradial-com-main` from `/home/dev/dev/worktrees/sibyl/nova/jev-heldout-fast-critic`:

```sh
PYTHONPATH=. moon run root:jev-revalidation-test -- /tmp/test_jev_heldout_failure_diagnosis.py -k retained_heldout_schema_failure -s
```

The result was **5 passed, 367 deselected**, exit 0. The probe reads retained data and calls the unchanged parser; it performs no provider calls and does not rewrite responses.

The local raw archive is `/tmp/jev-heldout-fast-20260920/live/`. The authoritative devbox archive is `/home/dev/dev/eval-runs/jev-heldout-fast-20260920/live/`. Exact raw file paths, hashes, request digests, validator errors, and usage are in `/tmp/jev-heldout-failure-diagnosis.json`. The command log is `/tmp/jev-heldout-failure-diagnosis-test.log`; the rerunnable probe is `/tmp/test_jev_heldout_failure_diagnosis.py`.

The interpreter hash is `62c01244c44098459079933fbdca848a9daa91eb45b293692b84721adea4c347`, matching the frozen live manifest. No code, fixture, original receipt, or result was changed. No diagnosis was sent to the arm-blind semantic reviewer.
