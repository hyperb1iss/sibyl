# Sibyl v1.1 Implementation Plan

- Status: concrete execution contract for v1.1 release closure
- Source roadmap: [`SIBYL_POST_1_0_ROADMAP.md`](SIBYL_POST_1_0_ROADMAP.md)
- Target release: v1.1 "Prove It"
- Current branch: `docs/post-1.0-roadmap`
- Last updated: 2026-07-04

This file is the v1.1 release plan. The release does not ship because a feature exists; it ships
when every public claim has a receipt, every gate has a budget, and team-scoped memory is proven not
to leak.

## Release Thesis

v1.1 proves Sibyl before v1.2 starts live coalescence. It closes benchmark honesty gaps, moves evals
to PR-gating cadence, closes the usage feedback loop that forgetting already expected, enables the
team-memory substrate, and adds portable OKF export as a reviewable memory changelog.

The scope stays narrow:

- Evidence harnesses and receipt gates.
- Usage-aware forgetting, with exposure and citation separated.
- Team scope substrate, not live cross-contributor coalescence.
- OKF export and memory changelog, not product re-import.
- Public claims frozen against committed receipts.

## Current State

### Done Locally

- W0A task-update schema drift fix: `166b9a0d`.
- W1 LongMemEval-S QA lane, scheduled/manual QA workflow, gate contract, and docs truth-up:
  `3968ee93`, `335801b8`, `7ae10b02`.
- W2 eval ledger, history baselines, PR gates, scheduled live-eval cadence, and local-embedding
  variant: `6c2b8263`, `d01b23fc`, `4764b17b`, `07c3951e`, `f8200693`, `831dd0aa`, `6072d30d`,
  `09196343`.
- W3 cost, latency, token, and embedding-call accounting: `77e9069c`.
- W4 write-path integrity gate, self-feeding guard, and low-signal no-op extraction: `960e045e`.
- W5 LongMemEval-V2 local receipt/gate/workflow scaffold: `11e681c5`.
- W6A usage-event schema/service foundation: `36094084`.
- W6B exposure stamping on read surfaces: `e59e9be1`.
- W6C citation surfaces and usage-loop fixture gate: `b9e3ade8`.
- W6D usage-ordered consolidation input: `6bf8881f`.
- W7A usage-aware temporal ranking, priority decay, and forgetting gate: `4bf80afd`.
- W7C exposure-vs-citation survival semantics: `2095b616`.
- W8 team-memory foundation, including team management, same-org SHARE promotion, trust-gate
  coverage, and review hardening: `c6d0584e`, `e09e5d56`, `17073768`, `24853209`, `dcb8d340`.
- W9 OKF export, graph-payload reconstruction checks, OKF archive export, and memory changelog gate:
  `5c105164`, `feb2b2b8`, `e46b41cf`, `88a68e46`, `98d9043c`.
- W10 public claim gate, receipt manifest contract, and docs truth-up: `f74f23f4`.
- W6E/W7B dogfood receipt contracts and live read-only collectors: `d4ba6d60`, `04535d4b`.
- W0B docker-only dogfood image publish workflow and deployment image receipt: `35704309`.
- W0C version evidence preflight: API health now prefers deployed `SIBYL_VERSION` so a docker-only
  RC can report the running image tag without mutating package pins: `13e3f17b`.
- W0C post-deploy evidence collector: `f370187a`.
  `tools/trust/dogfood_receipts.py collect-deployment` combines the W0B image receipt,
  `/api/health`, and Docker inspect output into deployment evidence consumed by W6E/W7B.

### Live State

Read-only Hetzner check on `eternia` shows the live stack is healthy but stale:

- `/opt/sibyl/.env`: `SIBYL_VERSION=1.0.0-rc.8`
- API image: `ghcr.io/hyperb1iss/sibyl-api:1.0.0-rc.8`
- Web image: `ghcr.io/hyperb1iss/sibyl-web:1.0.0-rc.8`
- SurrealDB image: `surrealdb/surrealdb:v3.1.0`
- Containers: backend, frontend, and SurrealDB are up and healthy.

