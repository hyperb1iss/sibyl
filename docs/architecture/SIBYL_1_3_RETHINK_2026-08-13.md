# Sibyl 1.3 Rethink — expressiveness, structure, and the debt ledger

- Status: synthesis of the 2026-08-13 seven-lane audit (four Opus tech-debt deep dives, one
  expressiveness map, two SOTA sweeps). Direction proposal, not yet a locked plan.
- Inputs: full reports with file:line receipts in
  [`docs/architecture/AUDIT_2026-08-13/`](AUDIT_2026-08-13/). Nothing in this doc is asserted
  without a receipt in one of them.
- Relationship to [`SIBYL_1_3_STRATEGY_SKELETON.md`](SIBYL_1_3_STRATEGY_SKELETON.md): the skeleton's
  ingest-side levers survive intact; this doc adds a Phase 0 (benchmark decontamination) ahead of
  them and a representation workstream alongside them.

## 1. Verdicts on the framing questions

**Is Sibyl expressive enough?** The write surface is already among the richest in the field — the
systems sweep found no shipping product that asks agents for more structure than we do (scopes,
projects, types, tags, importance, confidence, epistemic basis, spans, retrieval keys, probes,
relationships). The defect is not vocabulary, it is consequence: roughly half the expressive axes
are stored and never read by retrieval, and the highest-value consumer (typed edge traversal
weights) exists with no writer able to feed it. Expressiveness is not the gap. Wiring is.

**Can an agent accurately express relationships and concepts?** No — and not because the schema
can't hold them. Every relationship an agent declares collapses to untyped `RELATED_TO` at write
(`tools/add.py:720`), while the expansion scorer already weights `DECIDES` 1.0, `REQUIRES` 0.98,
`SUPERSEDES` 0.95, `SUPPORTS` 0.94 (`retrieval/search.py:85-107`). The consumer is built; the
producer was never wired. That weight table is currently a wish list.

**Does it matter?** Only where structure changes retrieval behavior — and the research is unusually
crisp here. Typed _stores_ have the one clean ablation win in the field (ENGRAM: 77.6% vs 46.6% with
types collapsed). Typed _edges_ have zero controlled evidence anywhere, and the market is actively
retreating from them (Mem0 removed its queryable graph interface this week; Cursor killed
auto-Memories; Letta left the memory-layer market). The winning shape everywhere is coarse typed
categories + verbatim flat payloads + heavy multi-signal retrieval + write-time gating. Conclusion:
we do NOT need an ontology or an edge-schema migration. We need the small set of predicates
retrieval already scores, wired end to end, plus type-aware retrieval semantics over the enum we
already have.

**Should we reconsider tagging and categorization?** Yes, in both directions. Tags are inert today
(unindexed, zero WHERE clauses — pure write-only theater) and no research shows flat tags earning
accuracy as a scoring signal, so they should become a cheap filter/facet surface or be demoted
honestly. Entity types, by contrast, are under-used: eight of them (including `person` and `place`,
added deliberately for personal memory) are unreachable by recall because `FACET_TYPES` omits them.
Categories earn their keep exactly when they change retrieval behavior; ours mostly don't yet.

**What are we missing?** Ranked by evidence strength:

1. **Supersession and correction enforcement** — the field's loudest open gap and our worst instance
   of it. Proactive interference degrades retrieval log-linearly as stale associations accumulate
   (arXiv:2603.14517), and we don't just fail to filter superseded memories — we boost them:
   traversal carries no temporal predicate and `SUPERSEDES` weighs 0.95, so following the edge
   _promotes_ the superseded row. `sibyl correct` mutates only `raw_captures`; the graph row keeps
   ranking. `excluded_from_recall` is a declaration, not an enforcement.
2. **Reader conversion** — formally recognized loss bucket ("Sufficient Context", ICLR 2025) with
   cheap validated levers: CoT-before-answer, extract-then-answer, span-level citation forcing, and
   pack length itself as a reader tax even at perfect retrieval. We convert 50-54% when the gold
   literal is exposed.
3. **Failure-strategy distillation** (ReasoningBank lane) — distilling strategies from failures as
   well as successes is the closest research lane to coding-agent procedural memory, and we don't
   systematically exploit it.
