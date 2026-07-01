# Sibyl 1.0 RC Plan

> ⚠️ **HISTORICAL (as of 2026-07-01).** This plan's work has shipped (Sibyl is at v1.0.2). Kept as a
> historical receipt; it does not describe current status. See
> [`SIBYL_NORTHSTAR.md`](SIBYL_NORTHSTAR.md) for current product truth.

- Status: active execution plan
- Created: 2026-05-19
- Release target: `1.0.0-rc.1`
- Release floor: `v0.10.0`, published 2026-05-17
- Current focus: v1.0 RC Evidence Freeze
- Parent roadmap: [`SIBYL_1_0_ROADMAP.md`](SIBYL_1_0_ROADMAP.md)
- Evidence checklist:
  [`SIBYL_POST_V010_RELEASE_REMAP_SPEC.md`](SIBYL_POST_V010_RELEASE_REMAP_SPEC.md)

## 1. Release Promise

Sibyl 1.0 RC is ready when the public claim surface is frozen, every release claim has a current
receipt from the tag candidate, install paths tell one story, and the release cut itself cannot
bypass the gates that make those claims true.

The RC does not add another large product slice. It closes the loop on evidence, release mechanics,
default-runtime truth, install rehearsal, and rollback readiness.

## 2. Current State

The project is close to RC.

Shipped and landed:

- `v0.10.0` is published and is the planning floor.
- The active roadmap and remap spec point at v1.0 RC Evidence Freeze.
- The v0.11 through v0.13 gate surfaces exist as Moon tasks.
- Current main CI and docs deployment are green on the latest pushed head.
- The supported package dependency graph no longer contains `graphiti-core` or `graphiti_core`.
- No open GitHub issues or pull requests are currently tracking release blockers.
- Release gate wiring is present: Release runs `moon run :check`, validates a same-SHA Nightly
  Regression receipt before live tag/publish steps, and uploads an `rc-gate-receipt-*` artifact.
- `moon run :check` and `moon run :test` both aggregate the RC gate tests named by this plan.
- The local task graph is readable: the v1.0 RC Evidence Freeze epic has one active RC task and no
  RC todo or blocked tasks.

Not yet RC-ready:

- Final same-SHA Nightly Regression and release dry-run receipts must be refreshed whenever the
  candidate SHA moves.
- Live release dispatch still requires Bliss's explicit go-ahead.
- Post-publish checks for GitHub Release, PyPI pages, Docker manifests, docs install copy, and clean
  installs can only run after the RC is published.

## 3. RC Success Criteria

The RC can be cut only when all of these are true:

- Active architecture docs agree that v1.0 RC is an Evidence Freeze.
- The task graph shows v1.0 RC work, not stale v0.8, v0.10, v0.11, or v0.12 ghosts.
- Every RC gate has a current receipt from the final tag candidate.
- `moon run :check` covers every gate test that protects an RC claim.
- Nightly regression is green on the final tag candidate.
- Graphiti Core is absent from supported runtime imports, tests, package metadata, install docs,
  Docker, Helm, CI, and default dev paths.
- Any remaining Graphiti-named compatibility surface is either removed from active default-runtime
  surfaces or explicitly accepted as a compatibility shim with a documented rationale.
- Redis remains optional for local single-machine usage and opt-in for distributed coordination.
- Backup/restore proves auth, graph, content, raw memory, tasks, settings, source imports, and
  synthesis provenance survive a round trip.
- Source ingest, corpus rehearsal, synthesis, context quality, workspace trust, autonomy, auth
  session behavior, reflection quality, overview performance, and memory trust all have receipts.
- Quickstart, Docker, Helm, package install, docs, and release notes agree on how users install and
  run the RC.
- The release workflow cannot produce a tag or publish artifacts from an ungated SHA.
- The rollback path is explicit before publish.

## 4. Release Blockers

### B1. Release Cut Can Bypass Gates