The Ansible role in this repo currently defaults to `sibyl_version: "1.0.2"`, but `v1.0.2` predates
the W6/W7 usage-loop and forgetting commits. Treat `1.0.2` as the stable 1.0 floor, not as a valid
v1.1 dogfood target. W0B must publish v1.1 release-candidate images first, then W0C can deploy that
RC to `eternia`.

### Remaining Release Blockers

1. W0B: trigger the docker-only dogfood image workflow for a v1.1 release-candidate commit that
   contains W6/W7/W8/W9/W10, then download the deployment image receipt.
2. W0C: refresh `eternia` from `1.0.0-rc.8` to that v1.1 RC image.
3. W1R: run and pin the paid model-backed LongMemEval-S QA receipt.
4. W5R: run and pin the paid official LongMemEval-V2 web plus enterprise receipt.
5. W6E: run the committed live usage-loop collector against the v1.1 dogfood deployment and promote
   its receipt from planned to blocking.
6. W7B: run the committed dogfood forgetting collector after W0C and real usage events, then promote
   its receipt from planned to blocking.
7. Final release freeze: rerun gates, refresh docs only where new receipts change claims, and cut
   release notes from committed evidence.

## Release Exit Criteria

v1.1 is releasable only when all of these are true:

- `qa-accuracy-gate` has a pinned LongMemEval-S QA-accuracy score and blocks drops greater than 1.0
  percentage point from that baseline.
- `eval-regression-gate` blocks PRs on strict recall@5 worse than baseline minus 0.5 percentage
  points, context-pack p95 above 1000 ms, or any leak.
- `cost-latency-gate` records per-query cost against a full-context baseline, embedding calls, token
  estimates, p50, and p95 for every citable eval artifact produced after W3 (pre-W3 pinned artifacts
  carry no accounting block and are not retrofitted).
- `write-path-integrity-gate` proves propagated hallucinations = 0, self-referential writes = 0, and
  low-signal extraction writes = 0 on seeded fixtures.
- `usage-loop-gate` proves exposure and citation flow through code surfaces, stamps 100% of
  applicable context/search builds into raw captures and graph entities, orders consolidation inputs
  by usage, and demonstrates cited-vs-uncited decay divergence on fixtures.
- W6E live dogfood receipt is enforced by a manifest contract and proves exposure and citation
  events flow through the deployed stack after W0C.
- `forgetting-gate` applies temporal decay consistently across search and context/recall, reduces
  stale uncited fixture bytes by at least 20%, keeps protected cited false-archives at 0, keeps
  strict recall@5 drop within 0.5 percentage points, and keeps W4 integrity failures at 0.
- W7B live dogfood receipt is enforced by a manifest contract and proves observed cited-vs-uncited
  decay divergence on the dogfood graph after W0C, with protected cited false-archives at 0.
- `longmemeval-v2-gate` has a scored official receipt for web and enterprise tiers and blocks LAFS
  Gain regressions beyond the committed tolerance.
- `team-scope-trust-gate` proves enabled team scope, team management, and promote/SHARE preserve
  isolation. Unauthorized private, delegated, and project memories do not surface in team packs.
  Promotion attribution and preview coverage are 100%.
- `okf-export-gate` exports a valid OKF bundle, proves byte-stable re-export, verifies graph-payload
  reconstruction in test tooling, and supports the scheduled/manual memory changelog.
- `doc-claim-gate` reconciles public docs against committed receipts and keeps retrieval recall, QA
  accuracy, LAFS Gain, cost/latency, local-embedding, and self-reported citation usage labeled as
  distinct evidence axes.

## Concrete Execution Waves

### Wave 1: Publish v1.1 Dogfood Images

**Status:** tooling done locally; approval-bound for GHCR publishing.

**Owner:** W0B release-candidate image prerequisite.

**Files:** `.github/workflows/publish-dogfood-images.yml`.

