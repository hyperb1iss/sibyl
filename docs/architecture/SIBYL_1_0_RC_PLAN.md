# Sibyl 1.0 Final-Prep Plan

- Status: active final-prep plan; `v1.0.0` is not cut
- Created: 2026-05-19
- Refreshed: 2026-06-26
- Release target: `1.0.0`
- Current shipped candidate: `v1.0.0-rc.8`
- Public rollback floor: `v1.0.0-rc.8`
- Previous stable rollback: `v0.10.0`
- Parent roadmap: [`SIBYL_1_0_ROADMAP.md`](SIBYL_1_0_ROADMAP.md)
- Release packet: [`SIBYL_1_0_RC_RELEASE_PACKET.md`](SIBYL_1_0_RC_RELEASE_PACKET.md)

This plan carries the original RC evidence-freeze work into the final 1.0 cut. The RC line has
already shipped through `v1.0.0-rc.8`; this document now tracks what must be true before the final
tag is created.

Do not tag, publish, dispatch Release, or announce 1.0 until Bliss gives explicit release go-ahead.

## Release Promise

Sibyl 1.0 is ready when the repo is versioned for `1.0.0`, package metadata and internal dependency
pins agree with that version, the exact final SHA has fresh same-SHA release receipts, and the live
cut cannot bypass those gates.

The final pass does not add another product slice. It closes release hygiene, local compose
installability, current evidence, and post-publish verification.

## Current State

Shipped and landed:

- `v1.0.0-rc.8` is published and is the public rollback floor.
- Main CI is green on `f0ff15e6a86e07c39cddacba9aaf8dfe1b815d9c`.
- Main Nightly Regression is green on `f0ff15e6a86e07c39cddacba9aaf8dfe1b815d9c`, but its restore
  job did not run from the scheduled event.
- rc8 Release and Publish workflows succeeded on `779a9347c81e5be1377e6675e4b702e8533ab980`.
- PyPI, Homebrew, AUR, GHCR, and clean package-install smoke checks passed for rc8.
- PR #194 fixes local compose runtime issues and is included in the prep branch.

Not yet final-ready:

- The final candidate SHA has not been selected and frozen.
- Same-SHA Live Runtime Eval and LongMemEval receipts are stale.
- Manual Nightly Regression for the final candidate SHA still needs to run so restore coverage is
  present.
- Release dry-run still needs to run for the final candidate SHA.
- Live Release dispatch still requires explicit Bliss approval.
- Post-publish artifact checks can only run after the live cut.

## Final Success Criteria

The final release can be cut only when all of these are true:

- `VERSION` is `1.0.0`.
- `sibyl-dev` and `sibyld` pin `sibyl-core==1.0.0`, including daemon extras.
- Published package classifiers no longer describe Sibyl as alpha.
- The local compose path works without the standalone worker crash loop and with reliable web/API
  health checks.
- `moon run :check` is green on the exact final candidate SHA.
- Manual Nightly Regression is green on the exact final candidate SHA.
- Live Runtime Eval and LongMemEval receipts are current for the final candidate SHA.
- Release dry-run validates the same-SHA Nightly Regression run ID and uploads an
  `rc-gate-receipt-*` artifact.
- The live Release workflow is dispatched only after explicit Bliss approval.
- GitHub Release, PyPI packages, Homebrew, AUR, Docker images, docs install copy, and clean installs
  are verified after publish.

## Work Plan

### Wave 1. Repo Final Prep

Goal: make the branch releaseable without cutting it.

Tasks:

1. Include PR #194 or an equivalent local compose fix.
2. Advance `VERSION` to `1.0.0`.
3. Update internal Python package pins to `sibyl-core==1.0.0`.
4. Move package classifiers to production/stable.
5. Refresh this plan and the release packet so they describe rc8 and final 1.0 truth, not stale rc1
   state.

Verification:

- Focused CLI lint, typecheck, and local compose tests.
- `moon run python-package-build`.
- `moon run release-workflow-test`.
- `moon run docs:lint`.
- `moon run docs:build`.

### Wave 2. Candidate Freeze

Goal: prove the exact final SHA is the one being released.

Tasks:

1. Push or merge the final-prep branch to the selected candidate location.
2. Run CI for the exact candidate SHA.
3. Run manual Nightly Regression for the exact candidate SHA.
4. Run Live Runtime Eval and LongMemEval for the exact candidate SHA.
5. Run Release dry-run with the same-SHA Nightly Regression run ID.
6. Record the final receipt IDs in the release packet.

Verification:

- Same-SHA CI is green.
- Same-SHA Nightly Regression is green and includes restore coverage.
- Same-SHA Live Runtime Eval and LongMemEval receipts are attached.
- Release dry-run reports `rc_gate_conclusion=success`, `nightly_conclusion=success`, and skipped
  tag/publish steps.

### Wave 3. Live Cut

Goal: publish 1.0 with no ambiguity.

Tasks:

1. Get explicit Bliss go-ahead for live Release dispatch.
2. Dispatch Release with `version=1.0.0`, `dry_run=false`, and the same-SHA Nightly Regression run
   ID.
3. Confirm Publish completes successfully.
4. Verify the GitHub release body and tag.
5. Verify PyPI package pages and package metadata.
6. Verify Homebrew, AUR, and Docker multi-arch manifests.
7. Verify clean installs of CLI and daemon entrypoints from published artifacts.

Verification:

- GitHub release exists for `v1.0.0`.
- PyPI has matching `sibyl-core`, `sibyl-dev`, and `sibyld` `1.0.0` packages.
- Docker tags exist for API and web images.
- Clean install entrypoints report `1.0.0`.
- The release packet records final receipts and residual risks.

## Release Decision Template

Use this decision once Wave 2 is complete:

```text
Ship v1.0.0:
  yes/no
Candidate SHA:
  <sha>
Blocking packet:
  <smallest remaining blocker, if no>
Proof command or receipt:
  <command, artifact, CI URL, or release packet section>
Residual risk:
  <accepted risks and owners>
Rollback target:
  v1.0.0-rc.8 or v0.10.0
```

## Recommendation

Do not cut final 1.0 until the final candidate SHA has same-SHA CI, manual Nightly Regression, Live
Runtime Eval, LongMemEval, and Release dry-run receipts, and Bliss explicitly approves the live
dispatch.

The highest-leverage path is:

1. land the final-prep branch;
2. freeze the resulting candidate SHA;
3. refresh the external same-SHA receipts for that exact SHA;
4. dispatch Release with the matching Nightly Regression run ID after explicit approval;
5. verify published artifacts and clean installs before announcing 1.0.