The release workflow currently owns version bump, release-note generation, tag creation, GitHub
release creation, and publish dispatch. It does not enforce the RC gate bundle before tag creation.
The publish workflow builds artifacts but does not replace the missing release gate.

Required outcome:

- A release cannot tag or publish unless the same SHA has passed the RC gate bundle.

Acceptable fixes:

- Run the curated RC gate bundle inside the release workflow before the tag step.
- Require and validate a green CI run for the exact SHA being released.

Exit criteria:

- Dry-run output describes only what actually ran.
- Live release output records the gate receipt or same-SHA CI receipt.
- The release commit message follows the repo's conventional-commit policy.

### B2. `:check` Does Not Cover Every RC Gate Test

The aggregate check task already covers many release gates, but it does not include every gate test
named by the RC matrix.

Required outcome:

- `moon run :check` includes the test variants for:
  - `autonomy-gate`
  - `reflection-quality-gate`
  - `auth-session-gate`
  - `overview-perf-gate`

Exit criteria:

- `moon run :check` fails if any RC gate test fails.
- The RC matrix and Moon aggregation agree.

### B3. Nightly Regression Is Not Pinned To The Candidate SHA

The current nightly receipt is useful but not sufficient for RC. Evidence Freeze receipts must match
the exact tag candidate.

Required outcome:

- Nightly regression is green on the final RC SHA.

Exit criteria:

- Baseline parity and live graph regression receipts cite the candidate SHA.
- Benchmark artifacts from the nightly run are retained or linked in the release packet.

### B4. Task Graph Receipt Is Missing

The docs require the project task graph to show current RC work. The repository is linked locally,
but the API-backed task and recall surfaces require the Sibyl server.

Required outcome:

- The RC task graph is readable and current.

Exit criteria:

- The v1.0 RC epic exists and reflects the remaining packets.
- Active tasks match this plan.
- Obsolete release ghosts are completed, archived, or explicitly labeled historical.

### B5. Graphiti Boundary Is Too Soft

Graphiti Core is no longer a supported dependency, but user-facing copy, environment docs, enum
names, config descriptions, and compatibility modules still mention Graphiti. For 1.0, this needs a
hard release boundary rather than a fuzzy migration vibe.

Required outcome:

- Active default-runtime surfaces stop presenting Graphiti as a supported runtime choice, or the
  remaining named compatibility shims are documented as accepted release risk.

Exit criteria:

- Supported runtime grep has no Graphiti Core imports or dependencies.
- Active install docs do not instruct new users to run Graphiti.
- UI copy describes native graph embeddings and compatibility history accurately.
- Any retained `graphiti` mode/config naming has a documented owner, reason, and removal condition.

### B6. Redis-Optional Local Runtime Needs A Receipt

The RC promise includes SurrealDB as the only required default data plane. Redis can remain
available for distributed coordination, but local single-machine usage cannot require it.

Required outcome:

- Local mode can enqueue and complete representative work without Redis.

Exit criteria:

- Crawl, entity creation, task update, backup, and reflection maintenance can run through the local
  coordination path.
- Redis mode remains documented as opt-in for distributed deployments.
- Default install docs do not imply Redis is required for a local RC install.

## 5. Work Plan

### Wave 1. Release Gate Wiring

Goal: make the release cut enforce the same truth as the RC checklist.

Tasks:

1. Add the missing RC gate tests to `moon run :check`.
2. Decide whether the release workflow runs the RC gate bundle directly or validates same-SHA CI.
3. Update release dry-run text so it never claims checks ran when they did not.
4. Update the release bot commit message to match conventional commits and include a useful body.
5. Keep the release version regex compatible with `1.0.0-rc.1`.

Verification:

- `moon query tasks --id check` shows all RC gate test dependencies.
- Release workflow dry-run cannot create a misleading success summary.
- Release workflow live path cannot tag before gate enforcement or same-SHA validation.
- Release commit subject and body satisfy repo commit policy.

### Wave 2. Task Graph And Planning Truth