**Implementation:**

- Pick the dogfood image tag `1.1.0-rc.1`, or a later v1.1 RC if this plan is rerun after more
  release commits.
- Build from a commit that contains at least:
  - `36094084` W6A usage event schema foundation;
  - `e59e9be1` W6B exposure stamping;
  - `b9e3ade8` W6C citation surfaces;
  - `6bf8881f` W6D usage-ordered consolidation input;
  - `4bf80afd` W7A usage-aware forgetting;
  - `2095b616` W7C exposure-vs-citation semantics;
  - `dcb8d340` W8 team-share authorization hardening;
  - `98d9043c` W9 cached OKF workflow exports;
  - `f74f23f4` W10 local claim gate.
- Use the docker-only RC publish path so unfinished v1.1 does not publish PyPI, Homebrew, AUR,
  GitHub releases, tags, or `latest` image tags just to dogfood live images. The committed workflow
  runs the RC gate and publishes only:
  - `ghcr.io/hyperb1iss/sibyl-api:1.1.0-rc.1`;
  - `ghcr.io/hyperb1iss/sibyl-web:1.1.0-rc.1`.
- Record immutable image digests and the source revision for both images. The committed workflow
  writes OCI revision/version labels plus a nested `sibyl-dogfood-deployment-image-receipt-v1`
  receipt that W6E/W7B can consume as deployment evidence.
- Do not push a tag, create a GitHub release, or publish package-manager artifacts without explicit
  approval.

**Verify:**

```bash
git merge-base --is-ancestor 36094084 <approved-source-sha>
git merge-base --is-ancestor e59e9be1 <approved-source-sha>
git merge-base --is-ancestor b9e3ade8 <approved-source-sha>
git merge-base --is-ancestor 6bf8881f <approved-source-sha>
git merge-base --is-ancestor 4bf80afd <approved-source-sha>
git merge-base --is-ancestor 2095b616 <approved-source-sha>
git merge-base --is-ancestor dcb8d340 <approved-source-sha>
git merge-base --is-ancestor 98d9043c <approved-source-sha>
git merge-base --is-ancestor f74f23f4 <approved-source-sha>
gh workflow run publish-dogfood-images.yml \
  -f image_tag=1.1.0-rc.1 \
  -f ref=<approved-source-sha> \
  -f dry_run=false
docker buildx imagetools inspect ghcr.io/hyperb1iss/sibyl-api:1.1.0-rc.1
docker buildx imagetools inspect ghcr.io/hyperb1iss/sibyl-web:1.1.0-rc.1
```

**Exit:** both GHCR images exist at the RC tag, both are built from a commit that contains
W6/W7/W8/W9/W10, and the immutable image digest plus source revision are recorded for W6E/W7B
receipts.

### Wave 2: Refresh Dogfood Deployment

**Status:** approval-bound.

**Task:** `5a06d458-ccec-4fb4-95ef-634c9674b0de`

**State touched:** external homelab Ansible repo, `eternia:/opt/sibyl/.env`, and the `sibyl.service`
managed Docker Compose stack.

**Implementation:**

- Get explicit approval because this reruns the role and restarts `sibyl`.
- In the external homelab Ansible repository, set or override `sibyl_version: "1.1.0-rc.1"` or the
  later W0B dogfood image tag.
- The deployed API health endpoint now reads `SIBYL_VERSION` before the repository `VERSION` file,
  so `/api/health` should report the RC tag after the role updates `/opt/sibyl/.env`.
- Run the homelab playbook for `eternia` only.
- Do not accept any live dogfood receipt until API and web images both show the v1.1 RC tag and the
  source commit contains W6/W7.

**Verify:**

