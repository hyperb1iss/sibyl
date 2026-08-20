# Sibyl v1.3 final implementation and release plan: One Surface

- Status: implementation complete; paid benchmark and release-cut evidence pending
- Created: 2026-08-19
- Working task: `8be323d1-0000-4ce2-9661-8ca504f17e17`
- Starting commit: `c3c786ab603a67e45689c765b2365f6d3cef8bc6`
- Plan baseline: `1e8e0b4b`
- Implemented code range: `1e8e0b4b..bc95c476`
- Release thesis: one memory contract across every public surface

This plan replaces the v1.3 scope in [`SIBYL_POST_1_0_ROADMAP.md`](SIBYL_POST_1_0_ROADMAP.md), the
provisional [`SIBYL_1_3_STRATEGY_SKELETON.md`](SIBYL_1_3_STRATEGY_SKELETON.md), and
[`SIBYL_1_3_RETHINK_2026-08-13.md`](SIBYL_1_3_RETHINK_2026-08-13.md). The research behind the
decision remains in
[`SIBYL_1_3_FINDINGS_AND_PLAN_2026-08-19.html`](SIBYL_1_3_FINDINGS_AND_PLAN_2026-08-19.html).

## Current state

The repository implementation is complete. Waves 0 and 4 are implemented and covered by focused
gates. Wave 1 now has a pinned official harness, fail-closed receipts, a tested decision rig, and
native SurrealDB monitoring. The machine race and render treatment are implemented as preregistered
workflows, but their paid runs have not been authorized or executed.

No architecture choice remains open for v1.3. The release cut still needs four pieces of evidence
from one frozen, clean commit:

1. an A/A receipt that establishes the measured noise floor or stops the paid path as rig-blocked;
2. a post-decontamination Small anchor for both domains when the rig passes;
3. machine-versus-naive and render-bundle decisions, or the applicable rig-blocked receipts;
4. the final clean-tree release gate and runbook receipt.

The paid runner is approval-bound. No LongMemEval score, leaderboard claim, or submission exists
until those receipts are generated. Product release remains allowed when the benchmark rig blocks,
but a blocked rig cannot produce a score claim.

## 1. Decision

Sibyl v1.3 is **One Surface**.

The release makes one behavioral promise: a memory has the same lifecycle, authorization,
relationship meaning, and failure semantics whether a caller uses REST, CLI, MCP, context
compilation, or the naive retrieval control. The internal engines may remain separate during this
release. Their public answers may not disagree.

The benchmark is a decision instrument, not the release identity. v1.3 will produce a trustworthy
post-decontamination anchor, compare the current machine pipeline with the naive-strong control, and
test one bounded render treatment. The release does not promise a leaderboard submission or a public
score claim.

Coalescence and TeamMemBench move to v1.4. Three agendas had accumulated in the v1.3 slot, and the
repository contains no coalescence engine to finish. Shipping the behavioral contract first gives
later coalescence work one trustworthy read surface, one authorization boundary, and one measurement
rig.

### 1.1 Locked decisions

1. **One behavioral contract, not one forced implementation.** A physical pipeline merger is not
   required for v1.3.
2. **No benchmark claim is required to release.** Benchmark work chooses product behavior and
   retires bad assumptions.
3. **Small is the only development tier.** Medium stays out until Sibyl clears 51.0% on Small and a
   measured Small-to-Medium slope justifies the expense.
4. **A submission requires positive official LAFS.** Recompute against the current official frontier
   immediately before any submission. The candidate must also be non-dominated by the displayed
   Small submissions.
5. **The machine pipeline remains the v1.3 default.** If naive-strong wins, it stays selectable
   through v1.3 and the machine deletion becomes a committed v1.4 task. Keeping two permanent
   pipelines is not an option.
6. **Contradiction is not retirement.** `CONTRADICTS` keeps both memories recallable and marks
   contested evidence. `SUPERSEDES` retires the target.
7. **Tags are browse metadata in v1.3.** Tool schemas and docs must stop implying that tags affect
   ranked retrieval. Indexing is later work.
8. **MCP SDK 2.0 lands after v1.3.** The release stays on the current compatible SDK range. The
   major upgrade gets its own host, auth, and collection gates.

## 2. Verified starting point

The findings below were rechecked against the starting commit, current GitHub state, local receipts,
and the official LongMemEval-V2 sources on 2026-08-19.