4. **Entity aliasing/merging** — extraction dedupes only within a single batch; nothing merges
   across sessions.

## 2. The stored-vs-used matrix (compressed)

Full table with receipts in `expressiveness-map.md`.

| Axis                                                                                                           | Status                                                                                                                             |
| -------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------- |
| scopes, project, status, retrieval keys, entity type, freshness, usage signals                                 | **Load-bearing** (keys are their own fused lane; usage loop stretches half-life 4× on citation, collapses 10× at misled_count ≥ 5) |
| spans, atomic                                                                                                  | Used indirectly via minted passage rows                                                                                            |
| importance, pinned, retention                                                                                  | Retention job only — never retrieval                                                                                               |
| tags, priority, updated_at, probes, basis, confidence, edge weight, edge temporal validity, node_labels filter | **Stored, never read**                                                                                                             |
| typed predicates (DECIDES/REQUIRES/SUPPORTS/SUPERSEDES...)                                                     | **Read, never writable**                                                                                                           |

Two failure modes at once: expressed-but-ignored, and consumed-but-unwritable.

## 3. Benchmark integrity: the contamination finding

`retrieval/query_ranking.py:337-705` hardcodes LongMemEval corpus vocabulary in the production
ranker: `_CONCEPT_GROUPS` contains `seattle`, `basil`, `airfryer`, and the dataset misspelling
`buisiness`; twenty regex pairs match `dermatologist`, `triathlon`, `screen protectors`. These feed
`_CONCEPT_OVERLAP_WEIGHT` and `_QUERY_FRAME_WEIGHT` (0.52 — second-largest weight in the model).
`query_anchors.py:42-43` re-fixes the same typo.

Consequences, stated precisely:

- Every real user's search is scored against benchmark-tuned tables. Product defect regardless of
  benchmarks.
- The 30.38% anchor partly measures the tables, not the architecture. The **paired lever verdicts
  mostly survive** (both arms of every gate shared the contaminated ranker, same-commit), but the
  anchor _level_ and anything interacting with concept overlap is suspect until re-measured clean.
- Related and compounding: the ranking pipeline is ordinal end to end (`fusion.py:126` binds score
  magnitudes to a variable never read), so most weight/boost constants cannot change outcomes. Weeks
  of knob-tuning were structurally noise — which is consistent with how many lever classes died at
  the gates.

**Phase 0 of 1.3 is therefore: strip the contamination, decide the ordinal-vs-scored fusion question
deliberately, unify or explicitly pair the two retrieval pipelines, and cut a clean anchor.**
Protocol law from the 1.2 plan carries unchanged.

## 4. The debt ledger — kill order

~120 findings across the four lanes. Full detail in the four `debt-*.md` reports. Tiered:

**Tier 0 — security (fix first, small):** MCP never enforces `api:read`/`api:write` (check exists,
wired to `None` — an `["mcp"]`-scoped key is refused by REST and accepted by MCP writes);
`GET /backups/jobs/{job_id}` cross-org readable; stock `helm install` ships production-labelled with
MCP auth disabled; no test walks routes asserting auth (a ~30-line test catches the class).

**Tier 1 — serving wrong data:** corrections never reach retrieval; superseded edges boosted (§1);
deleted memories' passage spans survive the retire sweep and keep serving deleted text
(`projection/passages.py:606`); the #354 self-feeding-synthesis guard landed one stage upstream of
where packs are actually rebuilt; three-way metadata storage where deleted keys resurrect on read;
blind full-replace entity writes (three real data-loss incidents already, each hand-patched).

**Tier 2 — benchmark + retrieval integrity:** ranker decontamination (§3); two mutually-unaware
retrieval pipelines with divergent weight scales; ordinal fusion decision; exact-name rescue lane
gated on a condition true whenever search succeeds (never fires); passages don't inherit
`retrieval_keys` (the 1.2 headline feature contributes nothing to its highest-precision query
class).