```bash
ssh eternia 'sudo grep "^SIBYL_VERSION=" /opt/sibyl/.env'
ssh eternia 'sudo docker compose --env-file /opt/sibyl/.env -f /opt/sibyl/docker-compose.yml images'
ssh eternia 'sudo docker compose --env-file /opt/sibyl/.env -f /opt/sibyl/docker-compose.yml ps'
ssh eternia 'sudo docker inspect sibyl-backend sibyl-frontend --format "{{.Name}} {{json .RepoDigests}} {{index .Config.Labels \"org.opencontainers.image.revision\"}}"'
curl -sf https://sibyl.hyperbliss.tech/api/health
python tools/trust/dogfood_receipts.py collect-deployment \
  --image-receipt <w0b-deployment-image-receipt.json> \
  --ssh-host eternia \
  --output benchmarks/results/ai-memory/deployment-dogfood-evidence.json
sibyl debug status
sibyl logs tail -l error -n 100
```

**Exit:** API and web images are the v1.1 RC tag, running container digests match the W0B receipt,
`/api/health` reports the RC tag, queue depth is sane, and recent logs show no deployment errors.

### Wave 3: Pin LongMemEval-S QA Accuracy

**Status:** approval-bound because it spends model budget.

**Tasks:** W1R, `525efcff-b715-469e-90ea-218647f5d201`.

**Files:** `.github/workflows/eval.yml`,
`benchmarks/results/ai-memory/pinned-longmemeval-s-qa.json`,
`benchmarks/results/ai-memory/manifest.json`, docs that mention QA accuracy.

**Implementation:**

- Confirm `OPENAI_API_KEY` is present in GitHub Actions secrets.
- Trigger the full LongMemEval-S workflow with QA enabled:

```bash
gh workflow run eval.yml \
  -f run_longmemeval_full=true \
  -f run_longmemeval_qa=true \
  -f longmemeval_concurrency=1 \
  -f longmemeval_corpus_text_policy=user-and-assistant-turns-v1 \
  -f longmemeval_native_fusion_backend=python_rrf
```

- Download `longmemeval-live-full-<sha>` and promote the full JSON report to
  `benchmarks/results/ai-memory/pinned-longmemeval-s-qa.json`.
- Flip the manifest `qa-accuracy-gate` from planned/non-blocking to blocking once the pinned
  artifact exists.
- Update public docs only with the exact pinned QA accuracy, reader model, judge model, dataset
  hash, prompt/rubric IDs, cost, latency, and caveats.

**Verify:**

```bash
moon run bench-gate-test
moon run bench-gate
moon run doc-claim-gate-test
moon run doc-claim-gate
```

**Exit:** the pinned report contains the `sibyl-longmemeval-s-qa-v1` QA block, the gate rejects
missing QA/accounting/runtime fields, and public docs no longer describe QA accuracy as planned
except for future reruns.

### Wave 4: Pin LongMemEval-V2 Official Receipt

**Status:** approval-bound because it needs a Qwen3.5-9B reader endpoint and paid `gpt-5.2`
evaluation.

**Tasks:** W5R, `a3441270-a6c3-42c3-abd5-837913a73ce9`.

**Files:** `.github/workflows/longmemeval-v2.yml`,
`benchmarks/results/ai-memory/pinned-longmemeval-v2.json`,
`benchmarks/results/ai-memory/manifest.json`, docs that mention LAFS Gain.

**Implementation:**

- Provision or select an OpenAI-compatible Qwen3.5-9B reader endpoint reachable by the workflow
  runner.
- Confirm `OPENAI_API_KEY` is present for Sibyl embeddings and the evaluator.
- Trigger the official full workflow:

```bash
gh workflow run longmemeval-v2.yml \
  -f run_official_full=true \
  -f official_tier=small \
  -f official_reader_base_url=<openai-compatible-qwen-reader-url> \
  -f official_reader_model=Qwen/Qwen3.5-9B \
  -f official_evaluator_model=gpt-5.2
```

- The workflow runs both `web` and `enterprise`, builds the official package, combines metrics, and
  emits `longmemeval_v2_<tier>_receipt.json`.
- Download `longmemeval-v2-combined-<tier>-<sha>` and promote the receipt to
  `benchmarks/results/ai-memory/pinned-longmemeval-v2.json`.
