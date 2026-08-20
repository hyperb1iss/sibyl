# Release runbook

Sibyl releases start from an exact candidate commit. The Release workflow may add one generated
version commit, but that commit can touch only `VERSION` and the pins listed by
`tools/release/sync_versions.py --list-targets`. The workflow proves that boundary before it creates
a tag.

## Choose the version

Release versions use one of two forms:

- `X.Y.Z` for a final release
- `X.Y.Z-rc.N` for a release candidate, where `N` starts at 1

The release parser rejects aliases such as `alpha`, `beta`, `preview`, and `pre`. Validate the
version before running any release job:

```bash
moon run release-version-validate -- X.Y.Z
moon run sync-versions-check
moon run release-workflow-test
```

## Check live project state

Recompute the GitHub preflight immediately before the cut. Record links to the queries and the time
of the check in the release notes or operator log. Do not copy a transient pull request or
Dependabot queue into a long-lived plan.

```bash
gh pr list --state open --json number,title,url,isDraft,reviewDecision,statusCheckRollup
gh issue list --state open --json number,title,url,labels
```

Resolve any open item that changes the release contract, upgrade path, security posture, or
published artifacts. Ordinary follow-up work can remain open when the release notes name it
explicitly.

## Fix the candidate commit

Fetch the target branch, confirm the worktree is clean, and record the exact commit before running
the gates:

```bash
git fetch origin main
git status --short
git rev-parse origin/main
```

Run the release gates on that commit. The browser gate needs the production-shaped fixture and a
running frontend. The Helm gate needs Helm installed.

```bash
moon run :check
moon run e2e:test-browser
moon run release-workflow-test
moon run inventory-test
moon run doc-claim-gate
```

Dispatch Nightly Regression for the same base commit. Keep its run ID. The Release workflow refuses
a successful nightly from a different commit.

## Check release claims

The workflow sends generated release notes through the public claim gate before it creates the
release. A benchmark number is publishable only when the benchmark manifest marks its artifact
citable and the doc claim gate passes against the same public claim corpus. A planned run, local
console output, or historical score is not release evidence.

The claim check writes `release-notes-claim-receipt.json`. Keep that file with the RC gate receipt.
If the check fails, edit the source claim or its evidence. Do not weaken the scanner or paraphrase a
rejected claim to evade it.

## Run a dry cut

Run the Release workflow with `dry_run` enabled first. Pass the exact Nightly Regression run ID:

```bash
gh workflow run release.yml \
  --ref main \
  -f version=X.Y.Z \
  -f dry_run=true \
  -f nightly_run_id=<run-id>
```

The dry run must complete the image CVE gate, version validation, RC gate, same-commit nightly
check, and receipt upload. It may create an ephemeral pin-only commit inside the runner, but it must
not push that commit, create a tag or GitHub release, or start a publish run.

## Cut and publish

Dispatch the same workflow without `dry_run`:

```bash
gh workflow run release.yml \
  --ref main \
  -f version=X.Y.Z \
  -f dry_run=false \
  -f nightly_run_id=<run-id>
```

The workflow performs these state changes in order:

1. It creates a pin-only version commit when `VERSION` differs.
2. It tags the candidate and pushes the version commit and tag.
3. It creates a GitHub prerelease that is not marked latest.
4. It dispatches `publish.yml` for the tag.
5. The publish workflow scans, signs, and publishes the exact release channels.
6. The publish workflow promotes the GitHub release only after every channel succeeds.

Do not create or replace the tag by hand while either workflow is running.

## Verify the published release

Record the tag commit and compare it with the candidate SHA reported by the Release workflow:

```bash
git fetch --tags origin
git rev-list -n 1 vX.Y.Z
gh release view vX.Y.Z --json url,isPrerelease,isDraft,tagName,targetCommitish
```

Check every published channel from the publish summary:

- the three Python packages and their checksums
- both container registries and their matching digests
- Cosign signatures and uploaded signature receipt
- Homebrew formula and AUR package
- immutable Helm chart archives and repository index
- GitHub release assets and final release body

The release is complete only when the GitHub object is no longer a prerelease and every listed
artifact points at the recorded version.

## Keep the receipts

Store these values together:

- validated base SHA and generated candidate SHA
- release tag and tag commit
- Nightly Regression run ID and URL
- RC gate and release notes claim receipts
- image scan results, image digests, and Cosign receipt
- Python, Homebrew, AUR, and Helm package evidence
- live pull request and issue preflight time and links
- deferred work with issue links

The receipt set must state whether benchmark work passed, failed, or remained inconclusive. A
blocked benchmark does not block the product release, but it does block a benchmark score claim.

## Stop and rollback points

Before the tag push, cancel the workflow and fix the candidate. No public release state exists.

After the version commit reaches `main` but before the tag exists, stop and inspect the failed step.
The generated commit can remain. Rerun the same version only after the candidate gates pass and the
tag is still absent.

After the tag or any package is public, do not move the tag or replace an immutable artifact. Mark a
broken GitHub release as a prerelease, pause remaining channels where possible, and fix forward with
a new patch version. Use package yanks or registry-specific withdrawal controls only for a confirmed
security or integrity problem, and record the reason in the incident and replacement release.

For a runtime rollback, redeploy the last known-good immutable image and chart versions. Restore
data only from a verified backup when the new release changed persisted state. Record the backup,
write-freeze boundary, and replay decision before reopening writes.
