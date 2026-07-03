# Sibyl Post-1.0 Roadmap (v1.1 → v1.2 → v1.3)

- Status: active planning baseline
- Created: 2026-07-01
- Revised: 2026-07-03 — usage-feedback loop, distillation/projection layers, and agents-first
  coalescence added after a cross-system comparison against a production memory system surfaced the
  open-loop finding (see §2).
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

1. **The substrate is necessary, but it is no longer the moat.** A unified engine still matters —
   Mem0 dropped external graph stores; Cognee's isolation works on only three of its backends; Papr
   stitches together MongoDB + Neo4j + Qdrant + Redis. But SurrealDB's own **Spectron** (agent
   memory; ~$23M, Feb 2026) now makes the unified-substrate / typed-memory / forgetting-as-a-verb
   argument on the very engine Sibyl runs on. You cannot out-database the database vendor. Sibyl's
   durable edge is therefore _above_ the substrate: the governed, self-hostable memory **product** —
   the memory loop as legible verbs, a real auth/multi-tenancy runtime, task coordination as a
   first-class memory citizen, and coalesced team memory with provenance and audit.
2. **Team / collaborative memory is open on every axis, and the literature agrees.** Almost all
   "multi-agent memory" in the market is per-agent namespacing, not coalescence. The 2026 governance
   literature names four failure modes no system clears — unauthorized leakage, stale propagation,
   contradiction persistence, provenance collapse (ArgusFleet, arXiv:2606.24535); GateMem
   (arXiv:2606.18829) shows no method achieves utility, access control, and forgetting together; and
   _no public benchmark measures team memory at all_. Sibyl already ships the two hardest
   prerequisites: a real multi-tenant auth runtime and a unified graph.

The bet is a three-release arc:

- **v1.1 "Prove It"** — close the table-stakes eval gaps honestly and at PR-gating cadence; close
  the usage feedback loop so forgetting means "unused = dead", not "old = dead"; lay the team-memory
  substrate.
- **v1.2 "Coalesce It — safely"** — ship live, provably-scoped, reversible memory coalescence
  (agents-first, then human teams), the distillation + files-projection layers, and the team
  benchmark internally.
- **v1.3 "Lead It"** — publish the benchmark the field lacks and push the frontier.

Positioning: _in a field where memory systems can't reliably beat `cat *.txt` on accuracy and can't
keep shared memory scoped, Sibyl's edge is not a bigger number and not the storage engine — it is
making auth-scoped, graph-native team memory a database guarantee instead of an application-layer
hope, delivered as a governed, self-hostable product: the one layer the frontier labs won't build
and the benchmarks say no one has solved._

## 2. Verified starting point

- **Retrieval is at ceiling on LongMemEval-S:** 96.96% strict R@5 / 98.90% R@10 (retrieval recall,
  no LLM in the path, run `26304777971`). Context-pack: 160/160, p95 84 ms, zero leaks.
- **Substrate is healthy.** The 2026-05-28 audit (C1–H10 + mediums) is fully remediated:
  transactions wrap destructive cascades; the per-org query lock is now a per-org connection pool; a
  shared ranker and shared RRF serve both retrieval surfaces (two surfaces — `hybrid_search` and
  `context_search` — remain by design); the empty-API-key-scope hole is closed. W13 (Graphiti /
  FalkorDB / PostgreSQL removal), W14 (Epic → task-tree), OIDC, Argon2id, Helm, and Ansible all
  shipped.
- **Forgetting/decay is partial, uneven — and blind.** The hybrid search path applies temporal decay
  by default (`apply_temporal=True`, 365-day half-life); the context/recall path does not
  (`temporal_target=None`). `jobs/consolidation.py::priority_decay` archives low-importance/stale
  entities reversibly. It is not yet uniform, tuned, or benchmarked. Worse (verified 2026-07-03):
  `priority_decay` reads `last_recalled_at` / `last_accessed_at` / `last_used_at`, but **nothing in
  the codebase writes those fields** (the only `last_used_at` writer is `api_keys` auth tracking).
  The recency term always falls back to `created_at`, so forgetting is effectively age-only — "old =
  dead" instead of "unused = dead". There is no usage signal anywhere in the memory loop: no
  citation contract, no recall-time stamping, no "did this memory help" feedback of any kind.