- Flip the manifest `longmemeval-v2-gate` from planned/non-blocking to blocking once the pinned
  receipt exists.
- Update public docs only with the exact LAFS Gain, tier, official repo SHA, dataset hashes, model
  IDs, cost, latency, and caveats.

**Verify:**

```bash
moon run bench-gate-test
moon run bench-gate -- benchmarks/results/ai-memory/pinned-longmemeval-v2.json --profile longmemeval-v2
moon run bench-gate
moon run doc-claim-gate
```

**Exit:** the receipt schema is `sibyl-longmemeval-v2-official-receipt-v1`, covers both web and
enterprise, and the manifest can block on it.

### Wave 5: Produce Live Usage-Loop Receipt

**Status:** local collector and planned manifest contract done; blocked on W0C and live dogfood run.

**Owner:** W6E, derived from W6C's live-receipt exit criterion.

**Files:** `tools/trust/usage_loop_gate.py`, `tools/trust/dogfood_receipts.py`,
`tools/tests/test_usage_loop_gate.py`,
`benchmarks/results/ai-memory/usage-loop-dogfood-receipt.json`,
`benchmarks/results/ai-memory/manifest.json`, `tools/bench/eval_gate.py`,
`tools/tests/test_bench_gate.py`.

**Implementation:**

- Use the committed read-only live observer to record a separate dogfood receipt without weakening
  the existing fixture gate.
- The observer queries the live org for:
  - recent `memory_usage_events`;
  - at least one exposure event and one citation event;
  - duplicate suppression by
    `(organization_id, session_key, message_key, item_kind, item_id, signal_type, source_surface)`;
  - stamped `last_recalled_at` and `last_used_at` on graph entities or raw captures;
  - a cited item whose decay score is meaningfully higher than an uncited peer.
- If live data is insufficient, run one approved dogfood session that uses `sibyl recall` or context
  packs and then records `sibyl cite <ids>` or `sibyl task complete --cited <ids>`.
- Keep `usage-loop-receipt.json` as the deterministic fixture receipt. Store live evidence in
  `usage-loop-dogfood-receipt.json`.
- Promote the committed planned `usage-loop-dogfood-gate` manifest contract to blocking once the
  receipt exists. Required fields:
  - `schema_version = sibyl-usage-loop-dogfood-receipt-v1`;
  - deployed API and web versions both equal the W0B v1.1 RC tag;
  - running API and web image digests match the W0B image receipt;
  - deployed source commit contains W6A, W6B, W6C, W6D, W7A, and W7C;
  - observed exposure event count >= 1;
  - observed citation event count >= 1;
  - duplicate stored event count = 0;
  - dedupe key fields present on sampled events;
  - sampled graph/raw records include `last_recalled_at` and `last_used_at` stamps after the
    observed session;
  - cited decay score advantage >= 0.1 against an uncited peer.
- Enforcement is split: `bench-gate` validates the manifest metric contracts once the entry is
  blocking (planned contracts pass without a receipt), while the finer required fields above are
  enforced at receipt-generation time by `tools/trust/usage_loop_gate.py` budgets and
  `tools/tests/test_usage_loop_gate.py` stale/incomplete-evidence rejection. Flip the manifest entry
  to blocking only after the live receipt exists.

**Verify:**

```bash
moon run usage-loop-gate-test
uv run python -m tools.trust.usage_loop_gate \
  --collect-dogfood-evidence benchmarks/results/ai-memory/usage-loop-dogfood-evidence.json \
  --deployment-evidence benchmarks/results/ai-memory/deployment-dogfood-evidence.json \
  --dogfood-receipt benchmarks/results/ai-memory/usage-loop-dogfood-receipt.json
moon run bench-gate-test
moon run bench-gate
```

**Exit:** the live receipt proves the deployed stack writes exposure and citation signals, and
`doc-claim-gate` can distinguish fixture proof from live dogfood proof.

### Wave 6: Produce Dogfood Forgetting Receipt