| Fact                                                         | Evidence                                                                                                                                                                              | Consequence                                                                  |
| ------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------- |
| The old benchmark gate targets zero LAFS.                    | The official Small reference frontier includes 51.0% at 0.2 seconds. The official scorer gives 42.8% at 10 seconds a gain of `0.0000`.                                                | Delete the old `42.8% at <=10s` gate. Use the official scorer as the gate.   |
| The Small board is not empty.                                | The deployed leaderboard lists three Small submissions.                                                                                                                               | Presence at the bottom of the board is not a release goal.                   |
| The latest stored scored anchor predates the ranker cleanup. | Local official receipts end before PR 386, and PR 395 later changed adapter slicing.                                                                                                  | The 30.38% and 31.26% numbers are historical context, not a v1.3 baseline.   |
| The harness pin is stale.                                    | Sibyl pins `be15ea6e...`; the reviewed official head was `2cc8c540...`. The newer harness passes a keyword-only `query_invocation_id`.                                                | Update the immutable pin and repair `set_query_context` before measuring.    |
| Lifecycle filtering is not universal.                        | `graph_metadata_recallable()` reaches context sections, context explore, and context neighbor expansion. The unified search and naive paths do not call it.                           | A corrected memory can still appear current through default search surfaces. |
| Predicate policy is duplicated.                              | `retrieval/hybrid.py` and `retrieval/search.py` carry different tables and different `SUPERSEDES` weights. `CONTRADICTS` falls through to an untyped default.                         | Define one predicate policy with explicit direction and lifecycle effects.   |
| REST and MCP disagree on organization write roles.           | REST entity writes allow owner, admin, and member. MCP resolves the JWT role but never applies the write-role set. API-key MCP context does not resolve the key owner's current role. | A viewer can reach MCP mutations that REST denies.                           |
| The local queue can suppress an ordinary retry.              | A failed local job is stored as `COMPLETE`; `_enqueue_unique()` refuses the same job id until result expiry. Entity job ids are content-derived.                                      | A retry can return the old failed job instead of creating work.              |
| Background work can fail behind a successful write.          | Bulk projection, extraction, and embedding enqueue failures are logged while the response remains successful.                                                                         | Success receipts must expose incomplete derived work and a recovery handle.  |
| Readiness ignores the active broker.                         | `check_readiness()` probes SurrealDB and schema bootstrap only.                                                                                                                       | A server that cannot accept required background work can report ready.       |
| Browser e2e cannot prove a page rendered.                    | The five smoke tests follow redirects and accept 3xx responses, including the login redirect.                                                                                         | Dependency and release changes can be green without rendering the app.       |
| Release files can miss the main CI path.                     | The CI classifier does not classify `charts/`, `VERSION`, `install.sh`, `Tiltfile`, or most workflow files as runtime or CI changes.                                                  | A release-critical edit can run no build, test, e2e, or Helm gate.           |

### 2.1 Foundations already present

The release builds on work already merged through PR 397:

- ranker vocabulary decontamination;
- a selectable naive-strong arm;
- typed predicates writable through CLI, REST, and MCP;
- the first supersession lifecycle gate;
- API-key scope enforcement on MCP;
- chart rejection of unsafe production JWT-secret configuration;
- auth, CLI exit-code, backup isolation, and dependency-scan repairs.

The plan does not reopen those changes. It closes the places where their contracts stop short of the
public boundary.

## 3. The One Surface contract

### 3.1 Public observations under test

One two-row supersession fixture must exercise all six observations:

1. context-pack sections;
2. context-pack fast evidence;
3. context-pack naive evidence;
4. REST search;
5. CLI context;
6. MCP search.

The fixture writes a current successor with a `SUPERSEDES` edge to a retired target. Every
observation must return the successor and omit the retired target as current evidence. Context
explore and explicit neighbor expansion receive their own regression assertions because they are
graph-browsing surfaces rather than ranked-search observations.

### 3.2 Lifecycle law

- A superseded, archived, deleted, or otherwise non-recallable row is absent from current ranked
  results.
- An incoming `SUPERSEDES` traversal may rescue the current successor when the seed is the retired
  target.
- An outgoing `SUPERSEDES` traversal may not reintroduce the retired target from the successor.
- Explicit history and audit operations may return retired rows when the caller asks for history.
- Lifecycle filtering happens after authorization and before final ranking or rendering. Filtering
  may not reveal the existence of an unauthorized row.

### 3.3 Predicate law