**Tier 3 — reliability:** startup failures invisible to health probes; `coordination_backend=auto`
never selects Redis (default queue is in-process, loses everything on restart); CI change classifier
blind to `uv.lock`/`pyproject.toml`/`charts/`/`VERSION`/Dockerfiles; `moon run :check` never runs in
PR CI (~32 suites first run at RC cut); the browser e2e suite cannot fail (all five tests assert the
login redirect); `sibyl auth login` env-var precedence inverted vs the client (login succeeds,
everything after 401s and buffers silently); 19 CLI failure paths exit 0; buffered writes surface
nowhere a user looks; web realtime gives up permanently after 10 attempts with no polling fallback.

**Tier 4 — cleanliness:** 6,559 lines of domain logic in the API route package (the root cause of
MCP/REST divergence); dead `EntityStore`/`GraphStore` Protocol seam (zero production consumers);
`episode`/`mentions` tables defined everywhere, written only by archive-restore; three divergent
result normalizers; production code monkeypatching module globals as a test shim.

Corrections to prior beliefs, verified: `entity.updated_at` string hazard retired by migration v5;
the five dead Graphiti tables dropped by migration v4; the graph starfield fix has landed
client-side (500-node cap + live overview mode).

## 5. SOTA and differentiation

**Landscape (receipts in `sota-systems.md` / `sota-research.md`):** write-time structuring that
_replaces_ raw text is empirically refuted — extracted artifacts lose 15.9-22 points vs verbatim
chunks, and union storage (chunks + artifacts) fully recovers chunk accuracy. Retrieval method spans
20 points on LoCoMo while write strategy spans 3-8. Passage projection is on the right side of this
iff spans keep verbatim text and distilled forms coexist rather than replace — that is the binding
design rule for the skeleton's sub-1K rebuild. LongMemEval-V2 remains the only public benchmark in
our workload shape and **its leaderboard is still empty on both tiers** — our number would be the
first in existence.

**Contested since July:** team memory for coding agents (TencentDB Agent Memory v2.0, 21.2k stars in
90 days; Hindsight v0.9.0 shipping harness-pluggable memory for Claude Code) and
consolidation-with-a-brand-name (Dreaming / Mental Models).

**Verified open lanes:** first LME-V2 entry; schema-level private→team→org privacy ladder with
grants; deterministic LLM-free write paths (100% of surveyed systems put an LLM in the write path —
Sibyl's writer-declared path is already LLM-free-capable); cross-user coalescence under privacy; and
the one this audit makes ours to claim: **writer-declared structure that provably pays**. Everyone
else either extracts structure with an LLM or dropped structure entirely; nobody lets the agent
declare it and then _demonstrably_ routes retrieval through it. We have the declaration surface
shipped and the consumers half-built. Closing that loop is a story no competitor currently occupies:
"what your agent says when it saves is what changes what it recalls."

## 6. Proposed 1.3 shape

- **Phase 0 — clean room.** Ranker decontamination, fusion decision, pipeline unification (or
  explicit pairing), fresh anchor via the standard unpaid screen ladder. No lever work lands on a
  contaminated baseline.
- **Phase 1 — structure with consequences.** Writable typed predicates (small closed vocabulary:
  supersedes, contradicts, requires, supports, decides/caused-by) feeding the existing traversal
  weights; supersession + correction enforcement at traversal and pack admission (write-time gating
  per MemGuard, recency-override rewriting per Infini Memory); passages inherit `retrieval_keys`;
  facet completion for the eight unreachable entity types; tags become an indexed filter surface or
  are honestly demoted; confidence/basis either enter scoring or stop being collected.
- **Phase 2 — the skeleton's ingest levers**, unchanged in intent, sharpened by research: sub-1K
  spans keep verbatim text with distilled notes as union storage; retrieval-key declaration;
  premise/gotcha notes; candidate acquisition over the overfetch path.
- **Phase 3 — reader conversion + latency.** Extract-then-answer, citation forcing, pack-length tax,
  tighter packs at fixed recall.
- **Parallel lane — Tier 0/1/3 debt** as fix-mode work, sized S/M and largely independent of the
  benchmark ladder.

Measurement law carries forward whole: replay-before-paid, 3-pass paired gates, pre-registered
numbers, same-commit pairing, jitter floors, receipt-checked arm activity.