**Status:** local collector and planned manifest contract done; blocked on W0C and W6E live events.

**Task:** `bd29d2b1-b627-4bf3-94b0-e7ebaf52cf76`

**Files:** `tools/trust/forgetting_gate.py`, `tools/trust/dogfood_receipts.py`,
`tools/tests/test_forgetting_gate.py`,
`benchmarks/results/ai-memory/forgetting-dogfood-receipt.json`,
`benchmarks/results/ai-memory/manifest.json`, `tools/bench/eval_gate.py`,
`tools/tests/test_bench_gate.py`.

**Implementation:**

- Use the committed read-only dogfood observer/dry-run forgetting probe.
- Select stale uncited, exposed-only, and cited dogfood memories from the live graph.
- Compute priority decay scores using the same code path as consolidation.
- Do not archive live records as part of the receipt unless Bliss explicitly approves a forgetting
  sweep. A dry-run receipt is enough for v1.1.
- Prove:
  - cited memories survive above uncited peers;
  - live context/recall ranking reflects usage-aware temporal decay, not only an offline score
    helper;
  - protected cited false-archives = 0;
  - stale uncited reduction would meet the fixture budget;
  - W4 write-path integrity still passes.
- Promote the committed planned `forgetting-dogfood-gate` manifest contract to blocking once the
  receipt exists. Required fields:
  - `schema_version = sibyl-forgetting-dogfood-receipt-v1`;
  - deployed API and web versions both equal the W0B v1.1 RC tag;
  - running API and web image digests match the W0B image receipt;
  - deployed source commit contains W6A, W6B, W6C, W6D, W7A, and W7C;
  - stale uncited sample count >= 1;
  - cited protected sample count >= 1;
  - cited survival delta >= 1;
  - protected cited false-archive count = 0;
  - strict recall drop <= 0.5 percentage points;
  - dry-run mode is true unless Bliss approved a live forgetting sweep;
  - write-path integrity check status is PASS.
- Enforcement is split here too: `bench-gate` validates the manifest metric contracts once the entry
  is blocking, while receipts produced from any stack whose source predates W6/W7 are rejected at
  generation time by the `tools/trust/dogfood_receipts.py` required-commit checks and
  `tools/tests/test_forgetting_gate.py`. Flip the manifest entry to blocking only after the live
  receipt exists.

**Verify:**

```bash
moon run forgetting-gate-test
uv run python -m tools.trust.forgetting_gate \
  --collect-dogfood-evidence benchmarks/results/ai-memory/forgetting-dogfood-evidence.json \
  --deployment-evidence benchmarks/results/ai-memory/deployment-dogfood-evidence.json \
  --dogfood-receipt benchmarks/results/ai-memory/forgetting-dogfood-receipt.json
moon run forgetting-gate
moon run write-path-integrity-gate
moon run bench-gate
```

**Exit:** live dogfood evidence demonstrates cited-vs-uncited divergence without mutating production
memory unexpectedly.

### Wave 7: Final Claim Freeze And Release Readiness

**Status:** last wave after W1R, W5R, W6E, and W7B.

**Files:** `docs/testing/benchmark-methodology.md`, `docs/architecture/retrieval-system.md`,
`docs/architecture/SIBYL_POST_1_0_ROADMAP.md`, `docs/architecture/SIBYL_NORTHSTAR.md`,
`benchmarks/results/ai-memory/manifest.json`, `benchmarks/results/ai-memory/doc-claim-receipt.json`.

**Implementation:**

- Rerun W10's doc-claim gate after all new receipts are committed.
- Replace approval-bound labels only where the actual receipt now exists.
- Keep retrieval recall, QA accuracy, LAFS Gain, cost/latency, local-embedding, fixture citation
  usage, and live dogfood citation usage as separate evidence axes.
- Record release notes from manifest entries, not prose memory.
- Keep every v1.2/v1.3 concept in the deferred ledger below unless it blocks a v1.1 exit criterion.

**Verify:**

