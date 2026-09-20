# Haiku output failure diagnosis

Both failed direct-arm calls returned a finding with `basis="unsupported_universal_claim"`. That value is absent from the exact schema sent to the provider. The shared product contract permits `unsupported_generalization` for this category. The parser correctly rejected the responses; this is a provider output schema failure, not a harness request-binding or route bug.

The affected calls are `case-15-repeat-0-direct` and `case-15-repeat-1-direct`, both for synthetic candidate `qs-016`. Each response was HTTP 200 from Anthropic, reported model `anthropic/claude-haiku-4.5`, finished with `tool_calls`, and supplied one `CriticOutput` call with valid JSON arguments. Each retained finding had a valid assertion hash and citation identity. The product schema validator reported exactly one error per response: `literal_error` at `findings[0].basis`.

The frozen interpreter reaches `CriticOutput.model_validate(...)` in `fast_critic.py:189`. Pydantic raises `ValidationError` for the unrecognized basis. The interpreter deliberately maps that exception to `invalid_critic_output`, retains usage, and returns no validation result. The failure occurs before the product's later mechanical checks. No successful finding or abstention was lost after validation.

The transmitted schema exactly matches `CriticOutput.model_json_schema()`. Its allowed basis values are:

- `factual_contradiction`
- `unsupported_generalization`
- `unsupported_causality`
- `missing_condition`
- `misleading_certainty`

The provider's critique text identifies an overgeneralization from one tested sheet and condition, but the wording does not make its invalid structured result usable. Preserve both original `error` outcomes. Relabeling the basis, adding a parser alias, changing the schema, or retrying would be a separate intervention and must not alter this frozen experiment's score.

## Accounting and raw receipts

The original usage remains present: repeat 0 cost $0.005615, repeat 1 cost $0.005596, total $0.011211. Each response records one known attempt. No tokens or cost were discarded because schema validation failed.

The raw receipt directory is `/tmp/jev-fast-critic-20260920/live/raw/`. The authoritative devbox directory is `/home/dev/dev/eval-runs/jev-fast-critic-20260920/live/raw/`.

| Raw receipt | SHA-256 |
| --- | --- |
| `case-15-repeat-0-direct.critic.json` | `09eb62fc35b27435d76e622ae5873139022a7f95129ae4c08321b89497aa3b4d` |
| `case-15-repeat-1-direct.critic.json` | `7bbf527697c98b6f60a7f0256b0c6b727d88b9f45b941a99dc6596a1523e33fb` |

The frozen interpreter hash is `62c01244c44098459079933fbdca848a9daa91eb45b293692b84721adea4c347`, matching the live manifest. The shared schema owner, `procedure_review.py`, hashes to `abb11750d4704272fee34beb5461e3d8f29c325f821a2f3873335e653c7eb5e7`.

## Reproduction

A scratch-only test reads both original receipts, validates actual request binding, checks schema equality and assertion/citation identities, and invokes the unchanged parser. The test asserts one `literal_error` at the exact field for each receipt. It does not correct output or invoke a provider.

The command ran on `devbox-stef-gradial-com-main` from `/home/dev/dev/worktrees/sibyl/nova/jev-assisted-fast-critic`:

```sh
PYTHONPATH=. moon run root:jev-revalidation-test -- /tmp/test_jev_fast_failure_diagnosis.py -k retained_schema_failure -s
```

The result was **2 passed, 346 deselected**, exit 0. The script is retained at `/tmp/test_jev_fast_failure_diagnosis.py` locally and on the devbox. The local command log is `/tmp/jev-fast-failure-diagnosis-test.log`. No frozen program, fixture, result, or raw receipt was changed.
