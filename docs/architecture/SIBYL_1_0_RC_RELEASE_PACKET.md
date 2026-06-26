# Sibyl 1.0 Final-Prep Release Packet

- Status: final-prep branch active; `v1.0.0` is not cut
- Current shipped candidate: `v1.0.0-rc.8`
- Final-prep branch: `release/prep-1.0-no-cut`
- Prep base: `f0ff15e6a86e07c39cddacba9aaf8dfe1b815d9c`
- Included runtime fix: PR #194, cherry-picked as `8bf9c254`
- Public rollback floor: `v1.0.0-rc.8`
- Previous stable rollback: `v0.10.0`

This packet is the handoff for the final 1.0 candidate. It prepares the repo for the `1.0.0` release
but intentionally does not tag, publish, dispatch Release, or mark 1.0 complete.

Do not cut `v1.0.0` until Bliss gives explicit release go-ahead.

## Decision Gate

Ship final now: no.

The branch can become a final release candidate after it is pushed, reviewed, and merged or
otherwise selected as the exact candidate SHA. The remaining release-only work is to refresh
same-SHA external receipts for that final SHA, then dispatch Release with the matching Nightly
Regression run ID.

Smallest current blockers:

1. Land the local compose runtime fix from PR #194 or this prep branch.
2. Land the `1.0.0` version and internal package dependency pins together.
3. Refresh same-SHA CI, Nightly Regression, Live Runtime Eval, and LongMemEval receipts for the
   exact final candidate SHA.
4. Run a Release dry-run for the same candidate SHA.
5. Get explicit Bliss approval before live Release dispatch.

## Claim Matrix

| Claim                          | Receipt                                                                  | Status                |
| ------------------------------ | ------------------------------------------------------------------------ | --------------------- |
| Version is final-prepped       | `VERSION=1.0.0`; Python internal deps pin `sibyl-core==1.0.0`            | Local pass            |
| Package metadata is 1.0-ready  | PyPI classifiers use `Development Status :: 5 - Production/Stable`       | Local pass            |
| Local compose path is fixed    | PR #194 / `8bf9c254`; focused CLI lint, typecheck, and tests             | Prep pass             |
| Current main CI is green       | CI run `28218659616` on `f0ff15e6a86e07c39cddacba9aaf8dfe1b815d9c`       | Current pass          |
| Current main nightly is green  | Nightly Regression run `28231371209` on `f0ff15e6a86e07c39cddacba9aaf8d` | Partial, restore skip |
| rc8 release workflow passed    | Release run `28151991606` on `779a9347c81e5be1377e6675e4b702e8533ab980`  | Published RC pass     |
| rc8 publish workflow passed    | Publish run `28152153233` on `779a9347c81e5be1377e6675e4b702e8533ab980`  | Published RC pass     |
| rc8 artifacts install          | PyPI, Homebrew, AUR, GHCR, clean `pip install --pre` smoke               | Published RC pass     |
| Final live eval is current     | Live Runtime Eval for final candidate SHA                                | Pending               |
| Final LongMemEval is current   | LongMemEval V2 / full receipt for final candidate SHA                    | Pending               |
| Final release cut is approved  | Explicit Bliss go-ahead plus live Release dispatch                       | Hold                  |
| Final post-publish smoke works | GitHub Release, PyPI, Homebrew, AUR, GHCR, clean installs                | Release-only          |

## Current Receipts

- `uv lock --check` -> resolved 214 packages in 18ms.
- `moon run python-package-build` -> built `sibyl_core-1.0.0`, `sibyl_dev-1.0.0`, and `sibyld-1.0.0`
  wheels and sdists.
- `moon run release-workflow-test` -> 16 passed in 1.24s.
- `moon run cli:lint -- src/sibyl_cli/local.py src/sibyl_cli/docker.py tests/test_local_surreal.py`
  -> all checks passed.
- `moon run cli:typecheck` -> all checks passed.
- `moon run --force cli:test -- tests/test_local_surreal.py -v` -> 348 passed in 5.15s.
- `moon run docs:lint` -> all matched files use Prettier code style.
- `moon run docs:build` -> build complete in 8.98s.
- `moon run inventory-check` -> snapshot current, 0 legacy graph import files, 95 retained
  legacy-term files.
- `moon run --summary minimal :check` -> 62 completed, 46 cached, 7 skipped, in 13.982s.
- rc8 Release workflow: <https://github.com/hyperb1iss/sibyl/actions/runs/28151991606>
- rc8 Publish workflow: <https://github.com/hyperb1iss/sibyl/actions/runs/28152153233>
- Current main CI: <https://github.com/hyperb1iss/sibyl/actions/runs/28218659616>
- Current main Nightly Regression: <https://github.com/hyperb1iss/sibyl/actions/runs/28231371209>

## Published rc8 Artifact Smoke

- PyPI has `sibyl-dev`, `sibyld`, and `sibyl-core` version `1.0.0rc8`.
- Homebrew tap formula is `1.0.0-rc.8` at commit `27201fb`.
- AUR package version is `1.0.0rc8-1`.
- GHCR manifests exist for `sibyl-api:1.0.0-rc.8` and `sibyl-web:1.0.0-rc.8` with `linux/amd64` and
  `linux/arm64` indexes.
- Clean isolated install smoke reported `sibyl 1.0.0rc8` and `sibyld 1.0.0rc8`.

## Final Dispatch Requirements

1. Select the final candidate SHA and do not move it before dispatch.
2. Confirm `VERSION`, `apps/cli/pyproject.toml`, and `apps/api/pyproject.toml` all agree on `1.0.0`.
3. Run same-SHA CI, manual Nightly Regression, Live Runtime Eval, and LongMemEval V2 / full
   evaluation for the final candidate SHA.
4. Run Release dry-run with the same-SHA Nightly Regression run ID and verify the uploaded
   `rc-gate-receipt-*` artifact.
5. After explicit approval, dispatch Release with `version=1.0.0`, `dry_run=false`, and the same-SHA
   Nightly Regression run ID.
6. Verify the GitHub release, PyPI package pages, Homebrew formula, AUR package, Docker manifests,
   and clean installs before announcing 1.0.

## Residual Risk

The remaining risk is external and release-operational, not repo-local: the latest Live Runtime Eval
and LongMemEval receipts are stale for the final SHA, and published artifact checks can only run
after the live cut.