Create one canonical policy object used by hybrid retrieval and context graph expansion. The engines
use different score units, so the policy keeps a hybrid multiplier and an expansion path score
instead of pretending one number means the same thing in both places. Each entry also declares
direction, lifecycle effect, and receipt label.

The first implementation preserves the current score for every existing relationship. `CONTRADICTS`
becomes explicit at the fallback values it already receives today. Any later score change needs its
own measured treatment.

| Predicate            | Hybrid multiplier | Expansion path score | Direction                           | Lifecycle effect                     |
| -------------------- | ----------------: | -------------------: | ----------------------------------- | ------------------------------------ |
| `SUPERSEDES`         |              1.10 |                 0.95 | Incoming only for result expansion. | Hide the target from current recall. |
| `CONTRADICTS`        |              1.00 |                 0.64 | Both directions.                    | Keep both rows recallable.           |
| `REQUIRES`           |              1.15 |                 0.98 | Outgoing.                           | None.                                |
| `SUPPORTS`           |              1.00 |                 0.94 | Outgoing.                           | None.                                |
| `DECIDES`            |              1.00 |                 1.00 | Outgoing.                           | None.                                |
| Untyped `RELATED_TO` |              0.85 |                 0.64 | Existing behavior.                  | None.                                |

The canonical object also carries every non-declarable relationship currently present in either
engine. Existing engine-specific values and ordering stay unchanged when one engine has no special
entry for a relationship.

Every retrieval receipt records hop counts by predicate and direction. A typed arm with zero typed
hops is therefore distinguishable from an arm whose typed hops were present but lost during ranking.

### 3.4 Failure law

- A user-visible success receipt names every required background job as `queued`, `complete`,
  `degraded`, or `failed`.
- A failed job id is retryable without waiting for result expiry.
- A pending marker cannot outlive the work it points to without becoming an explicit failed state.
- A retrieval lane may not report `completed` after returning an empty fallback caused by an
  exception.
- The machine and naive benchmark arms use the same exception policy. A row is either valid for both
  arms or failed for both arms.

## 4. Benchmark protocol law

The benchmark protocol is part of the release because it decides which product path survives. The
score is not itself a release gate.

1. **Pin reviewed source.** Replace `be15ea6e...` with `2cc8c540...` or a later reviewed immutable
   commit. Record the official diff in the receipt.
2. **Block metadata leakage.** The adapter receives only the official query-invocation identifier.
   It may not receive raw question text, answer labels, or hidden grading metadata through query
   context.
3. **Freeze the candidate.** Every paired pass runs the same clean Sibyl commit, official harness
   commit, dataset checksums, reader, judge, and arm geometry.
4. **Run A/A first.** No A/B result is interpreted until the exact runner and stack produce a stable
   measured noise floor. Three percentage points is the initial target, not a release blocker.
5. **Measure three paired passes.** Report the per-pass deltas, mean delta, sign, latency, token and
   provider usage, and the measured A/A span.
6. **Prove arm activity.** Receipts must include non-zero arm-specific activity when a row could
   exercise that arm. A configured flag is not proof.
7. **Match geometry.** Run one control where machine and naive arms expose the same character total
   and item limits.
8. **Replay before paying.** Use frozen retrieval and reader inputs for free validation, causal
   screens, and formatting checks. Paid full runs come last.
9. **Keep failed rows visible.** Never convert a retrieval, context, provider, or scorer exception
   into an empty success.
10. **Make claims mechanically.** A public claim requires both domains, a clean commit, complete
    provider receipts, positive official LAFS, and a generated claim receipt.

### 4.1 Submission policy

v1.3 can release with no leaderboard submission. A candidate may be submitted only when the official
scorer reports positive LAFS against a freshly fetched frontier and the point is not dominated by a
displayed Small submission. Any submission needs a separate explicit decision after reviewing the
generated receipt. Medium is not a fallback board for a weak Small result.

## 5. Execution waves

The waves are dependency-ordered. Release-integrity work can run beside the measurement path once
its task contracts are fixed.

### Wave 0: enforce the contract

#### S0.1 Apply lifecycle law to every read engine

**Primary files**

- `packages/python/sibyl-core/src/sibyl_core/memory_pipeline/lifecycle.py`
- `packages/python/sibyl-core/src/sibyl_core/tools/search.py`
- `packages/python/sibyl-core/src/sibyl_core/retrieval/naive.py`
- `packages/python/sibyl-core/src/sibyl_core/retrieval/hybrid.py`
- `packages/python/sibyl-core/src/sibyl_core/retrieval/search.py`