Goal: align durable task state with the current RC.

Tasks:

1. Restore task graph readability.
2. Confirm the v1.0 RC epic and active tasks match this plan.
3. Close, archive, or relabel stale tasks from shipped release packets.
4. Record the remaining blockers as concrete RC tasks.

Verification:

- `sibyl context` shows the linked Sibyl project.
- The RC epic shows current work.
- Doing and todo task lists contain RC packets, not stale shipped work.
- Recall for "Sibyl 1.0 RC" returns this plan and the active blockers.

### Wave 3. Default Runtime Boundary

Goal: make Surreal-only runtime closure claim-safe.

Tasks:

1. Clean user-facing Graphiti wording in active UI and docs where it describes current default
   behavior.
2. Decide the fate of `SIBYL_RETRIEVAL_MODE=graphiti` and `NativeRetrievalMode.GRAPHITI` for RC.
3. Give any retained compatibility shim an owner, rationale, and removal condition.
4. Confirm no package metadata, lockfile, Docker, Helm, CI, or install path references Graphiti
   Core.
5. Confirm local single-machine docs do not require Redis.

Verification:

- RC grep audit for `graphiti[_-]core` returns no supported runtime, test helper, package metadata,
  install doc, or active guide match.
- Package metadata and lockfile grep returns no Graphiti Core dependency.
- Active install docs and UI copy describe native SurrealDB behavior.
- Redis references are either migration/coordination opt-in text or removed from default install
  paths.

### Wave 4. Data Integrity And Runtime Receipts

Goal: prove the runtime claims that would be expensive to discover broken after release.

Tasks:

1. Run inventory gates for supported-runtime closure.
2. Run backup/restore gate and inspect the receipt scope.
3. Prove Redis-optional local coordination for representative background work.
4. Run memory trust and trust-control gates.
5. Run auth-session gate.

Verification:

- `moon run inventory-check`
- `moon run inventory-typecheck`
- `moon run inventory-test`
- `moon run backup-restore-gate`
- `moon run memory-trust-gate`
- `moon run trust-control-gate`
- `moon run auth-session-gate`
- Local coordination receipt covers crawl, entity creation, task update, backup, and reflection
  maintenance without Redis.

### Wave 5. Memory Product Receipts

Goal: prove the product loops named by the release notes.

Tasks:

1. Refresh source adapter and large-corpus receipts.
2. Refresh source-grounded synthesis receipts.
3. Refresh autonomy and reflection-quality receipts.
4. Refresh context-quality and workspace-trust receipts.
5. Refresh overview performance receipt.
6. Refresh benchmark ledger receipt.

Verification:

- `moon run adapter-ingest-gate`
- `moon run large-corpus-rehearsal`
- `moon run synthesis-gate`
- `moon run autonomy-gate`
- `moon run reflection-quality-gate`
- `moon run context-quality-gate`
- `moon run workspace-trust-gate`
- `moon run overview-perf-gate`
- `moon run bench-gate`

### Wave 6. Install Rehearsal

Goal: prove a user can install and run the RC from the published surfaces.

Tasks:

1. Rehearse the documented quickstart from a clean environment.
2. Rehearse the Python package install path for CLI and daemon entrypoints.
3. Rehearse Docker image startup with the documented compose path.
4. Rehearse Helm chart rendering and linting.
5. Confirm whether Homebrew is an RC target. If it is, rehearse it. If it is not, remove it from the
   RC checklist and release notes.
6. Build the docs site and verify install pages match the release artifacts.

Verification:

- `sibyl --version` reports the RC version from a clean package install.
- `sibyld --version` reports the RC version from a clean package install.
- Docker images start and expose expected health endpoints.
- Helm chart renders without default-runtime legacy services.
- Docs build succeeds and install instructions match available artifacts.

### Wave 7. Candidate Validation

Goal: prove the final SHA is the one being released.

Tasks:

