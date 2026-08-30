# Sibyl 1.3 release status

- Snapshot date: 2026-08-30
- Release state: ready for dry cut pending approval
- Main commit: `012e7b55407d84ac2049914956b7017546d64b68`
- Latest released version: `1.2.2`
- Governing plan: [SIBYL_1_3_IMPLEMENTATION_PLAN.md](SIBYL_1_3_IMPLEMENTATION_PLAN.md)
- Runbook: [SIBYL_1_3_RELEASE_RUNBOOK.md](SIBYL_1_3_RELEASE_RUNBOOK.md)
- Supersedes: [SIBYL_1_3_RELEASE_STATUS_2026-08-26.md](SIBYL_1_3_RELEASE_STATUS_2026-08-26.md)

## Status

The 1.3 product implementation is merged and green. CI run
[33025662969](https://github.com/hyperb1iss/sibyl/actions/runs/33025662969) and Nightly Regression
run [33244786444](https://github.com/hyperb1iss/sibyl/actions/runs/33244786444) both passed on the
main commit above.

The paid benchmark path is stopped. Five A/A controller dispatches between 2026-08-25 and 2026-08-26
produced zero completed two-domain passes, and the rig now seals that fact as a rig-blocked receipt
with `blocked_reason: dispatch_exhausted`. The implementation plan names this receipt as the release
escape hatch: benchmark blockage stops paid benchmark work and every score claim, and does not block
the product release.

The release moves from hold to ready for a dry cut. The version bump from 1.2.2, the tag, and the
publish run each need Bliss's explicit approval. No benchmark number appears in the 1.3 release
claim.

## Corrections to the 2026-08-26 snapshot

The 08-26 document made three claims that the r5 artifacts it cited contradict. The re-read is
recorded in Sibyl memory as `error_pattern_bc484d448434`.

1. The peak memory is known. The 08-26 text reported only the 4.57 GiB end-of-run RSS and called the
   peak unknown. The service-diagnostics artifact for run
   [32998783818](https://github.com/hyperb1iss/sibyl/actions/runs/32998783818) contains
   `runtime-summary.txt`, which records a SurrealDB v3.2.3 container peak of 11.7 GiB RSS
   (`rss_peak_kib=12285320`), a cgroup-reported peak of 13.0 GB, a host minimum of 418 MiB
   available, and 1.4 GiB swapped, on the 15 GiB hosted runner. The 4 GiB RocksDB block cache from
   [PR 442](https://github.com/hyperb1iss/sibyl/pull/442) held (Surreal logged a 4.56 GB total
   memory limit), so the growth sits outside RocksDB accounting. Enterprise Small has the larger
   haystack (the July canary peaked at 8.03 GiB with the automatic cache) and crossed the ceiling.
2. The latency owner is known. The 08-26 text listed a 54.92 second median memory-query latency
   without an owner. The `sibyld.log` aggregation for the query phase shows
   `graph_entity_search:_fulltext_search` (one four-index OR per call, four calls per pack) ran 951
   times at a p50 of 12.9 seconds and a maximum of 29.9 seconds, which sums to the whole
   memory-query budget. The anchor web run had an 18.6 second p50. Memory starvation and the
   fulltext crawl are one defect, index scans on a swapping host.
3. The 29.31% anchor is not a valid comparator for the 23.33% r5 Web result. The sealed machine arm
   ran with `note_distillation=False`, `typed_stream_retrieval=False`, `typed_pool=typed`,
   `max_context_chars_per_item=18000`, and `defer_embeddings=True`. The anchor corpora ran with note
   distillation on (the only lever that survived replication, worth about 3.7 points per domain),
   typed stream retrieval on, and 12,000 characters per item. The historical notes-off web level was
   roughly 25 to 26%, and web's own three-pass span is 4.6 points, so the r5 number is inside the
   noise of its own configuration. The rig's `evidence_exposed` field was eligible on only 48 of 240
   questions, too thin to classify misses.

## The rig-blocked receipt

The receipt lives at
`benchmarks/results/longmemeval-v2-release/sibyl-v1-3-aa-rig-blocked-receipt.json` with its observed
ledger beside it at `benchmarks/results/longmemeval-v2-release/sibyl-v1-3-aa-dispatch-ledger.json`.
The ledger is the `gh api` projection of each controller run, its builder run, and the builder's
jobs, fetched 2026-08-30T07:07:30Z. The tool reads the ledger from disk. With `--verify-github` it
re-fetches every controller run, builder run, and job through `gh api`, re-discovers each
controller's builders from the workflow run listing, and requires field-for-field equality with the
ledger before sealing; only then does the receipt record `ledger_provenance: github_verified`. An
offline seal records `unverified`, and the release authorization projection rejects it.

| Field                     | Value                                                                     |
| ------------------------- | ------------------------------------------------------------------------- |
| Receipt digest            | `sha256:5850fd3f31ac8db9090b8e7bb8b62b349823500f0ebac6ebe3ba3a09cccc0df8` |
| Ledger digest             | `sha256:74339727e600a92a0157db517e6c5ce0b378bf08b4dc8ba2eb274818427a4299` |
| Ledger provenance         | `github_verified`: 10 runs and 29 jobs re-fetched 2026-08-30T07:35:29Z    |
| Status                    | `RIG_BLOCKED`                                                             |
| Blocked reason            | `dispatch_exhausted`                                                      |
| Attempts                  | 5 of 5 required                                                           |
| Completed two-domain runs | 0                                                                         |
| Branch                    | `main`                                                                    |
| Paid benchmark allowed    | `false`                                                                   |
| Score claim allowed       | `false`                                                                   |

| Attempt                     | Main commit | Controller run                                                              | Builder run                                                                 | Web Small | Enterprise Small | Combined receipt |
| --------------------------- | ----------- | --------------------------------------------------------------------------- | --------------------------------------------------------------------------- | --------- | ---------------- | ---------------- |
| `sibyl-v1.3-aa-20260825`    | `48b44836`  | [32888217656](https://github.com/hyperb1iss/sibyl/actions/runs/32888217656) | [32888310148](https://github.com/hyperb1iss/sibyl/actions/runs/32888310148) | cancelled | failure          | never created    |
| `sibyl-v1.3-aa-20260825-r2` | `48b44836`  | [32897996847](https://github.com/hyperb1iss/sibyl/actions/runs/32897996847) | [32898089824](https://github.com/hyperb1iss/sibyl/actions/runs/32898089824) | failure   | failure          | skipped          |
| `sibyl-v1-3-aa-20260825-r3` | `74ddb867`  | [32911050360](https://github.com/hyperb1iss/sibyl/actions/runs/32911050360) | [32911112960](https://github.com/hyperb1iss/sibyl/actions/runs/32911112960) | failure   | failure          | skipped          |
| `sibyl-v1-3-aa-20260825-r4` | `aed4adb2`  | [32921881380](https://github.com/hyperb1iss/sibyl/actions/runs/32921881380) | [32921948833](https://github.com/hyperb1iss/sibyl/actions/runs/32921948833) | failure   | failure          | skipped          |
| `sibyl-v1-3-aa-20260826-r5` | `7f31a330`  | [32998699090](https://github.com/hyperb1iss/sibyl/actions/runs/32998699090) | [32998783818](https://github.com/hyperb1iss/sibyl/actions/runs/32998783818) | success   | failure          | skipped          |

The attempts span four main commits because PRs [442](https://github.com/hyperb1iss/sibyl/pull/442),
[443](https://github.com/hyperb1iss/sibyl/pull/443), and
[444](https://github.com/hyperb1iss/sibyl/pull/444) landed between dispatches. The receipt lists
every commit rather than pretending the campaign ran on one. Every controller succeeded, so each row
is a real dispatch, and each builder's head commit matches its controller.

The tool rejects a ledger that fails any of these checks:

- a repository other than `hyperb1iss/sibyl` or a branch other than `main`, anywhere in the ledger;
- fewer than five successful controller dispatches;
- a builder whose two official domains both succeeded, or whose combined receipt succeeded (that is
  A/A data, not exhaustion);
- a builder whose commit or branch differs from the controller that dispatched it;
- a job whose run id, run attempt, or commit differs from its builder;
- a ledger fetched before its last run finished.

The existing span-instability path now carries `blocked_reason: span_unstable`, so the two
rig-blocked shapes cannot be mistaken for each other. The release authorization projection accepts
the receipt as a `rig_blocked` authority that carries no stack or arm contract, because a dispatch
that produced no pass observed neither. A contract test rebuilds the committed receipt from the
committed ledger and pins the run ids.

## What is complete

| Area                       | State    | Evidence                                                                                                                                                                                                                                                                                                                               |
| -------------------------- | -------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| One Surface behavior       | Complete | [PR 406](https://github.com/hyperb1iss/sibyl/pull/406) merged the lifecycle, predicate, authorization, and failure contract.                                                                                                                                                                                                           |
| Harmony and legacy cleanup | Complete | [PR 412](https://github.com/hyperb1iss/sibyl/pull/412) removed legacy runtime paths. [PR 413](https://github.com/hyperb1iss/sibyl/pull/413) converged the major services on canonical owners.                                                                                                                                          |
| MCP SDK 2                  | Complete | [PR 410](https://github.com/hyperb1iss/sibyl/pull/410) migrated the server and clients.                                                                                                                                                                                                                                                |
| Dependencies and toolchain | Complete | PRs [411](https://github.com/hyperb1iss/sibyl/pull/411), [414](https://github.com/hyperb1iss/sibyl/pull/414), [419](https://github.com/hyperb1iss/sibyl/pull/419), [432](https://github.com/hyperb1iss/sibyl/pull/432), and [435](https://github.com/hyperb1iss/sibyl/pull/435) refreshed dependencies and pinned the supported stack. |
| CLI pending writes         | Complete | [PR 438](https://github.com/hyperb1iss/sibyl/pull/438) added automatic replay. [PR 441](https://github.com/hyperb1iss/sibyl/pull/441) bound replay to credential lineage.                                                                                                                                                              |
| Sealed release evaluation  | Complete | PRs [417](https://github.com/hyperb1iss/sibyl/pull/417) through [437](https://github.com/hyperb1iss/sibyl/pull/437) added the official harness adapter, provider accounting, saved memory, receipts, CI execution, and release controls.                                                                                               |
| Hosted-runner memory fixes | Complete | PRs [442](https://github.com/hyperb1iss/sibyl/pull/442), [443](https://github.com/hyperb1iss/sibyl/pull/443), and [444](https://github.com/hyperb1iss/sibyl/pull/444) bounded the Surreal cache and streamed shared trajectory loading. They did not keep Enterprise Small on the 15 GiB runner.                                       |
| Rig-blocked receipt        | Complete | The dispatch-exhaustion receipt above, sealed from the observed r1 to r5 ledger.                                                                                                                                                                                                                                                       |
| Current main CI            | Passing  | CI run [33025662969](https://github.com/hyperb1iss/sibyl/actions/runs/33025662969) and Nightly Regression run [33244786444](https://github.com/hyperb1iss/sibyl/actions/runs/33244786444) passed on `012e7b55`.                                                                                                                        |

The supported development stack is proto 0.61.1, moon 2.5.3, Node 24.19.0, pnpm 11.23.0, Python
3.13.15, and uv 0.12.5.

## Official evaluation snapshot

The r5 Web Small metrics recorded on 2026-08-26 stand as observed data for that sealed
configuration.

| Metric                      |             Result |
| --------------------------- | -----------------: |
| Questions completed         |         240 of 240 |
| Overall accuracy            |             23.33% |
| Median memory-query latency |      54.92 seconds |
| Reader tokens               |          3,464,676 |
| Settled provider cost       |        $0.91057044 |
| Prompt contexts truncated   |                  0 |
| Official domain runtime     | 4 hours 54 minutes |

The Web artifacts (result, service diagnostics, and frozen memory including the SurrealDB snapshot)
are retained for 30 days from 2026-08-26 and remain immutable. Enterprise Small lost its runner at 3
hours 44 minutes and uploaded nothing.

None of these numbers is a 1.3 benchmark claim. The receipt requires both domains, a finite LAFS
gain, and a measured noise floor, and the campaign produced none of them.

## Release gates

| Gate                                   | State            | What remains                                                                                                                   |
| -------------------------------------- | ---------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| Behavioral contract                    | Complete         | No release work remains.                                                                                                       |
| Authorization and failure truth        | Complete         | No release work remains.                                                                                                       |
| Official harness and receipt integrity | Complete         | Preserve the reviewed harness pin and sealed inputs.                                                                           |
| Stable A/A noise floor                 | Stopped          | The rig-blocked receipt stops paid benchmark work for 1.3.                                                                     |
| Two-domain post-decontamination anchor | Stopped          | A blocked rig produces no score claim. The anchor moves to the deferred benchmark work.                                        |
| Machine versus naive decision          | Stopped          | Stopped by the rig-blocked receipt. The machine pipeline stays the default.                                                    |
| Render treatment decision              | Stopped          | Stopped by the rig-blocked receipt. The bundle stays off.                                                                      |
| Release runbook                        | Complete         | [SIBYL_1_3_RELEASE_RUNBOOK.md](SIBYL_1_3_RELEASE_RUNBOOK.md) records the commit, receipts, deferred work, and rollback points. |
| Version, tag, and publication          | Pending approval | Dry cut first. The bump from 1.2.2 to 1.3.0, the tag, and the publish run each need explicit approval.                         |

## Deferred benchmark work

The benchmark campaign resumes after 1.3 ships. The work below is tracked in Sibyl; none of it is a
release condition.

- Task `2a129447`: the hybrid fulltext lane fix. Commit `fc17af80` on branch
  `nova/sibyl-1-3-hybrid-fulltext-lane` splits `graph_entity_search._fulltext_search` into four
  per-field bounded top-k statements. The core suite passed 2,755 tests, and a Codex cross-model
  review returned PASS with an executed embedded-engine probe showing the old and new top-k rankings
  identical for limits 1, 2, 3, and 6. The branch is pushed and its pull request is pending. It is
  not part of the `012e7b55` candidate.
- Task `8a56f5c7`: reproduce the SurrealDB 3.2.3 residency growth from the r5 frozen snapshot, and
  measure the fulltext fix's live latency against it. In progress, no latency numbers yet. One
  preliminary observation: restoring the r5 web snapshot into a fresh v3.2.3 container with the same
  4 GiB block cache idles at about 6.6 GiB RSS before any query runs, so most of the runner's
  resident footprint is index residency rather than query-time growth.
- Task `defd7d5b`: native SurrealDB grows to 33 GB resident under sustained bench load and SELECTs
  degrade tenfold.
- Task `e2ffa177`: the operational ingest write path collapses at table depth under bulk upserts
  against fulltext and HNSW index maintenance.
- Task `0b639fc2`: move the bench stack off the laptop. Personal GitHub accounts cannot use larger
  hosted runners and the repository has zero self-hosted runners, so the 15 GiB ceiling is fixed
  until the stack moves.
- Task `f71e152e`: this receipt, status, and runbook work.

## Current decision

Proceed to the dry cut described in the runbook from `012e7b55`. Keep the r5 Web artifacts
immutable, publish no score, and spend no credits on another official run before the fulltext fix
and the residency reproduction close. Tag and publish only on explicit approval.