**Work**

- Move current-recall eligibility behind one surface-neutral helper.
- Apply the helper to unified search and naive retrieval before results leave the core package.
- Prevent outgoing `SUPERSEDES` expansion from reviving the retired target.
- Preserve the incoming rescue path from target to successor.
- Add the six-observation fixture and graph-browsing regressions.

**Gate**

The retired row is absent from all six observations. The successor remains reachable. History access
still returns both rows when explicitly requested.

#### S0.2 Replace duplicated predicate policy

**Primary files**

- `packages/python/sibyl-core/src/sibyl_core/retrieval/hybrid.py`
- `packages/python/sibyl-core/src/sibyl_core/retrieval/search.py`
- `packages/python/sibyl-core/src/sibyl_core/models/relations.py`

**Work**

- Introduce one typed policy object and import it from both retrieval engines.
- Encode the direction and lifecycle table from section 3.3.
- Add explicit `CONTRADICTS` behavior instead of the untyped fallback.
- Add predicate and direction counters to retrieval receipts.
- Repair the exact-name rescue guard and its production-shaped fixture before using rescue-lane
  evidence in a benchmark claim.

**Gate**

Both engines consume the same policy object. Tests exercise every declared predicate and prove that
`CONTRADICTS` does not retire either row. A fixture corpus keeps hybrid and expansion ordering
unchanged. Any intentional ordering delta must be named and measured in the S1.3 anchor receipt.

#### S0.3 Make MCP write authority match REST

**Primary files**

- `apps/api/src/sibyl/server.py`
- `apps/api/src/sibyl/auth/mcp_auth.py`
- `apps/api/src/sibyl/auth/api_key_common.py`
- `apps/api/src/sibyl/persistence/surreal/auth_runtime/api_keys.py`
- `packages/python/sibyl-core/src/sibyl_core/auth/memory_policy.py`

**Work**

- Resolve the caller's current organization role for JWT and API-key MCP credentials.
- Deny MCP mutations to viewers before any idempotency reservation or write.
- Keep owner, admin, and member behavior aligned with REST.
- Apply REST's existing target-visibility rule to MCP: validate every `related_to` target against
  the caller's read scope before creating any raw capture, graph row, or edge. The rule includes
  untyped edges.
- Run MCP validation before its idempotency reservation so a corrected retry can reuse the key. REST
  deliberately reserves first to preserve replay after later access revocation. The reservation
  order is surface-specific.
- Keep the core writer's suppression downgrade as defense in depth for direct internal callers.
  Public REST and MCP requests still refuse a hidden or absent target before the writer runs.
- Return the same denial class without turning target ids into an existence oracle.

**Gate**

Owner, admin, and member can perform an allowed write. Viewer cannot write by JWT or by a
viewer-owned API key. A hidden `related_to` target is refused before `server.py` reserves MCP
idempotency, without creating a raw capture, graph row, edge, or idempotency receipt.

#### S0.4 Make background-work outcomes true and retryable

**Primary files**

- `apps/api/src/sibyl/coordination/_local/broker.py`
- `apps/api/src/sibyl/coordination/_local/pending.py`
- `apps/api/src/sibyl/jobs/pending.py`
- `apps/api/src/sibyl/api/routes/entities.py`
- `packages/python/sibyl-core/src/sibyl_core/tools/add.py`

**Work**

- Represent failed local jobs as failed, not completed.
- Permit a failed content-derived job id to enqueue a new attempt immediately.
- Make pending state resolve to complete or failed before its marker expires.
- Return derived-work state and recovery identifiers when projection, extraction, or embedding
  enqueue fails.
- Keep synchronous fallback explicit in the response instead of pretending the operation is still
  pending.

**Gate**

A forced first-attempt failure followed by the identical user request executes a second job and
materializes searchable data. Each response tells the truth about the parent write and every derived
job.

#### S0.5 Make canonical quality metadata authoritative

**Primary files**

- `packages/python/sibyl-core/src/sibyl_core/memory_pipeline/quality.py`
- `packages/python/sibyl-core/tests/test_memory_pipeline_quality.py`

**Work**

- Read canonical `importance` and `confidence` before every legacy alias.
- Fall back to legacy fields only when the canonical field is absent.
- Continue mirroring canonical values into legacy storage fields while older readers still require
  them.