```bash
moon run doc-claim-gate-test
moon run doc-claim-gate
moon run bench-gate-test
moon run bench-gate
moon run :check
git diff --check
```

**Exit:** docs build and claim gate pass with all v1.1 receipts current, and there are no unlabeled
public claims.

## Concept Coverage Ledger

| Roadmap concept                                        | v1.1 owner | State                                                                        |
| ------------------------------------------------------ | ---------- | ---------------------------------------------------------------------------- |
| Retrieval-vs-QA honesty                                | W1, W10    | Lane/docs done; full paid receipt W1R remains                                |
| PR-gating eval regression                              | W2         | Done                                                                         |
| Deterministic local embedding                          | W2         | Done                                                                         |
| Cost, latency, token, embedding-call accounting        | W3         | Done                                                                         |
| HaluMem-style write-path integrity                     | W4         | Done                                                                         |
| Self-feeding guard and no-op extraction gate           | W4         | Done                                                                         |
| LongMemEval-V2 official published run                  | W5         | Scaffold done; paid official receipt W5R remains                             |
| Usage-event storage/service foundation                 | W6A        | Done                                                                         |
| Exposure stamping on context/search                    | W6B        | Done                                                                         |
| Explicit citation contract and fixture usage-loop gate | W6C        | Done                                                                         |
| Live dogfood usage-loop receipt                        | W6E        | Collector/contract done; receipt run after W0B/W0C                           |
| Usage-ordered consolidation input                      | W6D        | Done                                                                         |
| Uniform, usage-aware forgetting foundation             | W7A        | Done                                                                         |
| Exposure-vs-citation survival semantics                | W7C        | Done                                                                         |
| Dogfood cited-vs-uncited decay divergence              | W7B        | Collector/contract done; receipt run after W0B/W0C/W6E                       |
| Team scope enablement and policy                       | W8         | Done                                                                         |
| Team management control plane                          | W8         | Done                                                                         |
| Promote/SHARE with provenance                          | W8         | Done and review-hardened                                                     |
| Team-scope trust gate                                  | W8         | Done and review-hardened                                                     |
| OKF export                                             | W9         | Done                                                                         |
| Git-diffable memory changelog                          | W9         | Done                                                                         |
| Claim truth-up and landscape docs                      | W10        | Done locally; final freeze reruns after receipts                             |
| Cross-org sharing                                      | W8, W10    | Explicitly out for v1.1                                                      |
| Team role model                                        | W8         | Reuse project-role semantics as the v1.1 interim                             |
| Memory-space shape                                     | W8         | Hierarchical scope plus tag overlays as the v1.1 interim; team scope enabled |
| TeamMemBench dataset decision                          | W10        | Hybrid real-plus-synthetic documented for v1.2                               |
| Ontology pruning                                       | W10        | No enum deletion in v1.1; risky axis collapse deferred                       |
| Distillation, files projection, re-extraction          | W10        | v1.2 handoffs documented                                                     |

## Deferred Concept Ledger

These concepts are accounted for but stay outside v1.1's build scope.

