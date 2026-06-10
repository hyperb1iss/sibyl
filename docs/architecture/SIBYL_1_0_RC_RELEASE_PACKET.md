# Sibyl 1.0 RC Release Packet

- Status: candidate packet prepared; external same-SHA receipts required before live dispatch
- Version: `1.0.0-rc.1`
- Candidate branch: `feature/sibyl-1-0-rc-candidate-20260610`
- Release floor: `v0.10.0`
- Rollback target: `v0.10.0`

This packet records the repo-side evidence required for the v1.0 RC candidate. The candidate SHA is
the final committed SHA on the candidate branch at release time. Volatile receipt values, including
the current candidate SHA and GitHub run IDs, live in PR #159 and the latest `rc-gate-receipt-*`
artifact so committing this file does not invalidate its own same-SHA evidence.

Do not tag or publish until Bliss gives explicit release go-ahead.

## Decision Gate

Ship now: ready after final same-SHA external receipts are green and Bliss explicitly approves live
dispatch. Not tagged or published.

Smallest blocker: refresh same-SHA Nightly Regression and release dry-run receipts for the final
candidate SHA if it moved, then get Bliss's explicit live-dispatch approval.

Residual release risk: Docker image startup, public PyPI pages, GitHub release body, container
manifests, and clean installs from published artifacts are post-publish checks.

## Claim Matrix

| Claim                                   | Receipt                                                                                                      | Status                 |
| --------------------------------------- | ------------------------------------------------------------------------------------------------------------ | ---------------------- |
| Active docs agree                       | `moon run docs:lint` and `moon run docs:build`                                                               | Local pass             |
| Task graph is current                   | `sibyl epic show epic_19e1dea67ebf`; `sibyl task list --status doing`; no RC todo or blocked tasks           | Local pass             |
| Source ingest is current                | `moon run adapter-ingest-gate`; `moon run large-corpus-rehearsal`                                            | Local pass             |
| Synthesis is source-grounded            | `moon run synthesis-gate`                                                                                    | Local pass             |
| Automatic memory is policy-safe         | `moon run autonomy-gate`; `moon run memory-trust-gate`; `moon run trust-control-gate`                        | Local pass             |
| Sessions are boring                     | `moon run auth-session-gate`                                                                                 | Local pass             |
| Reflection quality is current           | `moon run reflection-quality-gate`                                                                           | Local pass             |
| Context and workspace trust are current | `moon run context-quality-gate`; `moon run workspace-trust-gate`                                             | Local pass             |
| Overview performance is current         | `moon run overview-perf-gate`                                                                                | Local pass             |
| Surreal-only runtime holds              | `moon run inventory-check`; `moon run inventory-typecheck`; `moon run inventory-test`; supported grep audit  | Local pass             |
| Redis is optional locally               | `moon run api:test -- tests/test_coordination_local.py -v`; `moon run api:memory-trust-jobs-test`            | Local pass             |
| Backup/restore is release-gated         | `moon run backup-restore-gate`                                                                               | Local pass             |
| Benchmark ledger is claim-safe          | `moon run bench-gate`; external LongMemEval receipt; Nightly Regression compare artifacts                    | Local + external pass  |
| Package artifacts build                 | `moon run python-package-build` produced `sibyl-core`, `sibyl-dev`, and `sibyld` wheels and sdists           | Local pass             |
| Package installs work                   | Clean isolated `uv tool install` for `sibyl-dev` and `sibyld`; both entrypoints report `1.0.0rc1`            | Local pass             |
| Helm surface works                      | `helm lint charts/sibyl`; `helm template sibyl charts/sibyl` has no `graphiti`, `falkor`, or `postgres` hits | Local pass             |
| Release cut is gated                    | Release dry-run runs `moon run :check` and validates same-SHA Nightly before skipped tag steps               | Workflow guard + tests |
| Publish dispatch is gated               | `.github/workflows/publish.yml` runs `moon run :check` before Python and Docker artifacts                    | Workflow guard + tests |
| Rollback is ready                       | Roll back to `v0.10.0`; do not move tags; verify published artifacts before announcing RC                    | Operator action        |

