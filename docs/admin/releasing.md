# Release runbook

Every Sibyl release goes through the Release workflow (`.github/workflows/release.yml`). A release
takes two dispatches of it: a dry run, then, after the maintainer approves, the real run. The
workflow proves every release gate on the exact candidate commit, so nobody runs a gate list by
hand. A local gate run is a debugging aid and never release evidence.

The workflow may add one generated version commit, and that commit can touch only `VERSION` and the
pins listed by `tools/release/sync_versions.py --list-targets`. The workflow proves that boundary
before it creates a tag.

## What the workflow proves

Each dispatch runs these gates on the dispatched commit, the candidate:

| Job or step                 | What it proves                                                                                                                         |
| --------------------------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| Preflight                   | A real cut runs only on `main`. A dry run may rehearse any branch.                                                                     |
| Image CVE gate              | The `api` and `web` images, built for `amd64` and `arm64`, carry no fixable high or critical advisory.                                 |
| E2E gate                    | The API, CLI and browser end-to-end suites pass against a production-shaped fixture (SurrealDB, API, worker, seeded corpus, frontend). |
| Nightly Regression evidence | A Nightly Regression run on the candidate finished with every job green. The job finds one, or dispatches one and waits for it.        |
| Determine version and bump  | The version parses, its tag does not exist, and the bump commit touches only generated pins.                                           |
| Run RC gate bundle          | `moon run :check`, the LongMemEval V2 release CI test and the doc claim gate pass, all forced past the moon cache.                     |
| Release notes               | On the real run, the generated notes pass the public claim gate.                                                                       |
| Validate same-SHA Nightly   | The cited nightly is checked once more, as the last gate before the tag.                                                               |
| Tag, release, publish       | On the real run only: the version commit and tag are pushed, a prerelease is created, and `publish.yml` starts.                        |

The RC bundle forces every task because the moon output cache is restored across runs. Without
`--force`, a gate whose declared inputs miss a real dependency would replay a pass recorded on
another commit. `:check` holds the root trust gates, every project's lint, typecheck and tests, and
the Helm contract tests, which run against the pinned Helm the workflow installs.

The nightly evidence rule is stricter than the run's own conclusion. The daily schedule skips
Restore To Scratch, and GitHub still reports that run as a success, so it does not count. A run
counts only when Baseline Parity, Live Graph Regression and Restore To Scratch all succeeded on the
candidate commit, which means a dispatched run or the Monday schedule. A nightly that failed on the
candidate stops the release instead of being dispatched again until green.

## Choose the version

Release versions use one of two forms:

- `X.Y.Z` for a final release
- `X.Y.Z-rc.N` for a release candidate, where `N` starts at 1

The release parser rejects aliases such as `alpha`, `beta`, `preview`, and `pre`. The release job
validates the version and checks that its tag is free before it creates the version commit, so no
local check is needed. Leaving `version` empty bumps the patch (or the part named by the `bump`
input) from `VERSION`.

## Check live project state

Recompute the GitHub preflight immediately before the cut. This is a human judgment the workflow
does not make. Record links to the queries and the time of the check in the release notes or
operator log. Do not copy a transient pull request or Dependabot queue into a long-lived plan.

```bash
gh pr list --state open --json number,title,url,isDraft,reviewDecision,statusCheckRollup
gh issue list --state open --json number,title,url,labels
```

Resolve any open item that changes the release contract, upgrade path, security posture, or
published artifacts. Ordinary follow-up work can remain open when the release notes name it
explicitly.

## Dispatch the dry run

Dispatch the workflow on `main` with `dry_run` enabled. Anyone with write access may do this,
including an agent, because a dry run changes no remote release state.

```bash
gh workflow run release.yml --ref main -f version=X.Y.Z -f dry_run=true
gh run list --workflow release.yml --limit 1
gh run watch <run-id>
```

Leave `nightly_run_id` empty. The workflow cites a same-SHA nightly that already passed, waits for
one that is running, or dispatches Nightly Regression on the candidate and waits for it. Pass
`nightly_run_id` only to cite one specific run. The workflow then validates that run and refuses it
without falling back to a search.

A dry run usually takes 25 to 35 minutes. The image, E2E and nightly jobs run in parallel, and the
release job's forced RC bundle takes most of the rest. Each dry run gets its own concurrency group,
so it never queues behind or cancels a real cut.

Evidence never carries across commits. If `main` moves before the cut, the new head is the
candidate, and it needs its own dry run.

## Read the dry run

Confirm each of these before reading the result as a pass:

- the run's head SHA is the candidate (`gh run view <run-id> --json headSha`)
- the Preflight, Image CVE gate, E2E gate, Nightly Regression evidence and Release jobs all passed,
  including all four image scans (`api` and `web` on `amd64` and `arm64`)
- the Nightly Regression evidence summary names a run on the candidate with Baseline Parity, Live
  Graph Regression and Restore To Scratch all `success`
- the `Determine version` step reports the expected version change and no existing tag
- the `Run RC gate bundle` step passed
- the step summary ends with "No version commit, tag, release, or publish was created."
- the `rc-gate-receipt-<sha>` artifact records `dry_run: true`, a `base_sha` equal to the candidate,
  `success` for every gate, and the cited nightly run, with `doc-claim-receipt.json` beside it

