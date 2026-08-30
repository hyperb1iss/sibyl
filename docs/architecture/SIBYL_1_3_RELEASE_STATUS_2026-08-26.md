# Sibyl 1.3 release status

Superseded by [SIBYL_1_3_RELEASE_STATUS_2026-08-29.md](SIBYL_1_3_RELEASE_STATUS_2026-08-29.md); the
body below is retained as written on 2026-08-26.

- Snapshot date: 2026-08-26
- Release state: hold
- Main commit: `7f31a330c0d9a25180a98a4332423b811d18c117`
- Latest released version: `1.2.2`
- Governing plan: [SIBYL_1_3_IMPLEMENTATION_PLAN.md](SIBYL_1_3_IMPLEMENTATION_PLAN.md)

## Status

The 1.3 product implementation is merged. The One Surface contract, authorization and failure
semantics, harmony refactors, dependency refresh, toolchain update, and release evaluation rig are
all on `main`. Normal CI passes at the current commit.

The release is not ready to cut. The first official A/A builder completed Web Small but lost the
Enterprise Small hosted runner. Web scored 23.33%, well below a useful product result. Enterprise
produced no domain artifact, so the workflow could not generate a combined receipt or a finite LAFS
gain.

The implementation plan allows the product release to proceed when a five-pass rig-blocked receipt
proves that evaluation cannot stabilize. We do not have that receipt. The release runbook is also
unfinished. Until one of those measurement paths closes, 1.3 remains on hold.

## What is complete