| Concept                                                                                   | Target       | v1.1 handoff                                           |
| ----------------------------------------------------------------------------------------- | ------------ | ------------------------------------------------------ |
| Reversible coalescence data model                                                         | v1.2 W1      | Depends on W8 team substrate                           |
| Contributor aliases/assertions for humans and agents                                      | v1.2 W1      | W8 records same-org team substrate                     |
| Conflict lifecycle and revocation semantics                                               | v1.2 W1/W3   | W8 promote provenance preserves source data            |
| Live cross-contributor entity resolution                                                  | v1.2 W2      | Depends on coalescence model                           |
| Concurrent multi-writer consistency                                                       | v1.2 W3      | Depends on team substrate and conflict records         |
| `multi_user.py` scale/load gate                                                           | v1.2 W4      | W10 records matrix requirement                         |
| Large-corpus rehearsal replacement                                                        | v1.2 W4      | W10 records 57-record rehearsal as unresolved          |
| Filtered-HNSW `recall@k=0.0` finding                                                      | v1.2 W4      | W10 records scale investigation                        |
| TeamMemBench internal benchmark                                                           | v1.2 W5      | W10 documents hybrid dataset decision                  |
| External competitor baselines                                                             | v1.2/v1.3    | Fold into TeamMemBench and public evidence comparisons |
| Outcome-grounded utility axis                                                             | v1.2 W5/v1.3 | Consumes W6 citations and task graph                   |
| Distilled per-project handbook                                                            | v1.2 W6      | Consumes W6 usage and W4 integrity gates               |
| `.sibyl/memory/` files projection                                                         | v1.2 W7      | Reuses W9 projection machinery                         |
| Retroactive re-extraction loop                                                            | v1.2 W8      | Depends on W2/W3/W4 gates                              |
| Ontology axis collapse                                                                    | v1.2         | W10 documents current axes and invariant               |
| Public TeamMemBench dataset/leaderboard                                                   | v1.3         | Depends on v1.2 internal benchmark                     |
| Frontier retrieval, belief revision, procedural memory                                    | v1.3         | W10 keeps out of v1.1 claims                           |
| Platform reach: MCP backend distribution, Surreal live queries/Cloud, Haven, Rust runtime | v1.3         | W10 keeps as platform arc, not v1.1 execution          |
| OKF importer and DataBook typed round-trip                                                | v1.3         | W9 is export/projection only                           |

## Dependency DAG

```text
W0A -> W2/W3/W4/W6A/W7A
W2 -> W1 local -> W1R receipt
W3 -> W1 local -> W1R receipt
W2/W3 -> W5 local -> W5R receipt
W6A -> W6B -> W6C -> W6D -> W8
W6A -> W7A -> W7C
W6D -> W7C -> W7B
W4 -> W7A
W8 -> W9 -> W10 local
W10 local -> W0B dogfood images
W0B -> W0C
W0C -> W6E live usage receipt
W0C -> W7B dogfood forgetting receipt
W6E -> W7B
W1R -> final W10 freeze
W5R -> final W10 freeze
W6E -> final W10 freeze
W7B -> final W10 freeze
```

Parallel notes:

- The W1R and W5R paid workflow runs can run in parallel after approval. Every receipt-promotion
  commit touching `manifest.json` or public evidence docs must serialize, including W1R, W5R, W6E,
  W7B, and the final freeze.
- W6E and W7B must wait for W0B/W0C because dogfood evidence from `1.0.0-rc.8` or `1.0.2` cannot
  prove W6/W7 behavior.
- W7B must wait for W6E because it needs real exposure/citation events.
- Final claim freeze stays last because it reconciles docs against the final receipt set.

## Review Checkpoints

- Claude spec review passed after iteration on 2026-07-04. The review found and the plan now fixes
  ancestry checks against the approved source SHA, live deployment evidence wiring for W6E/W7B, W6D
  source-commit coverage, and receipt-promotion serialization across shared manifest/docs files.
- Independent cross-model review has passed for the W6E/W7B live receipt tooling and the W0B dogfood
  image workflow. Rerun review for future receipt promotion commits because they touch release
  evidence.
- Do not let review expand v1.1 scope. New ideas go into the deferred ledger or v1.2/v1.3 handoff
  unless they block a v1.1 exit criterion.

## Execution Rules

- Use `moon run` for repository quality checks and gates.
- Do not use raw `uv` or `pnpm` for monorepo lint/test/build/typecheck.
- Do not auto-start or restart `moon run dev`, Docker Compose, or the Hetzner stack without explicit
  approval.
- Do not mutate dogfood or production memory for W6E or W7B unless the mutation is explicitly
  approved; prefer dry-run observation.
- Land work in atomic, goal-aligned commits with focused verification receipts.
- Non-trivial implementation needs independent cross-model verification before completion.
- Do not commit this planning file or unrelated dirty files unless Bliss explicitly asks for that
  commit.