```bash
gh run view <run-id>
gh run download <run-id> --pattern 'rc-gate-receipt-*'
```

A dry run that fails at any step blocks the release. Read the failing job's log, fix the cause on
`main`, and dispatch a new dry run on the new head. Do not start the real cut on the strength of a
partial dry run. The appendix maps each gate to the moon task that reproduces it locally.

## Approval gate

Three state changes need the maintainer's explicit approval, given after the dry run passes and
named individually: the version bump commit on `main`, the version tag, and the publish run. A green
dry run is evidence for the request, not the approval. A single non-dry dispatch performs all three,
so every approval must be in hand before that dispatch. An agent may dispatch the dry run and
present its evidence, but no agent tags, publishes, or pushes to `main` on its own.

## Cut and publish

Dispatch the same workflow without `dry_run`, on the commit the dry run proved:

```bash
gh workflow run release.yml --ref main -f version=X.Y.Z -f dry_run=false
```

The real run proves every gate again on the same candidate rather than replaying the dry run. It
cites the same nightly when that run is still the latest verdict on the commit. Then it performs
these state changes in order:

1. It creates a pin-only version commit when `VERSION` differs.
2. It tags the candidate and pushes the version commit and tag.
3. It creates a GitHub prerelease that is not marked latest.
4. It dispatches `publish.yml` for the tag.
5. The publish workflow runs its own RC gate on the tagged checkout, then scans, signs, and
   publishes the exact release channels.
6. The final publish job attaches the evidence assets while the release is still a prerelease. A
   separate step that carries no files then promotes the release to full and latest.

Anything that fails before the promotion step leaves a visible prerelease that is never presented as
the current version. Do not create or replace the tag by hand while either workflow is running.

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

The `rc-gate-receipt-<sha>` artifact of the real run holds the base and candidate SHAs, every gate
result, the cited Nightly Regression run, the release notes claim receipt and the doc claim receipt.
Store it together with:

- the release tag and tag commit
- image scan results, image digests, and Cosign receipt
- Python, Homebrew, AUR, and Helm package evidence
- live pull request and issue preflight time and links
- deferred work with issue links

The receipt set must state whether benchmark work passed, failed, or remained inconclusive. A
blocked benchmark does not block the product release, but it does block a benchmark score claim.

## Check release claims

The workflow sends generated release notes through the public claim gate before it creates the
release. A benchmark number is publishable only when the benchmark manifest marks its artifact
citable and the doc claim gate passes against the same public claim corpus. A planned run, local
console output, or historical score is not release evidence.

If the claim check fails, edit the source claim or its evidence. Do not weaken the scanner or
paraphrase a rejected claim to evade it.

## Stop and rollback points

Before the cut, record the last known-good version on every channel: its tag and tag commit, GitHub
release, both image registries, PyPI packages, Homebrew formula, AUR package, Helm charts, and the
release and publish run IDs. A rollback needs named targets, not a search.

Before the tag push, cancel the workflow and fix the candidate. No public release state exists.

After the version commit reaches `main` but before the tag exists, stop and inspect the failed step.
The generated commit can remain. Rerun the same version only after a dry run on the new head passes
and the tag is still absent.

After the tag or any package is public, do not move the tag or replace an immutable artifact. Mark a
broken GitHub release as a prerelease, pause remaining channels where possible, and fix forward with
a new patch version. Use package yanks or registry-specific withdrawal controls only for a confirmed
security or integrity problem, and record the reason in the incident and replacement release.

For a runtime rollback, redeploy the last known-good immutable image and chart versions. Restore
data only from a verified backup when the new release changed persisted state. Record the backup,
write-freeze boundary, and replay decision before reopening writes.

## Appendix: reproduce a failing gate locally

Use these only to debug a gate the workflow failed. A local pass is not release evidence, and the
fix still has to land on `main` and pass a new dry run. Pass `--force` so moon runs the task instead
of replaying a cached result.

| Failing gate                | Reproduce with                                                                                       |
| --------------------------- | ---------------------------------------------------------------------------------------------------- |
| Run RC gate bundle          | `moon run :check --force`, or the failing project task, such as `moon run core:test --force`         |
| LongMemEval V2 release CI   | `moon run bench-longmemeval-v2-release-ci-test --force`                                              |
| Doc claim gate              | `moon run doc-claim-gate --force`                                                                    |
| Helm contracts              | `moon run helm-test --force`, with Helm 3 on `PATH` (the tests skip without it)                      |
| Version or pin sync         | `moon run release-version-validate -- X.Y.Z` and `moon run sync-versions-check`                      |
| Workflow contract           | `moon run release-workflow-test --force`                                                             |
| E2E gate                    | Start the fixture the `e2e-gate` job starts, then run its two forced `moon run e2e:*` commands       |
| Nightly Regression evidence | `python3 -m tools.release.nightly_evidence verify --repo hyperb1iss/sibyl --sha <sha> --run-id <id>` |

The E2E fixture is SurrealDB (the `start-surrealdb` action), `sibyld serve` and `sibyld worker` in
`apps/api`, `moon run baseline-seed` and `moon run baseline-replay-runtime`, and a production build
of `apps/web` served with `pnpm start`. The `e2e-gate` job in the workflow lists the exact commands
and environment.