| Area                       | State                             | Evidence                                                                                                                                                                                                                                                                                                                               |
| -------------------------- | --------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| One Surface behavior       | Complete                          | [PR 406](https://github.com/hyperb1iss/sibyl/pull/406) merged the lifecycle, predicate, authorization, and failure contract.                                                                                                                                                                                                           |
| Harmony and legacy cleanup | Complete                          | [PR 412](https://github.com/hyperb1iss/sibyl/pull/412) removed legacy runtime paths. [PR 413](https://github.com/hyperb1iss/sibyl/pull/413) converged the major services on canonical owners.                                                                                                                                          |
| MCP SDK 2                  | Complete                          | [PR 410](https://github.com/hyperb1iss/sibyl/pull/410) migrated the server and clients. This supersedes the older plan text that placed the migration after 1.3.                                                                                                                                                                       |
| Dependencies and toolchain | Complete                          | PRs [411](https://github.com/hyperb1iss/sibyl/pull/411), [414](https://github.com/hyperb1iss/sibyl/pull/414), [419](https://github.com/hyperb1iss/sibyl/pull/419), [432](https://github.com/hyperb1iss/sibyl/pull/432), and [435](https://github.com/hyperb1iss/sibyl/pull/435) refreshed dependencies and pinned the supported stack. |
| CLI pending writes         | Complete                          | [PR 438](https://github.com/hyperb1iss/sibyl/pull/438) added automatic replay. [PR 441](https://github.com/hyperb1iss/sibyl/pull/441) bound replay to credential lineage. Users do not need to replay writes manually.                                                                                                                 |
| Sealed release evaluation  | Complete                          | PRs [417](https://github.com/hyperb1iss/sibyl/pull/417) through [437](https://github.com/hyperb1iss/sibyl/pull/437) added the official harness adapter, provider accounting, saved memory, receipts, CI execution, and release controls.                                                                                               |
| Hosted-runner memory fixes | Complete for known retained state | PRs [442](https://github.com/hyperb1iss/sibyl/pull/442), [443](https://github.com/hyperb1iss/sibyl/pull/443), and [444](https://github.com/hyperb1iss/sibyl/pull/444) bounded the Surreal cache and streamed shared trajectory loading without reducing benchmark coverage or concurrency.                                             |
| Current main CI            | Passing                           | [CI run 32998609152](https://github.com/hyperb1iss/sibyl/actions/runs/32998609152) passed dependency audit, build, static checks, package tests, E2E, Darwin authority, and live SurrealDB ingestion.                                                                                                                                  |

The supported development stack is proto 0.61.1, moon 2.5.3, Node 24.19.0, pnpm 11.23.0, Python
3.13.15, and uv 0.12.5. Node 24 remains the declared runtime through the `~24` engine constraint.
Node 26 is not part of the 1.3 release surface.

## Official evaluation snapshot

The current experiment is `sibyl-v1-3-aa-20260826-r5`. The controller completed successfully in
[run 32998699090](https://github.com/hyperb1iss/sibyl/actions/runs/32998699090). The first builder
ran the exact merged commit in
[run 32998783818](https://github.com/hyperb1iss/sibyl/actions/runs/32998783818).

### Web Small

Web Small completed the full official domain and uploaded its result, service diagnostics, and
frozen memory artifacts.

| Metric                      |             Result |
| --------------------------- | -----------------: |
| Questions completed         |         240 of 240 |
| Overall accuracy            |             23.33% |
| Non-abstention accuracy     |             26.79% |
| Abstention accuracy         |             15.28% |
| Median memory-query latency |      54.92 seconds |
| P95 memory-query latency    |      74.64 seconds |
| Reader tokens               |          3,464,676 |
| Settled provider cost       |        $0.91057044 |
| Prompt contexts truncated   |                  0 |
| Official domain runtime     | 4 hours 54 minutes |

The category breakdown points to two different defects:

| Category  | Correct | Answered wrong | Unknown |
| --------- | ------: | -------------: | ------: |
| Static    |  28.57% |         13.19% |  58.24% |
| Dynamic   |  15.28% |         18.06% |  66.67% |
| Procedure |  25.81% |         54.84% |  19.35% |
| Gotchas   |  20.00% |         66.67% |  13.33% |

Static and dynamic questions mostly fail as unknown, which points toward missing or inaccessible
evidence. Procedure and gotcha questions usually receive an answer, but the answer is wrong. Those
rows need evidence-composition and reader analysis. The absence of context truncation rules out the
configured prompt limit as a general explanation.

The successful final diagnostics reported zero SurrealDB restarts and no OOM kill. SurrealDB used
about 4.57 GiB RSS. The host still had 8.3 GiB available, although 2.7 GiB of its 3.0 GiB swap was
in use. Those values describe the end of the run, not its peak.

The Web receipt is complete for its domain, but it is not a citable 1.3 benchmark result. The
receipt requires both domains and a finite LAFS gain. Its leaderboard-metrics check therefore fails
by design while Enterprise is absent.

### Enterprise Small

Enterprise Small lost communication with its hosted runner after 3 hours 44 minutes. GitHub marked
the job failed while the official domain step was still active. The runner did not execute final
diagnostics or artifact upload, and GitHub has not retained a downloadable job log.

The evidence does not identify the process or benchmark phase at the resource peak. The failure is
consistent with transient host starvation, but that remains a hypothesis. The final Web snapshot
cannot establish Enterprise's peak.

### Combined result

The combined receipt job was skipped because Enterprise failed. Current release evidence therefore
has no complete two-domain anchor, no finite LAFS gain, and no measured A/A noise floor.

## Release gates

| Gate                                   | State            | What remains                                                                                  |
| -------------------------------------- | ---------------- | --------------------------------------------------------------------------------------------- |
| Behavioral contract                    | Complete         | No release work remains.                                                                      |
| Authorization and failure truth        | Complete         | No release work remains.                                                                      |
| Official harness and receipt integrity | Complete         | Preserve the reviewed harness pin and sealed inputs.                                          |
| Stable A/A noise floor                 | Blocked          | Finish the preregistered passes or produce the five-pass rig-blocked receipt.                 |
| Two-domain post-decontamination anchor | Blocked          | Enterprise must complete on the same sealed configuration, unless the rig closes as blocked.  |
| Machine versus naive decision          | Pending          | Adjudicate the race, record it as inconclusive, or stop through the rig-blocked path.         |
| Render treatment decision              | Pending          | Ship, kill, or mark the bundle inconclusive, unless the rig-blocked path stops it.            |
| Release runbook                        | Pending          | Record the exact commit, generated receipts, deferred work, and rollback points.              |
| Version, tag, and publication          | Pending approval | Bump from 1.2.2 only after the gates close. Tagging and publishing require explicit approval. |

## Next work

1. Analyze all 240 Web traces. Classify each miss as absent evidence, poor ranking, bad context
   assembly, reader misuse, or judge failure. Compare the selected evidence with the gold answer
   before changing retrieval.
2. Add Enterprise telemetry and automatic phase checkpoints that survive runner loss. Record host
   and process RSS, swap, SurrealDB cache use, benchmark phase, and completed question counts. Keep
   the official workload and concurrency intact.
3. Fix the mechanisms supported by the trace analysis. Use frozen-input replay and focused probes to
   reject weak treatments before another paid official run.
4. Resume the sealed A/A experiment only after the probes and telemetry pass. Do not replay writes
   or reconstruct missing results by hand.
5. Generate the combined anchor, noise-floor receipt, machine-versus-naive decision, and render
   decision. If the rig cannot stabilize after the preregistered attempts, generate the formal
   rig-blocked receipt instead.
6. Write the release runbook, run the exact-commit release gates and dry cut, then request approval
   for the version bump, tag, and publication.

## Current decision

Hold the 1.3 cut. Keep the completed Web artifacts immutable, do not publish the 23.33% score as a
release claim, and do not spend credits on another official run until the trace analysis and
Enterprise telemetry explain what should change.