- Prove that updating the canonical value cannot be reversed by a stale legacy shadow on the next
  read or write.

**Gate**

A record containing conflicting canonical and legacy values keeps the canonical value through
normalization, storage expansion, retrieval, and retention scoring.

### Wave 1: repair the rig and cut the anchor

#### S1.1 Update the official harness contract

**Primary files**

- `.github/workflows/longmemeval-v2.yml`
- `benchmarks/longmemeval_v2_validation_slice.py`
- `benchmarks/longmemeval_v2_memory/sibyl_memory.py`
- `tools/tests/test_longmemeval_v2_probe.py`

**Work**

- Review and pin the official harness commit from section 4.
- Adapt `set_query_context` to the keyword-only invocation identifier.
- Add a contract test against the pinned official base class.
- Record the official source commit and diff boundary in every plan and receipt.

**Gate**

The adapter imports and runs against a clean checkout of the pinned harness. The query-context test
proves that question text and answer metadata never cross the adapter boundary.

#### S1.2 Repair validation and receipt integrity

**Primary files**

- `benchmarks/longmemeval_v2_validation_slice.py`
- `benchmarks/longmemeval_v2_live_retrieval.py`
- `benchmarks/longmemeval_v2_memory/sibyl_memory.py`
- `tools/bench/`

**Work**

- Make validation-screen defaults match the adapter's source-evidence setting.
- Make typed-evidence validation aware of the selected retrieval mode.
- Fail the vector lane honestly when HNSW retrieval fails.
- Apply one exception policy to `compile_context` and naive retrieval.
- Promote the jitter-floor and pack-comparison tools into tracked code with tests and moon tasks.
- Add native-process support to the Surreal runtime monitor.

**Gate**

Fault injection produces failed rows and non-zero commands. Healthy runs carry lane activity,
context status, stack identity, provider usage, and clean-tree provenance on every row.

#### S1.3 Publish the first trustworthy anchor

Run three A/A passes on a clean post-Wave-0 commit. Three percentage points is the target span. If
the span is larger, run two more passes and publish the observed span rather than hiding it. Define
the decision noise floor `N` as the larger of `3pp` and that stable observed span.

If the five-pass span does not stabilize, or any compared receipt differs outside the allowed run
identifiers, stop the paid benchmark path and publish a rig-blocked receipt. Benchmark blockage does
not block the product release.

Once the rig is usable, run the current machine arm across both Small domains and publish one
generated anchor receipt. The receipt includes accuracy, evidence exposure, latency, tokens,
provider usage, and `N`.

The anchor replaces 30.38% and 31.26% for planning. Historical receipts remain available but may not
be used as the denominator for a v1.3 claim.

### Wave 2: decide whether the machine earns its latency

Define delta as `machine accuracy - naive accuracy`. Use `N` from S1.3 as the minimum effect size
throughout Waves 2 and 3.

#### S2.1 Pre-register the race

Freeze these values before the first paired pass:

- exact machine and naive arm configurations;
- retrieval, reader, and judge models;
- dataset and official harness checksums;
- item and character geometry;
- three paired pass seeds or run identifiers;
- the A/A span from S1.3;
- the decision rule below.

The machine treatment is the complete machine path: hybrid evidence, context sections, and client
assembly. A loss does not prove that Reciprocal Rank Fusion alone failed.

#### S2.2 Run unmatched and matched geometry

Run three paired passes at the shipping geometry, followed by one matched- character control. Verify
arm activity before reading the score.

#### S2.3 Apply the decision rule

- The machine earns retention on accuracy when mean delta is at least `+N` and every pass has a
  positive sign.
- The machine becomes the v1.4 deletion candidate when mean delta is below `+N`, naive latency is at
  most 60% of machine latency, and both observations repeat across all three passes.
- Every other result is inconclusive. Keep the machine default, preserve both receipts, and name the
  failed premise before another paid run.

No pipeline is deleted inside v1.3. A naive result that meets its rule ships as a selectable
non-default and creates a named v1.4 deletion task.

### Wave 3: test one bounded render treatment

Reader conversion is tested only after the race has selected the retrieval substrate. The treatment
changes what the reader sees, not what the scorer sees.

#### S3.1 Screen each lever on frozen inputs

Use replay to test these changes separately before bundling them:

1. raise the benchmark adapter's total character budget enough for selected items to reach
   full-state rendering;