- **Two structural advantages exist but are not wired as loops.** (a) Verbatim raw captures +
  lineage edges (`derived_from` / `extracted_into`) + a versioned extractor
  (`sibyl_reflection_extractor v0.12`) mean every extractor improvement could be replayed over the
  historical corpus — but no re-extraction path exists. (b) Tasks and memories share one graph
  (`PRODUCES` / `CAPTURED_IN`, `complete_task(learnings=...)`), so memory utility could be grounded
  in task outcomes — but no outcome signal is derived. Both are unique in the field; competitors'
  rawest artifact is already an LLM product and no competitor has tasks in the graph.
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
   Anthropic's and others' memory tools rather than competing with client-side file memory. The
   posture is **structured substrate, curated projection**: the graph stays the source of truth;
   filesystem-native agents get it projected as curated files.
8. **Close loops before tuning them.** Linear work (better evals, tuned decay, more types) improves
   the system additively; feedback loops compound. Sequence loop-closing work (usage signal,
   re-extraction, outcome grounding) ahead of parameter tuning that would otherwise tune noise — and
   never tune a decay function whose input signal does not exist yet.

## 4. v1.1 — "Prove It"

Theme: make every public claim boring and complete, move evals from manual to PR-gating, finish the
eval story, close the usage feedback loop, and enable the team-memory substrate. No headline claim
ships without a receipt.

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

Includes two cheap write-hygiene guards surfaced by the cross-system comparison:

- **Self-feeding guard:** the nightly dream cycle reflects on recent session sources — verify (and
  enforce with a fixture) that reflection sessions and synthesis outputs are excluded from their own
  input, or the graph slowly fills with memories about remembering.
- **No-op gate on background extraction:** minimum-signal-or-write-nothing discipline in
  `jobs/memory_extraction.py`, so low-signal sources leave no residue and the review queue stays
  lean.

- Gate `write-path-integrity-gate`: extraction/consolidation hallucination rate ≤ threshold on a
  seeded fixture; self-feed fixture produces zero self-referential writes.

### W5. LongMemEval-V2 (published)

Stand up the official-full harness (Qwen3.5-9B reader + `gpt-5.2` evaluator, web + enterprise tiers)
and publish the LAFS-Gain number with a citable receipt. The external leaderboard is empty and the
benchmark is agent-shaped — it matches Sibyl's coding/agent use case, so an early credible entry is
high-leverage.

- Gate `longmemeval-v2-gate`: scored run with committed receipt; regression bound on LAFS Gain.

### W6. Usage feedback loop (citation contract) — before decay tuning

The single highest-leverage item in the release. Today the decay/priority machinery reads usage
fields nothing writes (§2); this work item writes them, turning forgetting from "old = dead" into
"unused = dead" — which is what forgetting is for — and giving consolidation a priority signal. The
proven production pattern is the citation contract (agents report which memories informed each
answer; a response hook records idempotent usage events; usage drives survival and ranking), adapted
here to an API consumer:

- **Contract:** context packs and search responses already carry stable item IDs. The skill packs
  ship the citing instruction the same way the read-path guidance ships today: when recalled memory
  informs an answer, cite it — a `cited_ids` field on `task complete` / `reflect`, and/or an
  explicit `sibyl cite <ids>` verb. MCP responses and the SessionStart hook carry pack-item IDs so
  citing is cheap.
- **Recording:** a server-side hook writes idempotent usage events (message/session-keyed dedup) and
  stamps `last_recalled_at` (exposure — weak signal, stamped at context-pack build even without
  agent cooperation) and `last_used_at` (citation — strong signal) on entities and raw captures.
- **Consumption:** `priority_decay` uses the stamps it was already designed to read; the nightly
  consolidator orders its input by usage the way the field's best write paths do
  (`retrieval_count DESC, last_used_at DESC`).
- **Honesty caveat travels with the feature:** citation is self-reported and measures "informed an
  answer," not task-outcome lift; outcome grounding lands in v1.2 (TeamMemBench utility axis).

- Gate `usage-loop-gate`: usage events flow end-to-end from a live agent session; seeded fixture
  where a cited entity measurably outlives its uncited twin through decay; recall-time stamping
  present on 100% of context-pack builds.

### W7. Forgetting: uniform + benchmarked

Apply temporal decay across the context/recall path (not just hybrid); confirm consolidation
scheduling; tune `priority_decay` and benchmark it (FadeMem-style storage-reduction %, recall
impact, write-path integrity). Turns the self-named "honest gap" into a measured feature. Depends on
W2's regression harness to catch ranking impact — and **hard-depends on W6**: tuning decay
half-lives while the usage fields are unwritten is tuning noise on an age-only signal.

### W8. Team-memory foundation (substrate, not coalescence yet)