## Receipt Highlights

- `moon run :check` -> 49 completed (40 cached).
- `moon run release-workflow-test` -> 10 passed.
- `moon run bench-gate` -> gate passed.
- `moon run bench-gate-test` -> 58 passed.
- Core review follow-through latest local gates: `moon run core:test -- tests/test_search.py -q` ->
  1578 passed, 14 skipped; `moon run core:lint` -> all checks passed; `moon run core:typecheck` ->
  all checks passed; `moon run bench-gate-test` -> 58 passed.
- Current same-SHA PR CI, LongMemEval V2, Nightly Regression, and release dry-run receipts are
  recorded in PR #159 and the latest release dry-run `rc-gate-receipt-*` artifact. If the candidate
  SHA moves, refresh these receipts before live dispatch.
- Release dry-run must show `rc_gate_conclusion=success`, `nightly_conclusion=success`, a
  `nightly_url` matching the same candidate SHA, and skipped tag, GitHub Release, publish dispatch,
  and artifact creation steps.
- LongMemEval-S live full receipt -> GitHub Actions run `26304777971`, commit
  `36032a25b2893f2fbcbc074bd0c212fb829dd975`, SHA256
  `d2c0b69e8e5901fa950aa679006f1d29f8d3dd6ef142df8cb8b7a541570a9a0d`, inner JSON size `7073488`,
  archive expiry `2026-08-20T18:21:29Z`, and citable external manifest
  `benchmarks/results/ai-memory/external/longmemeval_sibyl_live_full_26304777971.json`.
- `moon run python-package-build` -> built `sibyl_core-1.0.0rc1`, `sibyl_dev-1.0.0rc1`, and
  `sibyld-1.0.0rc1` artifacts.
- Clean isolated install -> `sibyl 1.0.0rc1`; `sibyld 1.0.0rc1`.
- `moon run docs:build` -> build complete, with existing Rollup chunk and PURE annotation warnings.
- `helm lint charts/sibyl` -> 1 chart linted, 0 failed.
- Helm render -> 350 lines; no `graphiti`, `falkor`, or `postgres` references.

## Prompt-To-Artifact Audit

The latest core-review follow-through turned the review prompt into five repo artifacts:

| Prompt thread                                       | Artifact commits | Verification receipt                                                                                                |
| --------------------------------------------------- | ---------------- | ------------------------------------------------------------------------------------------------------------------- |
| Keep `sibyl-core` standalone                        | `42633f2f`       | Core boundary test prevents module-scope `sibyl` or `apps.api` imports from `sibyl_core`.                           |
| Expose reactive SurrealDB capture signals           | `15844e0d`       | Raw capture live-query bridge tests cover subscription, event normalization, and degraded fallback behavior.        |
| Move single-create embedding off the write path     | `1d963323`       | Async embedding projection tests cover pending payloads and backfill receipts.                                      |
| Trainable reranking without losing replay receipts  | `12184328`       | Learned reranker replay tests cover out-of-fold training, feature attribution, and deterministic fallback behavior. |
| Make graph-native retrieval signals visible in rank | `ddacb83b`       | Search tests preserve graph-expansion receipts, boost corroborated paths, and keep graph-only expansion demotion.   |

Release-only blockers after those artifacts are external/operator work: freeze the final candidate
SHA, refresh same-SHA Nightly Regression and release dry-run receipts, dispatch Release only after
explicit approval, and verify the published artifacts.

## Release Dispatch Requirements

1. Do not move the candidate SHA before dispatch. If it changes, rerun same-SHA CI, LongMemEval V2,
   Nightly Regression, and release dry-run.
2. After explicit Bliss go-ahead, dispatch Release with `version=1.0.0-rc.1`, `dry_run=false`, and
   the same-SHA Nightly Regression run ID recorded in PR #159 and the latest release dry-run
   receipt.
3. Verify the GitHub release, PyPI package pages, Docker manifests, docs install page, and clean
   installs from published artifacts before announcing the RC.