2. deduplicate operational notes by note kind and source id;
3. keep a bounded note lane additive instead of evicting raw evidence;
4. label and group evidence lanes in plain English;
5. render one action spine per trajectory during assembly;
6. render observed absence only when the underlying inventory is complete;
7. give the digest explicit roles and a bounded budget.

The character change is benchmark-adapter scope until a product benchmark justifies a production
default change. Every treatment stays inside one hard total-character ceiling.

#### S3.2 Run one paid bundle

Combine only replay survivors. Preserve a receipt bit and contribution count for every included
lever. Run the bundled treatment across three paired reader passes on the selected retrieval
substrate.

#### S3.3 Apply the kill gate

Use the evidence-exposure value from S1.3, not the historical 0.652 estimate. Retire the
exposure-dependent grouping, action-spine, and digest hypotheses if the character increase gains
less than `+5pp` exposure across both domains.

A render treatment ships only when its paired accuracy gain exceeds `N`, every pass has a positive
sign, its mean latency regression is at most two seconds, and its reader-token regression is at most
25%. Otherwise the bundle is killed or recorded as inconclusive.

### Wave 4: make the release surface provable

Wave 4 can proceed beside Waves 1 through 3.

#### R4.1 Make readiness cover required coordination

- Probe the active broker when the configured backend requires one.
- Report backend identity and broker latency in readiness.
- Fail readiness when required background work cannot be accepted.
- Keep liveness limited to process health.
- Resolve `coordination_backend=auto` from configured runtime intent instead of silently choosing
  local when Redis settings are present.

**Gate:** a dead configured broker makes `/ready` fail while `/health` remains live. Local mode
stays ready without Redis.

#### R4.2 Replace redirect-tolerant browser smoke tests

- Authenticate the browser through a production-shaped fixture.
- Assert a rendered dashboard, graph, search result, and task interaction.
- Treat unexpected redirects as failures.
- Keep an explicit unauthenticated redirect test as a separate contract.
- Make `e2e:test-browser` depend on `e2e:playwright-install`.
- Fail the browser task when the frontend is unavailable instead of skipping.
- Make the CI e2e job invoke the moon browser task explicitly.

**Gate:** each browser test has a controlled application failure that makes the CI job fail. The CI
job installs Chromium, runs `moon run e2e:test-browser`, and renders pages rather than accepting or
skipping the login redirect.

#### R4.3 Close the CI release-path hole

- Classify `charts/`, `VERSION`, `install.sh`, `Tiltfile`, release workflows, publish workflows, and
  shared GitHub actions.
- Fail closed on an unmatched path and print the path.
- Route release-surface changes through static, release-workflow, runtime, image, e2e, and Helm
  gates as appropriate.
- Run the existing Helm subprocess tests with Helm installed.
- Add a small `helm template` matrix for defaults and production values.

**Gate:** a charts-only change, a VERSION-only change, and a publish-workflow change each trigger a
non-skipped relevant job. Default local configuration does not render a Redis worker. Redis
configuration does.

#### R4.4 Repair confirmed upgrade-path drift

- Replace the removed private manager call tracked by issue 394 with a public runtime capability in
  `packages/python/sibyl-core/src/sibyl_core/tools/admin.py`.
- Audit the sibling `bulk_create_direct` capability checks in
  `packages/python/sibyl-core/src/sibyl_core/projection/memory.py`, the admin restore path, and
  `apps/api/src/sibyl/cli/generate.py`. The generate command currently calls the absent method
  without a guard.
- Add live SurrealDB 3.2.3 coverage for attribute merge, explicit sentinel clearing, snapshot
  folding, and same-uuid rewrite.
- Express intentional projection-field removal explicitly rather than relying on omission after
  merge semantics changed.

**Gate:** the admin backfill, projection rewrite, and restore paths pass against the supported live
SurrealDB version without private manager methods.

#### R4.5 Make the remaining public claims exact

- Describe tags as browse-only metadata in MCP, CLI, REST, and user docs.
- Wire the chart's public URL into backend and frontend runtime settings so generated reset,
  callback, and application links cannot default to localhost.
- Give release version parsing one prerelease grammar across version sync, package metadata, tags,
  and Homebrew. Reject unsupported labels before any release mutation.
- Generate benchmark claims only through the claim gate.
- Write the release runbook as prose outside workflow YAML.
- Recompute the live PR and issue preflight at cut time. Do not copy the transient Dependabot queue
  into this plan.