Enable the `team` scope across its three gates; ship team-management routes + CLI (layered on the
existing memory-space member APIs); wire team → memory-space; implement the SHARE/promote action
(the `scope_crossing_requires_promotion` path) with provenance and attribution retained.

- Gate `team-scope-trust-gate`: private / delegated / project memory provably cannot surface in a
  team pack; promotion is attributed and preview-shown; leak fixtures = 0.

### W9. Portability & interchange (OKF export) — doubles as the memory changelog

`sibyl export --format okf` — a Sibyl → OKF (Google Open Knowledge Format v0.1) exporter: one
Markdown + YAML file per entity, relationships as Markdown links, with the labeled-property-graph
preserved losslessly via OKF-legal extension frontmatter (`sibyl_id`, an `edges:` list carrying
type/weight/target) so the bundle is valid OKF for other tools yet round-trips back into Sibyl.
Small (~2–4 days; reuses `graph_payload_from_archive()` and OKF's `visualize`), with
disproportionate payoff: it turns the "your memory stays yours, export in one command" sovereignty
pillar into a Google-blessed, vendor-neutral, git-diffable artifact and reinforces the MCP-backend
play.

The git-diffability is a feature, not a side effect: a scheduled export committed to a branch makes
every consolidation, decay, and promotion cycle a **reviewable diff** — "what did the agent learn
this week" becomes `git log`, memory edits get before/after content humans can actually read, and
memory gains a rollback story. This is the auditable-memory-edits property the strongest
consolidation designs get from git-baseline sandboxes, obtained here for the cost of a cron job.

### W10. Doc & claim truth-up

Land the doc-staleness reconciliation; keep benchmark-claim discipline; keep the AI-memory-landscape
doc accurate (retrieval-vs-QA framing, forgetting/decay reality).

Exit criteria: published QA-accuracy and LongMemEval-V2 numbers with citable receipts; a regression
gate blocks PRs on quality drops; the cost/latency curve is published; **the usage loop is closed
(cited-vs-uncited decay divergence demonstrated on fixtures and observed on the dogfood graph)**;
forgetting is uniform, usage-aware, and benchmarked; the `team` scope is enabled with proven
isolation, a way to create/populate teams, and a promote/SHARE path; OKF export ships with the
scheduled memory-changelog mode.

## 5. v1.2 — "Coalesce It — safely"

Theme: turn the offline/online merge primitives into a live, provably-scoped, reversible,
provenance-preserving team-memory coalescence engine — with a first-class data model and
deterministic conflict states. Build the team benchmark internally. Cross-user entity resolution is
the field's unsolved problem; the differentiator is doing it _without ever leaking scope and always
reversibly_.

**Contributors are writers, not just humans.** The near-term wedge is one operator running agent
swarms: multiple agents writing into one project is the same coalescence problem — cross-writer
entity resolution, attribution, conflict states — with zero privacy risk and an immediate dogfood
(the `sibyl-consolidation/` merge tarballs are exactly this workload). Prove the engine agents-first
on our own graph; human teams then land on the same records via the v1.1 team substrate. Also in
this release: the two projection layers (distilled handbook, memory-as-files) and the re-extraction
loop, all of which consume the v1.1 usage signal.

### W1. Coalescence data model & reversibility (build this first)

Before merging anything, define the model: **canonical entity vs contributor aliases vs contributor
assertions**; an attribution schema (which principal — human _or agent_ — asserted what, when, from
which source); a **conflict lifecycle** with deterministic states (open / merged / superseded /
contested); **split/undo** (every merge is reversible); **revocation semantics** (a contributor
leaves or a memory is retracted → the coalesced state recomputes); **redaction/anonymization
transforms** applied before a memory enters a shared space; and a **human review UX** for contested
merges.

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

**The utility axis is outcome-grounded — the field first no one else can do.** Tasks and memories
share the graph, and the v1.1 usage loop records citations, so "helped" can be measured as _cited
during tasks that completed_ rather than retrieval recall or LLM-judged relevance. No public memory
benchmark has a ground-truth utility axis; this is TeamMemBench's defensible novelty and the reason
it must not ship as retrieval-recall-in-a-team-costume.

### W6. Distillation pass — the per-project handbook

Mechanical merge keeps N records; distillation compresses knowledge. Add a dream-cycle stage that
maintains a **distilled per-project summary artifact** (a navigable handbook + wake summary),
regenerated when the underlying graph has changed enough, built with the existing
`synthesis_plan/draft/verify` tools. Wake bundles and the SessionStart hook serve the distilled
artifact instead of raw top-k assembly — curated summaries beat top-k for wake context, which is
where the strongest production systems and the frontier-lab file-memory designs all converged. The
v1.1 usage signal tells the distiller what has earned prominence; the W4 write-path-integrity gate
(v1.1) applies to the distiller too, since an LLM rewrite pass is a hallucination surface.

### W7. Materialized memory-as-files (`.sibyl/memory/`)

`sibyl export --project <id>` plus a session hook that materializes the distilled handbook (W6) +
recent context pack into `.sibyl/memory/` as read-only files. Filesystem-native agents (Claude Code,
Codex) grep it at zero marginal latency and keep working when the server is unreachable; the
citation contract still routes usage back over the API. This is design principle 7 made concrete —
**structured substrate, curated projection**: Sibyl remains the governed source of truth _behind_
the flat-file UX the frontier converged on, instead of competing with it. Composes with W6 and with
the v1.1 OKF exporter (shared projection machinery).

### W8. Retroactive re-extraction loop

Verbatim raw captures + lineage edges + a versioned extractor mean every extraction/distillation
improvement can be **replayed over the entire historical corpus** — memory quality compounds
retroactively, a property no competitor has (their rawest artifact is already an LLM product). Wire
it as a loop: `sibyl admin re-extract --since-extractor-version <v>` re-runs extraction over raw
captures, diffs the derived entities against current, and scores the delta with the v1.1 regression
harness before promoting. Supersession edges preserve the old derivation for rollback.

Exit criteria: reversible team coalescence with deterministic conflict states and attribution,
proven on the agent-swarm dogfood; isolation proven under concurrent load and revocation; internal
TeamMemBench passing with a documented scale envelope (orgs / connections / latency) and an
outcome-grounded utility metric; distilled handbook + files projection shipped and serving wake
bundles; one re-extraction replay executed end-to-end with a scored, gated delta.

## 6. v1.3 — "Lead It"

- **Publish TeamMemBench** (dataset + leaderboard) — only with a defensible dataset (real-team-log
  or a rigorously-justified hybrid) and at least one external comparator. Category-defining if done
  right; theater if rushed.
- **Frontier retrieval:** MAGMA-style multi-graph disentanglement (decide the seam in v1.2); an
  optional cross-encoder reranker path for diffuse-evidence question types; belief-revision
  semantics (Kumiho/AGM) _if_ observed conflicts justify it; procedural/skill memory;
  **outcome-conditioned memory valuation** — decay/priority driven by task-outcome lift (the v1.1
  citation signal correlated with v1.2 outcome grounding), the strongest form of "unused = dead".
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

The loop-closing order is deliberate (principle 8): the usage signal (v1.1 W6) precedes decay tuning
(v1.1 W7) because tuning age-only decay is tuning noise; it also precedes the distillation and
projection layers (v1.2 W6–W7) so the distiller knows what earned prominence, and TeamMemBench (v1.2
W5) so utility can be outcome-grounded. Re-extraction (v1.2 W8) waits for the v1.1 regression
harness because a replay without a scoring gate is a corpus-wide unreviewed rewrite.
Agents-as-contributors precede human teams because it is the same engine with zero privacy risk,
dogfoodable on day one.

## 8. Decisions still to lock

- **Citation contract shape** — explicit verb (`sibyl cite <ids>`), completion-field
  (`task complete --cited`), or both; and whether recall-time exposure stamping counts toward
  survival or only citation does (recommendation: exposure slows decay, citation resets it).
- **Team role model** — reuse project roles for teams, or a distinct team-role set?
- **Memory spaces** — hierarchical, tag-based, or both?
- **TeamMemBench dataset** — real-team-logs (privacy-heavy), synthetic, or hybrid; this gates
  whether v1.3 publication is viable.
- **Cross-org sharing** — in scope for this arc, or explicitly out (org stays the hard boundary and
  team memory is within-org only)?
- **Ontology pruning** — audit type/edge usage on the dogfood graph (`sibyl debug schema`): of ~25
  entity types and ~40 relationship types, which do write paths actually create? Collapse the four
  overlapping classification axes (`entity_type`, `labels[]`, `tags[]`, per-type `category`) to two
  — `entity_type` drives behavior (facets, lifecycle, policy), `tags` boost retrieval — and drop
  dead enum entries (e.g. `COMMUNITY`, whose detection was removed). Standing invariant: types route
  memories _within_ a context pack, they must never gate _whether_ a memory surfaces —
  classification errors must stay mis-shelvings, never silent retrieval failures.
