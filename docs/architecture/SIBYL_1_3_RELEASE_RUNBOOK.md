# Sibyl 1.3 release runbook

- Candidate commit: `012e7b55407d84ac2049914956b7017546d64b68` (`main`, 2026-08-27)
- Version: `1.2.2` to `1.3.0`
- Release state: ready for dry cut pending approval
- Status: [SIBYL_1_3_RELEASE_STATUS_2026-08-29.md](SIBYL_1_3_RELEASE_STATUS_2026-08-29.md)
- Plan: [SIBYL_1_3_IMPLEMENTATION_PLAN.md](SIBYL_1_3_IMPLEMENTATION_PLAN.md)
- General procedure: [docs/admin/releasing.md](../admin/releasing.md)

This runbook binds the general release procedure to the 1.3 candidate. Every command below runs from
a clean checkout of the candidate commit unless it says otherwise. Nothing here creates a version
commit, a tag, a GitHub release, or a publish run; those steps sit behind the approval gate in the
middle of this document.

## 1. Candidate commit

The candidate is `012e7b55`, the `main` head that merged
[PR 445](https://github.com/hyperb1iss/sibyl/pull/445). The exact-commit evidence already on record:

| Check              | Run                                                                         | Result  | Date       |
| ------------------ | --------------------------------------------------------------------------- | ------- | ---------- |
| CI                 | [33025662969](https://github.com/hyperb1iss/sibyl/actions/runs/33025662969) | success | 2026-08-27 |
| Nightly Regression | [33244786444](https://github.com/hyperb1iss/sibyl/actions/runs/33244786444) | success | 2026-08-29 |
| Nightly Regression | [33172885088](https://github.com/hyperb1iss/sibyl/actions/runs/33172885088) | success | 2026-08-28 |

Pass `33244786444` as `nightly_run_id`. The Release workflow refuses a nightly from any other
commit.

The candidate moves only if `main` moves. The hybrid fulltext lane fix (commit `fc17af80` on
`nova/sibyl-1-3-hybrid-fulltext-lane`, pull request pending) and this receipt lane (branch
`nova/sibyl-1-3-rig-blocked-dispatches`) are not in `012e7b55`. If either merges before the cut, the
new `main` head becomes the candidate, CI must pass on it, Nightly Regression must be dispatched on
it, and section 3 runs again on it. Nothing in this runbook may be carried across commits.

Pin the candidate before touching anything:

```bash
git fetch origin main
git status --short
git rev-parse origin/main
```

## 2. Generated receipts

| Receipt                     | Path                                                                                 | Digest                                                                    | Source runs                                                                 |
| --------------------------- | ------------------------------------------------------------------------------------ | ------------------------------------------------------------------------- | --------------------------------------------------------------------------- |
| Dispatch ledger (observed)  | `benchmarks/results/longmemeval-v2-release/sibyl-v1-3-aa-dispatch-ledger.json`       | `sha256:74339727e600a92a0157db517e6c5ce0b378bf08b4dc8ba2eb274818427a4299` | Controllers 32888217656, 32897996847, 32911050360, 32921881380, 32998699090 |
| Rig-blocked receipt         | `benchmarks/results/longmemeval-v2-release/sibyl-v1-3-aa-rig-blocked-receipt.json`   | `sha256:5850fd3f31ac8db9090b8e7bb8b62b349823500f0ebac6ebe3ba3a09cccc0df8` | Builders 32888310148, 32898089824, 32911112960, 32921948833, 32998783818    |
| r5 Web Small artifacts      | `gh run download 32998783818 -R hyperb1iss/sibyl` (retained 30 days from 2026-08-26) | recorded inside the run's result and diagnostics artifacts                | Builder 32998783818 on `7f31a330`                                           |
| RC gate receipt             | `rc-gate-receipt-<candidate-sha>` artifact of the Release run                        | produced by the dry cut                                                   | The dry-cut Release run                                                     |
| Release notes claim receipt | `release-notes-claim-receipt.json` inside the same artifact                          | produced by the real cut only                                             | The Release run without `dry_run`                                           |

The rig-blocked receipt is `RIG_BLOCKED` with `blocked_reason: dispatch_exhausted`,
`paid_benchmark_allowed: false`, `score_claim_allowed: false`, and
`ledger_provenance: github_verified`. It stops paid benchmark work for 1.3 and forbids any score in
the release notes. The provenance label records when the ledger last matched GitHub; it is not the
evidence. The documented check is the command that projects the receipt into release authority,
because that command re-fetches every controller run, builder run, and job through `gh api`,
re-discovers each controller's builders from the workflow run listing, rebuilds the receipt from the
bound ledger bytes, and writes the authority only when everything matches. Any field drift or fetch
failure fails closed, and the same live re-verification runs every time the authority is validated:

```bash
uv run python -m benchmarks.longmemeval_v2_ablations release-rig-blocked-authority \
  --receipt benchmarks/results/longmemeval-v2-release/sibyl-v1-3-aa-rig-blocked-receipt.json \
  --ledger benchmarks/results/longmemeval-v2-release/sibyl-v1-3-aa-dispatch-ledger.json \
  --output /tmp/sibyl-1-3-rig-blocked-authority.json
```

The output is the `rig_blocked` authority projection. Keep it with the RC gate receipt. The
authority carries no score, no paid-work permission, and no stack or arm contract; it exists to
prove that the benchmark stop was observed, not asserted.

To reseal the receipt after a new dispatch, refresh the ledger and run
`tools/bench/longmemeval_v2_rig.py rig-blocked --ledger ... --output ... --verify-github`; a seal
without `--verify-github` records `ledger_provenance: unverified` and the authority command rejects
it before touching the network.

To refresh the ledger from GitHub (only if a new dispatch is added), fetch each controller run with
`gh api repos/hyperb1iss/sibyl/actions/runs/<id>`, find its builder with
`gh run list --workflow longmemeval-v2.yml --limit 200 --json databaseId,displayTitle` filtered on
the display title prefix `LongMemEval V2 aa-<controller id> `, fetch that builder run and its
`/jobs?per_page=100`, and project the fields the ledger schema names. The tool itself never fetches.

## 3. Gates at the candidate commit

Run every gate uncached on the pinned checkout. A cached moon task that returns in milliseconds
proves nothing; use `--force` where the task allows it.

```bash
moon run release-version-validate -- 1.3.0
moon run --force sync-versions-check
moon run --force release-workflow-test
moon run --force root:bench-gate-test root:inventory-lint root:inventory-typecheck
moon run --force bench-longmemeval-v2-release-ci-test
moon run --force root:doc-claim-gate-test
moon run --force :check
moon run e2e:test-browser
moon run inventory-test
moon run doc-claim-gate
```

The `sync-versions-check` task must pass at `1.2.2` before the cut; the Release workflow bumps
`VERSION` itself and reruns the same check after `moon run sync-versions`, then proves the bump
touched only the pins listed by `tools/release/sync_versions.py --list-targets`. The browser gate
needs the production-shaped fixture and a running frontend. The Helm gate needs Helm installed.

Recorded on branch `nova/sibyl-1-3-rig-blocked-dispatches` at `b304c204` (not on the candidate):
`root:bench-gate-test` 897 passed in 27 seconds, `bench-longmemeval-v2-release-ci-test` 9 passed,
`root:inventory-lint` and `root:inventory-typecheck` clean, `:lint` 12 tasks completed. Those
numbers describe the receipt branch and do not stand in for the candidate run.

Recompute the live preflight immediately before the cut and keep the links:

```bash
gh pr list --state open --json number,title,url,isDraft,reviewDecision,statusCheckRollup
gh issue list --state open --json number,title,url,labels
```

## 4. Dry cut

The Release workflow runs the image CVE gate before the release job exists. The dry run then
validates the version, applies a pin-only bump inside the runner without pushing it, runs
`moon run :check`, verifies the same-commit nightly, and uploads the RC gate receipt. It creates no
tag, no release, and no publish run.

```bash
gh workflow run release.yml \
  --ref main \
  -f version=1.3.0 \
  -f dry_run=true \
  -f nightly_run_id=33244786444
```

Confirm before reading the result:

- the run's head SHA is the candidate;
- the `Image CVE gate` scan jobs passed for `api` and `web` on both `amd64` and `arm64`;
- the `Determine version` step reports `1.2.2` to `1.3.0` and no existing `v1.3.0` tag;
- the `Run RC gate bundle` step passed;
- the `Validate same-SHA Nightly Regression` step used run `33244786444`;
- the `rc-gate-receipt-<sha>` artifact exists and records `dry_run: true`.

A dry cut that fails at any step returns this runbook to section 3. Do not retry the real cut on the
strength of a partial dry run.

## 5. Approval gate

Three state changes need Bliss's explicit go, given after the dry cut passes and named individually:
the version bump commit from 1.2.2 to 1.3.0 on `main`, the `v1.3.0` tag, and the publish run. A
green dry cut is evidence for the request, not the approval. No agent tags, publishes, or pushes to
`main` on its own.

The release notes carry no benchmark number. The public claim gate rejects a score that lacks a
citable manifest entry, and the rig-blocked receipt forbids one, so a benchmark claim cannot pass
either check.

## 6. Cut and publish

Dispatch the same workflow without `dry_run`:

```bash
gh workflow run release.yml \
  --ref main \
  -f version=1.3.0 \
  -f dry_run=false \
  -f nightly_run_id=33244786444
```

The v1.2.2 topology applies unchanged:

1. The image CVE gate scans images built from the candidate before anything is named.
2. The release job creates one pin-only commit, `chore(release): cut 1.3.0`, tags it `v1.3.0`, and
   pushes both.
3. It creates a GitHub prerelease that is not marked latest.
4. It dispatches `publish.yml` for the tag.
5. Publish runs its own RC gate on the tagged checkout, then builds, scans, signs, and distributes
   Python packages, Homebrew, AUR, both container registries, and Helm.
6. The final publish job attaches the evidence assets while still a prerelease, then a files-free
   promotion flips the release to full and latest. Anything failing before that point leaves a
   visible prerelease and never presents 1.3.0 as current.

Do not create, move, or replace the tag by hand while either workflow is running.

## 7. Verify

```bash
git fetch --tags origin
git rev-list -n 1 v1.3.0
gh release view v1.3.0 --json url,isPrerelease,isDraft,tagName,targetCommitish
```

The tag commit must be the pin-only bump whose parent is the candidate. Check every channel from the
publish summary:

- the three Python packages and their checksums;
- both container registries and their matching digests;
- the Cosign signatures and the uploaded signature receipt;
- the Homebrew formula and the AUR package;
- the Helm chart archives and repository index;
- the release assets and the final release body.

The release is complete only when the GitHub object is no longer a prerelease.

## 8. Rollback points

The v1.2.2 release is the last known-good version on every channel.

| Channel        | Rollback target                                                                                                                                                                  |
| -------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Git            | Tag `v1.2.2` at `229576453dffa5f883fec5ed2743c58aae84f6f5` (pin-only bump of validated base `d71087a14aa13d0c9062d3e097ba649c5e70af1f`)                                          |
| GitHub release | [v1.2.2](https://github.com/hyperb1iss/sibyl/releases/tag/v1.2.2), published 2026-08-14T03:49:12Z, full release, with 17 evidence assets                                         |
| Images         | `ghcr.io/hyperb1iss/sibyl-api:1.2.2`, `ghcr.io/hyperb1iss/sibyl-web:1.2.2`, and the `docker.io/hyperb1iss` mirrors, with Cosign receipts on the release                          |
| PyPI           | `sibyl-core==1.2.2`, `sibyl-dev==1.2.2`, `sibyld==1.2.2`                                                                                                                         |
| Homebrew       | Tap formula `sibyl-homebrew-1.2.2.rb` (attached to the release)                                                                                                                  |
| AUR            | `sibyl` PKGBUILD 1.2.2 (attached to the release as `sibyl-1.2.2-PKGBUILD`)                                                                                                       |
| Helm           | `sibyl/sibyl --version 1.2.2` and `sibyl/sibyl-surrealdb --version 1.2.2` from the gh-pages repository                                                                           |
| Workflows      | Release run [31767365088](https://github.com/hyperb1iss/sibyl/actions/runs/31767365088), publish run [31767945736](https://github.com/hyperb1iss/sibyl/actions/runs/31767945736) |

Before the tag push, cancel and fix the candidate; no public state exists. After the version commit
reaches `main` but before the tag, stop and inspect; the commit may stay and the same version may be
rerun once gates pass and the tag is still absent. After the tag or any package is public, never
move the tag or replace an immutable artifact: mark the release a prerelease, pause the remaining
channels, and fix forward with `1.3.1`. For a runtime rollback, redeploy the 1.2.2 image and chart
pins above and restore data only from a verified backup if 1.3.0 changed persisted state.

## 9. Deferred benchmark work

None of this gates the release. The 1.3 benchmark outcome on record is rig blocked by dispatch
exhaustion, with no noise floor, no anchor, no race or render decision, and no score claim. Each
item below is a Sibyl task in project `project_05eb5c8c782a`.

| Task       | Work                                                                                                                                                                              |
| ---------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `2a129447` | Hybrid fulltext lane: `fc17af80` on `nova/sibyl-1-3-hybrid-fulltext-lane` splits `_fulltext_search` into four per-field bounded top-k statements; reviewed, pull request pending. |
| `8a56f5c7` | Reproduce the SurrealDB 3.2.3 residency growth from the r5 snapshot and measure the fulltext fix's live latency. In progress; preliminary idle footprint about 6.6 GiB RSS.       |
| `defd7d5b` | Native SurrealDB grows to 33 GB resident under sustained bench load; SELECTs degrade tenfold.                                                                                     |
| `e2ffa177` | Operational ingest write path collapses at table depth under bulk upserts against fulltext and HNSW index maintenance.                                                            |
| `0b639fc2` | Move the bench stack off the laptop. Personal accounts cannot use larger hosted runners and the repository has zero self-hosted runners.                                          |
| `f71e152e` | This receipt, status, and runbook lane.                                                                                                                                           |