**Gate:** a stock chart with a configured public URL renders that URL into backend and frontend
settings. Every accepted prerelease version passes the same parser and normalization tests before
release mutation begins.

## 6. Release exit criteria

Sibyl v1.3 may cut when every required item below has a receipt.

### Behavioral contract

- [x] All six public observations enforce the lifecycle law.
- [x] Hybrid and context expansion consume one predicate policy.
- [x] Predicate receipts prove direction and activity.
- [x] `CONTRADICTS` keeps both memories visible and marked contested.
- [x] Tags are described as browse-only everywhere they are exposed.

### Authorization and failure truth

- [x] Viewer writes fail through REST and MCP for JWT and API-key credentials.
- [x] Hidden relationship targets cannot be linked through MCP.
- [x] Failed local jobs can be retried immediately with the same user request.
- [x] Successful writes expose every derived job outcome and recovery handle.
- [x] Required broker failure makes readiness fail.
- [x] Canonical quality fields cannot be overwritten by stale legacy aliases.

### Measurement

- [x] The official harness is pinned to a reviewed immutable commit.
- [x] Query context is identifier-only and covered by a contract test.
- [x] Faulted vector and context lanes cannot report success.
- [ ] A/A produces a stable `N`, or a five-pass rig-blocked receipt stops paid benchmark work.
- [ ] A usable rig produces a clean post-decontamination Small anchor for both domains. A blocked
      rig produces no score claim.
- [ ] The machine versus naive race is adjudicated, inconclusive, or stopped by the rig-blocked
      receipt.
- [ ] The render bundle is shipped, killed, inconclusive, or stopped by the rig-blocked receipt.
- [x] No release document depends on a leaderboard submission.

### Release integrity

- [x] Authenticated browser e2e proves real page rendering.
- [x] Release-critical paths trigger relevant CI and Helm work.
- [x] A stock chart with a configured public URL renders that URL into every runtime consumer that
      generates external links.
- [x] One prerelease grammar governs release validation and version sync.
- [x] The supported live SurrealDB version covers the rewrite semantics used by admin, projection,
      and restore paths.
- [x] `moon run :check` passes from a clean tree.
- [x] Targeted lifecycle, MCP authorization, queue retry, readiness, browser, benchmark-contract,
      release-workflow, and Helm gates pass uncached.
- [ ] The release runbook records the exact commit, generated receipts, deferred work, and rollback
      points.

## 7. Explicitly deferred to v1.4 or later

- live coalescence and its reversible merge model;
- TeamMemBench construction or publication;
- the sub-1K corpus rebuild;
- a permanent machine-pipeline deletion;
- tag indexing and ranked tag boosts;
- MCP SDK 2.0;
- reader-prompt work beyond the bounded render treatment;
- a physical merger of REST, MCP, CLI, context, and naive internals;
- unrelated route-package, result-normalizer, and archive-table cleanup.

The deferral does not erase the roadmap. v1.4 starts with one trustworthy surface and can evaluate
coalescence without first debugging contradictory read paths or invalid benchmark anchors.

## 8. Verification commands

Use the narrowest moon targets first, then run the complete release gate from a clean tree.

```bash
moon run --force core:check api:check cli:check
moon run --force e2e:lint e2e:test-browser
moon run --force root:bench-gate-test root:inventory-lint
moon run --force root:release-workflow-test root:helm-test
moon run --force root:doc-claim-gate-test
moon run --force :check
```

The benchmark receipt must also record the exact command generated by its plan artifact. A pasted
shell command is not the source of truth for a scored run.

## 9. Implementation receipts

The code range contains 40 commits after the frozen plan baseline: 36 implementation and test
commits, plus four architecture receipt and format commits. The main verification receipts are:

