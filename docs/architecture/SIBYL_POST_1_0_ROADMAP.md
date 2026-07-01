# Sibyl Post-1.0 Roadmap (v1.1 → v1.2 → v1.3)

- Status: active planning baseline
- Created: 2026-07-01
- Current release floor: v1.0.2 (1.0 shipped)
- Supersedes the post-1.0 "future work" framing in [`SIBYL_1_0_ROADMAP.md`](SIBYL_1_0_ROADMAP.md);
  the product truth remains [`SIBYL_NORTHSTAR.md`](SIBYL_NORTHSTAR.md).

This roadmap covers the three releases after 1.0. It was assembled from a full-codebase +
competitive-landscape research pass and a cross-model review, and it is grounded in verified code
reality (not the frozen pre-1.0 planning docs).

## 1. Thesis

The AI-memory field in mid-2026 has a credibility problem: purpose-built memory systems routinely
fail to beat naive full-context or even BM25 on _accuracy_ (SocialMemBench scores commercial
frameworks 0.12–0.18 vs 0.37 for full context; GroupMemBench shows BM25 matching most systems;
LoCoMo's full-context baseline beats Mem0). The honest, defensible wins in this field are **cost,
latency, token-efficiency, and isolation integrity** — not a bigger benchmark number.

Two structural facts make Sibyl's position stronger than it looks:

1. **The field is fragmenting away from unified graph substrates.** Mem0 dropped external graph
   stores; Cognee's isolation works on only three of its backends; Papr stitches together MongoDB +
   Neo4j + Qdrant + Redis. Sibyl's "graph + document + vector + full-text + auth in one engine, with
   physical namespace-per-org isolation" is a coherence story no competitor can tell truthfully.
2. **Team / collaborative memory is open on every axis.** No competitor does cross-user entity
   resolution, and _no benchmark measures team memory at all_ (GroupMemBench, EverMemBench,
   SocialMemBench, and GateMem are all early-2026 preprints showing systems fail; none use real team
   logs; there is no shared leaderboard).

The bet is a three-release arc:

- **v1.1 "Prove It"** — close the table-stakes eval gaps honestly and at PR-gating cadence; lay the
  team-memory substrate.
- **v1.2 "Coalesce It — safely"** — ship live, provably-scoped, reversible team-memory coalescence,
  and build the team benchmark internally.
- **v1.3 "Lead It"** — publish the benchmark the field lacks and push the frontier.

Positioning: _in a field where memory systems can't reliably beat `cat *.txt` on accuracy and can't
keep shared memory scoped, Sibyl's edge is not a bigger number — it is the only substrate that makes
multi-tenant, auth-scoped, graph-native team memory a database guarantee rather than an
application-layer hope._

## 2. Verified starting point

- **Retrieval is at ceiling on LongMemEval-S:** 96.96% strict R@5 / 98.90% R@10 (retrieval recall,
  no LLM in the path, run `26304777971`). Context-pack: 160/160, p95 84 ms, zero leaks.
- **Substrate is healthy.** The 2026-05-28 audit (C1–H10 + mediums) is fully remediated:
  transactions wrap destructive cascades; the per-org query lock is now a per-org connection pool; a
  shared ranker and shared RRF serve both retrieval surfaces (two surfaces — `hybrid_search` and
  `context_search` — remain by design); the empty-API-key-scope hole is closed. W13 (Graphiti /
  FalkorDB / PostgreSQL removal), W14 (Epic → task-tree), OIDC, Argon2id, Helm, and Ansible all
  shipped.
- **Forgetting/decay is partial and uneven.** The hybrid search path applies temporal decay by
  default (`apply_temporal=True`, 365-day half-life); the context/recall path does not
  (`temporal_target=None`). `jobs/consolidation.py::priority_decay` archives low-importance/stale
  entities reversibly. It is not yet uniform, tuned, or benchmarked.
- **Team memory: every ingredient exists, all disabled/unmanaged/offline.** `MemoryScope.TEAM` is
  hard-disabled in three places (`_ENABLED_MEMORY_SPACE_SCOPES`, the space-state machine, and
  `memory_policy.py`). The `teams` / `team_members` / `team_projects` tables and their RBAC
  resolution exist but there is no team-management route or CLI. Memory-space CRUD/member APIs
  already exist (`api/routes/memory.py`), so team management layers on them. A promotion pipeline
  exists but the SHARE action is refused (`scope_crossing_requires_promotion`). Two coalescence
  engines exist: online `retrieval/dedup.py` (HNSW candidate-gen + cosine) and offline
  `migrate/merge.py` (identity/collision reconciliation). The `sibyl-consolidation/` merge tarballs
  are a real dogfood of exactly this.
- **Eval infra: strong methodology, weak automation.** Honest hit-vs-strict-recall, per-question
  physical isolation, and gate + manifest provenance are in place. But evals do not gate PRs (manual
  `workflow_dispatch`); the nightly runs on a mock LLM; there is no time-series regression, no
  end-to-end QA-accuracy number, and no scored LongMemEval-V2; `tools/perf/multi_user.py` is
  exercised by an e2e perf test but not wired to a CI gate; `large_corpus_rehearsal` is 57 records;
  the filtered-HNSW `recall@k=0.0` finding is unresolved; zero competitor baselines are run.

## 3. Design principles

1. **Trust and reversibility come first.** One private-memory leak or one irreversible bad merge
   makes team memory radioactive. Every coalescence action must be scoped-by-construction,
   attributable, previewable, and undoable. This governs sequencing and scope.
2. **Lead with cost / latency / isolation, not benchmark size** — the field's only honest edge and
   Sibyl's structural strength.
3. **Honest benchmarks stay the moat** — one canonical run, never round up, and the
   retrieval-vs-QA-accuracy caveat always travels with the number.
4. **Eval the write path, not just the read path** — memory systems accumulate hallucinations during
   extraction/update that QA-only judging hides (HaluMem).
5. **Gates carry budgets, not slogans** — every gate names numeric thresholds (recall, latency,
   leak, error, cost).
6. **Do not chase discredited benchmarks** — LoCoMo is saturated, has ~6.4% wrong gold answers, and
   is harness-dependent; if cited at all, frame it critically. Prioritize LongMemEval-V2 and Sibyl's
   own team benchmark.
7. **Turn the platform threat into distribution** — be the MCP-native, OKF-speaking backend behind
   Anthropic's and others' memory tools rather than competing with client-side file memory.

## 4. v1.1 — "Prove It"

Theme: make every public claim boring and complete, move evals from manual to PR-gating, finish the
eval story, and enable the team-memory substrate. No headline claim ships without a receipt.

### W1. End-to-end QA-accuracy lane

Add a reader pass over Sibyl's retrieved LongMemEval-S sessions plus the official GPT-4o/`gpt-5.2`
judge; publish QA accuracy alongside the 96.96% R@5 retrieval number. Closes the miscategorization
gap where comparison tables read Sibyl's retrieval recall as if it were QA accuracy.

- Gate `qa-accuracy-gate`: publishable QA-accuracy number from a pinned run; fails if QA accuracy
  drops > 1.0 pp vs the last committed score.

### W2. Eval automation + regression-over-time

Scheduled real-key runs (stratified slice nightly, full weekly); a committed time-series ledger; and
a deterministic local-embedding variant (`all-MiniLM-L6-v2` / BGE-M3) so quality gates run on PRs
without OpenAI cost or nondeterminism.

- Gate `eval-regression-gate` (blocks PRs): strict recall@5 ≥ (last committed − 0.5 pp);
  context-pack p95 ≤ 1000 ms; `leak_count = 0`. Retires the mock-LLM nightly blind spot; also
  produces the local-embedding comparison number.

### W3. Cost / latency / token accounting

Co-report tokens per query, embedding calls, p50/p95, and a dollar estimate against a full-context
baseline, in every live artifact.

- Gate `cost-latency-gate`: per-query cost and p95 recorded; p95 within budget; cost regression
  flagged.

### W4. Write-path integrity (HaluMem-style)

Measure whether extraction and consolidation inject or reduce hallucinations, and gate it. On-trend,
under-shipped by competitors, and consistent with the honest-benchmark posture.

- Gate `write-path-integrity-gate`: extraction/consolidation hallucination rate ≤ threshold on a
  seeded fixture.

### W5. LongMemEval-V2 (published)

Stand up the official-full harness (Qwen3.5-9B reader + `gpt-5.2` evaluator, web + enterprise tiers)
and publish the LAFS-Gain number with a citable receipt. The external leaderboard is empty and the
benchmark is agent-shaped — it matches Sibyl's coding/agent use case, so an early credible entry is
high-leverage.

- Gate `longmemeval-v2-gate`: scored run with committed receipt; regression bound on LAFS Gain.

### W6. Forgetting: uniform + benchmarked

Apply temporal decay across the context/recall path (not just hybrid); confirm consolidation
scheduling; tune `priority_decay` and benchmark it (FadeMem-style storage-reduction %, recall
impact, write-path integrity). Turns the self-named "honest gap" into a measured feature. Depends on
W2's regression harness to catch ranking impact.

### W7. Team-memory foundation (substrate, not coalescence yet)

Enable the `team` scope across its three gates; ship team-management routes + CLI (layered on the
existing memory-space member APIs); wire team → memory-space; implement the SHARE/promote action
(the `scope_crossing_requires_promotion` path) with provenance and attribution retained.

- Gate `team-scope-trust-gate`: private / delegated / project memory provably cannot surface in a
  team pack; promotion is attributed and preview-shown; leak fixtures = 0.

### W8. Portability & interchange (OKF export)

`sibyl export --format okf` — a Sibyl → OKF (Google Open Knowledge Format v0.1) exporter: one
Markdown + YAML file per entity, relationships as Markdown links, with the labeled-property-graph
preserved losslessly via OKF-legal extension frontmatter (`sibyl_id`, an `edges:` list carrying
type/weight/target) so the bundle is valid OKF for other tools yet round-trips back into Sibyl.
Small (~2–4 days; reuses `graph_payload_from_archive()` and OKF's `visualize`), with
disproportionate payoff: it turns the "your memory stays yours, export in one command" sovereignty
pillar into a Google-blessed, vendor-neutral, git-diffable artifact and reinforces the MCP-backend
play.

### W9. Doc & claim truth-up

Land the doc-staleness reconciliation; keep benchmark-claim discipline; keep the AI-memory-landscape
doc accurate (retrieval-vs-QA framing, forgetting/decay reality).

Exit criteria: published QA-accuracy and LongMemEval-V2 numbers with citable receipts; a regression
gate blocks PRs on quality drops; the cost/latency curve is published; forgetting is uniform and
benchmarked; the `team` scope is enabled with proven isolation, a way to create/populate teams, and
a promote/SHARE path; OKF export ships.

## 5. v1.2 — "Coalesce It — safely"

Theme: turn the offline/online merge primitives into a live, provably-scoped, reversible,
provenance-preserving team-memory coalescence engine — with a first-class data model and
deterministic conflict states. Build the team benchmark internally. Cross-user entity resolution is
the field's unsolved problem; the differentiator is doing it _without ever leaking scope and always
reversibly_.

### W1. Coalescence data model & reversibility (build this first)

Before merging anything, define the model: **canonical entity vs contributor aliases vs contributor
assertions**; an attribution schema (who asserted what, when, from which source); a **conflict
lifecycle** with deterministic states (open / merged / superseded / contested); **split/undo**
(every merge is reversible); **revocation semantics** (a contributor leaves or a memory is retracted
→ the coalesced state recomputes); **redaction/anonymization transforms** applied before a memory
enters a shared space; and a **human review UX** for contested merges.

### W2. Live coalescence engine

Unify online `retrieval/dedup.py` (HNSW + cosine) and offline `migrate/merge.py` (identity/collision
reconciliation) into a live cross-contributor entity-resolution + relationship-redirection engine
scoped to a team space, emitting the W1 records. Merges are provenance-preserving — contributor
assertions are never destroyed.

### W3. Concurrent multi-writer consistency

Additive-safe vs conflict-checked write semantics (in the spirit of Letta's `memory_insert` /
`memory_replace`); bi-temporal edge invalidation for contradictions (Zep-style validity windows —
invalidate, don't delete). Deterministic conflict states, not silent merges. (Formal belief-revision
semantics are deferred to v1.3 unless observed conflicts justify them earlier.)

### W4. Eval team memory at scale (numeric spec)

Wire `multi_user.py` into a CI gate and define the load matrix explicitly: N orgs × users/org,
corpus size, QPS, and concurrent writes, with the cache/pool settings under stress (more than
`surreal_graph_client_cache_size` = 64 hot orgs, per-org pool saturation, cross-org fan-out).
Replace the 57-record rehearsal with adoption-grade corpora, and resolve the filtered-HNSW
`recall@k=0.0` finding at scale.

- Gate `scale-load-gate`: at the defined matrix, p95 ≤ budget, error rate = 0, `leak_count = 0`,
  recall ≥ floor.
- Gate `team-isolation-under-load-gate`: concurrent multi-tenant load plus revocation-under-load
  produces zero cross-tenant / cross-scope leakage.

### W5. TeamMemBench (internal)

Build the internal benchmark for cross-user entity resolution, concurrent multi-writer consistency,
and "helped AND stayed scoped" (utility + access control + forgetting, GateMem-style). Dogfood seed:
the eternia/macbook merge. Internal-only this release — publication waits for v1.3 once a defensible
dataset and an external comparator exist (synthetic-only risks "benchmark theater").

Exit criteria: reversible team coalescence with deterministic conflict states and attribution;
isolation proven under concurrent load and revocation; internal TeamMemBench passing with a
documented scale envelope (orgs / connections / latency).

## 6. v1.3 — "Lead It"

- **Publish TeamMemBench** (dataset + leaderboard) — only with a defensible dataset (real-team-log
  or a rigorously-justified hybrid) and at least one external comparator. Category-defining if done
  right; theater if rushed.
- **Frontier retrieval:** MAGMA-style multi-graph disentanglement (decide the seam in v1.2); an
  optional cross-encoder reranker path for diffuse-evidence question types; belief-revision
  semantics (Kumiho/AGM) _if_ observed conflicts justify it; procedural/skill memory.
- **Platform reach:** be the MCP-native, OKF-speaking backend behind Claude's `/memories` and other
  agents; SurrealDB live queries → reactive UI (awaits a patched Surreal); SurrealDB Cloud managed
  multi-tenant; the Haven lighthouse integration; the adjacent Rust high-throughput runtime
  (`docs/research/rust-port/`).
- **Interchange:** an OKF importer (needs LLM edge-inference from prose links); watch the W3C
  "DataBook" RDF/SPARQL profile as a better typed round-trip target than plain OKF. Keep
  `graph.json` as the lossless internal archive; OKF is the portable public projection.

## 7. Sequencing rationale

Evals come before the feature because team-memory coalescence _is_
isolation-correctness-under-merge: it cannot ship credibly without the harness (v1.1) to prove
isolation and quality, and the eval gaps are table-stakes the field judges on. v1.1 also front-loads
the honest-benchmark reputation before the v1.2 bet. v1.1's team substrate (scope enable + team
CRUD + promote) is the minimum for v1.2's engine to stand on. Within v1.2, the
reversibility/attribution model (W1) precedes the merge engine (W2) so nothing irreversible ships.
The public benchmark waits for v1.3 because a bad dataset is worse than none.

## 8. Decisions still to lock

- **Team role model** — reuse project roles for teams, or a distinct team-role set?
- **Memory spaces** — hierarchical, tag-based, or both?
- **TeamMemBench dataset** — real-team-logs (privacy-heavy), synthetic, or hybrid; this gates
  whether v1.3 publication is viable.
- **Cross-org sharing** — in scope for this arc, or explicitly out (org stays the hard boundary and
  team memory is within-org only)?