1. Run or cite CI for the exact candidate SHA.
2. Run nightly regression on the exact candidate SHA.
3. Run `moon run :check` after gate aggregation is fixed.
4. Verify docs deploy or docs build for the candidate SHA.
5. Build the release packet as a claim-to-receipt matrix.

Verification:

- Same-SHA CI is green.
- Same-SHA nightly regression is green.
- `moon run :check` is green.
- Release packet maps every public claim to a command, artifact, or CI URL.

### Wave 8. Release Cut And Post-Publish Checks

Goal: cut the RC without ambiguity and prove published artifacts work.

Tasks:

1. Prepare release notes from the claim-to-receipt matrix.
2. Cut `1.0.0-rc.1` only after explicit release go-ahead.
3. Verify the GitHub release body and tag.
4. Verify PyPI package pages and package metadata.
5. Verify Docker multi-arch manifests and tags.
6. Verify docs install page references the RC correctly.
7. Verify clean installs of CLI and daemon entrypoints from published artifacts.
8. Record residual risks and accepted follow-up items.

Verification:

- GitHub release exists for `v1.0.0-rc.1`.
- PyPI has matching `sibyl-core` and `sibyl-dev` RC packages.
- Docker tags exist for API and web images.
- Clean install entrypoints report the RC version.
- Release notes include receipts and residual risks.

## 6. Release Packet

The working packet lives in [`SIBYL_1_0_RC_RELEASE_PACKET.md`](SIBYL_1_0_RC_RELEASE_PACKET.md).

The release packet should contain this matrix:

| Claim                                   | Required Receipt                                                    |
| --------------------------------------- | ------------------------------------------------------------------- |
| Active docs agree                       | Docs lint/build and links to active planning docs                   |
| Task graph is current                   | RC epic and active task output                                      |
| Source ingest is current                | `adapter-ingest-gate` and `large-corpus-rehearsal` receipts         |
| Synthesis is source-grounded            | `synthesis-gate` receipt                                            |
| Automatic memory is policy-safe         | `autonomy-gate`, `memory-trust-gate`, `trust-control-gate` receipts |
| Sessions are boring                     | `auth-session-gate` receipt                                         |
| Reflection quality is current           | `reflection-quality-gate` receipt                                   |
| Context and workspace trust are current | `context-quality-gate`, `workspace-trust-gate` receipts             |
| Overview performance is current         | `overview-perf-gate` receipt                                        |
| Surreal-only runtime holds              | Inventory gates, package grep, docs grep, install rehearsal         |
| Redis is optional locally               | Local coordination receipt                                          |
| Backup/restore is release-gated         | `backup-restore-gate` receipt                                       |
| Benchmark ledger is claim-safe          | `bench-gate` and nightly regression receipts                        |
| Install surfaces work                   | Quickstart, package, Docker, Helm, docs receipts                    |
| Release cut is gated                    | Same-SHA CI or release workflow gate receipt                        |
| Rollback is ready                       | Rollback owner, tag/artifact plan, and known-good SHA               |

## 7. Release Decision

Use this decision once Wave 7 is complete:

```text
Ship v1.0 RC:
  yes/no
Version:
  1.0.0-rc.1
Candidate SHA:
  <sha>
Blocking packet:
  <smallest remaining blocker, if no>
Proof command or receipt:
  <command, artifact, CI URL, or release packet section>
Residual risk:
  <accepted risks and owners>
Rollback target:
  <known-good SHA or release>
```

## 8. Recommendation

Do not cut the RC until the final candidate SHA has same-SHA CI, LongMemEval V2, Nightly Regression,
and release dry-run receipts, and Bliss explicitly approves the live dispatch.

The project is close enough that the remaining work should stay narrow. The highest-leverage path
is:

1. keep the candidate SHA still;
2. refresh the external same-SHA receipts for that exact SHA;
3. dispatch Release with the matching Nightly Regression run ID after explicit approval;
4. verify published artifacts and clean installs before announcing the RC.