| Surface               | Receipt                                                                                                                                                                                                            |
| --------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| One Surface lifecycle | One live project fixture passed all six observations against SurrealDB 3.2.3. The installed `sibyl context` observation also passed with a fresh automation token and an expired stored login for the same server. |
| Lifecycle planner     | A live SurrealDB 3.2.3 `EXPLAIN FULL` receipt selected a target-first union index scan for the forced `idx_relates_target_created` path. Candidate-specific keyset scans replace unbounded offset paging.          |
| Schema migration      | Graph schema migration 20 repairs absent, string, and malformed edge timestamps before restoring the required datetime contract. Live and embedded upgrade tests cover the migration and its indexed query.        |
| Core contract         | `core:check` passed with 2,698 tests, 14 expected skips, and one documented xfail.                                                                                                                                 |
| API contract          | `api:check` passed, including current-role MCP authorization, hidden-target ordering, retry truth, derived-work receipts, readiness, and scoped API-key persistence.                                               |
| CLI contract          | `cli:check` passed with 564 tests. Environment and explicit bearer tokens no longer consult unrelated stored-login expiry metadata.                                                                                |
| Benchmark rig         | `root:bench-gate-test` passed all 474 tests. Closed receipts bind A/A, preregistration, decision passes, and the anchor to one reviewed control contract. Decision and anchor seeds cannot reuse A/A seeds.        |
| Release path          | The focused release suites passed 42 release and CI tests, 15 Helm tests, five authenticated browser tests, six runner tests, and four live SurrealDB 3.2.3 contracts. The docs build rendered 107 pages.          |
| Full release gate     | The uncached `moon run --force :check` gate passed all 56 moon tasks from a clean tree after the final receipt update.                                                                                             |

The live One Surface test used the deterministic local embedding provider. It created and removed
its own project, decisions, relationship, and temporary MCP API key.

No paid LongMemEval run or leaderboard submission was made. The approval-bound measurement path
remains stopped before A/A because no provider key or spending approval was supplied.

## 10. Primary sources and receipts

- [Official LongMemEval-V2 leaderboard](https://xiaowu0162.github.io/longmemeval-v2/)
- [Deployed LongMemEval-V2 leaderboard data](https://xiaowu0162.github.io/longmemeval-v2/static/js/index.js)
- [Official LongMemEval-V2 repository](https://github.com/xiaowu0162/LongMemEval-V2)
- [Official leaderboard tooling](https://github.com/xiaowu0162/LongMemEval-V2/tree/main/leaderboard)
- [Sibyl pull request 386](https://github.com/hyperb1iss/sibyl/pull/386)
- [Sibyl pull request 390](https://github.com/hyperb1iss/sibyl/pull/390)
- [Sibyl pull request 391](https://github.com/hyperb1iss/sibyl/pull/391)
- [Sibyl pull request 395](https://github.com/hyperb1iss/sibyl/pull/395)
- [Sibyl pull request 396](https://github.com/hyperb1iss/sibyl/pull/396)
- [Sibyl issue 394](https://github.com/hyperb1iss/sibyl/issues/394)
- Local official receipts under `.moon/cache/evals/`
- The v1.3 research input documents linked at the top of this plan

## 11. Independent review history

| Round | Reviewer                                  | Verdict | Result                                                                                                                                                                                                                             |
| ----- | ----------------------------------------- | ------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1     | Claude, independent plan review           | Fail    | Found nine material issues: a reversed deletion rule, two weak measurement gates, incomplete predicate policy, MCP target-order ambiguity, browser CI gaps, upgrade-path omissions, and three dropped findings.                    |
| 2     | Claude, focused plan re-review            | Pass    | All nine material findings closed. The final edit corrected one REST reservation-order attribution and aligned render sign consistency with the race gate.                                                                         |
| 3     | Nash, independent implementation review   | Fail    | Reproduced a fail-open lifecycle lookup, benchmark drift across passes, missing receipt lineage, and a live fixture that named a hidden compatibility command as public CLI behavior.                                              |
| 4     | Nash, focused implementation re-review    | Fail    | Confirmed the original code failures were fixed, then rejected the frozen document because its commit range, test counts, and full-gate receipt predated the repairs.                                                              |
| 5     | Claude, cross-model implementation review | Fail    | Found remaining A/A receipt-closure and negative-test gaps. A separate objection about preregistration timing did not apply because the locked protocol runs A/A before preregistration.                                           |
| 6     | Claude, focused implementation re-review  | Pass    | Confirmed exact control binding, three fresh decision seeds, all three A/A outcomes, closed receipt schemas, and anchor lineage. The final negative tests cover render configuration drift, geometry drift, and anchor seed reuse. |
| 7     | Nash, exact-head implementation review    | Fail    | Reproduced a valid automation token being blocked by expired stored-login metadata during the live `sibyl context` observation. The other lifecycle, planner, migration, and benchmark probes passed.                              |
| 8     | Nash, final exact-head re-review          | Pass    | Confirmed the automation-token repair, focused CLI regression, public six-observation live fixture on fresh SurrealDB 3.2.3, plan range and count accuracy, and a clean frozen tree.                                               |
