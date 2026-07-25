---
title: "SOTA Landscape — Agent Memory, July 2026"
description: Six-lane industry/academic sweep validating the v1.2 "Retrieve It" plan
date: 2026-07-25
status: research
---

# SOTA Landscape: Agent Memory — 2026-07-25

Six parallel research lanes (competitors, benchmarks, academic literature, platform-native memory,
retrieval production practice, team-memory/privacy) run 2026-07-25 to validate the v1.2 plan
([`SIBYL_1_2_IMPLEMENTATION_PLAN.md`](SIBYL_1_2_IMPLEMENTATION_PLAN.md)) before locking it. Every
load-bearing claim below was pinned to a primary source (GitHub repo, arXiv ID, official doc,
release page) fetched on the research date; vendor benchmark numbers are labeled as claims with
their axis. Supersedes the competitive read of
[`COMPETITIVE_ANALYSIS_SIBYL_VS_COGNEE_VS_HINDSIGHT_2026-05-31.md`](COMPETITIVE_ANALYSIS_SIBYL_VS_COGNEE_VS_HINDSIGHT_2026-05-31.md).

## TL;DR

**The v1.2 plan survives SOTA contact intact — every Track A/B/C bet is independently validated by
both production practice and 2026 literature — and three of its strategic payoffs got bigger since
it was drafted.** The official LongMemEval-V2 leaderboard is live and empty (first credible entry is
still available, and its scoring metric is the accuracy+latency shape Sibyl already reports). The
projection-layer bet turned out to be the industry-standard architecture (OpenAI, Anthropic, and
Google all independently shipped distilled-summary + greppable-index + detail-on-demand memory in
Feb–May 2026), with a Google-backed public spec (OKF v0.1) as the export target Sibyl already
supports. And coalescence-under-privacy remains shipped by nobody, so the v1.3 deferral holds.

Three adjustments were adopted into the plan from this sweep; nothing was removed:

1. **A2 gains a named requirement**: an exact-match lexical escape hatch or cross-encoder rerank
   stage. Dropping BM25 from fusion is measured and defensible; dropping _lexical signal entirely_
   leaves identifier/error-string queries unprotected, and retrieve-wide-then-rerank is the 2026 fix
   of record.
2. **A1 gains an offline eval arm**: contextualized chunk embeddings (voyage-context-4, 2026-06-29)
   now beat manual header prepends on the vector side by ~7% chunk-level. Headers stay (they feed
   BM25 and the reader); the embedder arm is cheap to measure.
3. **A3 gains a routing rule**: single-hop queries route around the agentic loop entirely. The June
   2026 ablation literature shows two retrieval iterations capture 95% of the gain of five, and the
   win concentrates on genuinely multi-hop questions.

## 1. Competitive field (delta since 2026-05-31)

The field re-sorted dramatically in two months:

- **Letta vacated the lane.** The flagship repo is now labeled legacy infrastructure; development
  moved to Letta Code (April 2026, a memory-first coding agent, 2.9k stars). The strongest research
  brand in agent memory left the embeddable-memory-layer market. Sleep-time compute shipped as its
  "dreaming" feature — validation for the v1.3 dream-cycle direction, no longer competition for the
  memory-layer lane.
- **Zep closed the self-host door.** Community Edition dead (Apr 2025, more retirements Feb 2026);
  repositioned as enterprise "Context Lake" (SOC 2, ABAC, BYOC — cloud only). Graphiti (29.2k stars,
  Apache-2.0) remains as an engine: no auth, no tenancy, no product.
- **Cognee is the live OSS threat.** 17.5k → 29.3k stars in ~2 months around its 1.0 launch
  (2026-06-26), weekly releases, €7.5M seed, 70+ orgs in production incl. Bayer. Positioning:
  "open-source memory platform," single-Postgres simplicity pitch, dataset-level RBAC.
- **Mem0 is the volume leader** (61.7k stars, $24M raised): April 2026 algorithm rewrite, July 2026
  "State of AI Agent Memory" report claiming LongMemEval-v1 94.4% — and, unusually, admitting real
  caveats (privacy is "application-layer decisions today"; identity resolution unsolved).
- **Hindsight** (18.8k stars, MIT) keeps the benchmark-marketing crown (LongMemEval-v1 91.4%, BEAM
  10M 64.1%) plus a July integration blitz and Hindsight Cloud.
- **New entrants**: EverMind EverOS (11.5k stars, Markdown-native, anomalous star/commit ratio),
  MemMachine, MemOS/MemTensor, Supermemory, CaviraOSS OpenMemory (ships migration importers _from_
  Mem0/Zep/Supermemory). No YC-batch breakout; consolidation around incumbents.
- **The big-lab squeeze is real but siloed.** Anthropic Managed Agents memory (public beta Apr
  2026), OpenAI Agents SDK memory primitives (Apr 2026), Copilot Memory default-on (Mar 2026),
  ChatGPT "Dreaming" (Jun 2026). Every one is a per-platform silo; the only cross-vendor motion is
  one-time import-to-switch weapons (Mar 2026), and MCP's roadmap explicitly leaves memory out of
  protocol scope.

**Sibyl-only ground, confirmed by absence across the whole field:** schema-level memory-scope
privacy (private→team→org ladder with grant semantics), deterministic/LLM-free write path (100% of
surveyed systems put an LLM in the write path; Graphiti ships hallucination "defenses" to treat the
symptom), memory + task coordination in one graph, hard namespace-per-org tenant isolation in OSS,
CLI-first cross-agent surface, and honest benchmark culture (three vendors simultaneously claim
LongMemEval-v1 SOTA; arXiv 2605.24060 documents the incomparability).

## 2. Benchmark state

- **LongMemEval-v1 is saturating** (vendor claims cluster 88–95%, all self-published, axes conflated
  — Supermemory's "95%" is Recall@15, not e2e QA). **LoCoMo is discredited**: the Penfield Labs
  audit (2026-04-04) found 6.4% of the answer key wrong and the judge accepting 62.8% of
  adversarially wrong answers — theoretical ceiling ≈93.6%, which several vendor claims exceed.
  Nobody serious treats these as differentiators anymore.
- **LongMemEval-V2 (arXiv 2605.12493) is the new agentic standard, and its official leaderboard is
  EMPTY as of 2026-07-25.** No vendor has published a v2 number anywhere. Sibyl's 31.26% full-451
  would be the first third-party number in existence. The official metric is **LAFS Gain**
  (latency-aware frontier score, 1s–200s budgets, multi-operating-point submissions) — exactly the
  accuracy+latency reporting shape Sibyl already produces. Precision note for all our writeups: the
  42.8%/51.0% reference numbers are the **Small-tier** baselines (Medium tier: 38.1%/45.9%); Sibyl's
  harness runs the Small-tier haystack.
- **Every reported score is (system × backbone × judge × dataset-era)**, not a system property: Zep
  spans 71.2→90.2 across eras; Hindsight moves ~8pp by backbone; MemDelta (arXiv 2606.29914) shows
  swapping only the embedding model moves accuracy ±6.2pp — enough to reverse rankings.
- **Integrity discourse is loud and moving our way**: Penfield's six-requirement checklist,
  MemDelta's confound analysis, survey-level calls for a GLUE-style shared harness, and Mem0's own
  concession that "accuracy without a token budget is a half-finished score." The credibility
  markers the field converged on (pinned dataset version, named judge + published prompt, fixed
  embedder across comparisons, tokens+latency alongside accuracy, open harness) are things Sibyl's
  protocol law already does.
- **Adjacent new benchmarks worth knowing**: GroupMemBench (Microsoft, arXiv 2605.14498) for
  multi-party memory — best system scores 46% and plain BM25 matches most dedicated memory systems,
  so the team-memory space is wide open (relevant to the v1.3 TeamMemBench ambition — a public
  target now exists). GateMem (arXiv 2606.18829) for shared-memory governance. MemSyco-Bench (arXiv
  2607.01071) for memory sycophancy — externally validates the errors-gotchas pool diagnosis
  (premise never checked) and is a candidate secondary eval for the A2 gotcha/premise notes.

## 3. Track A bets vs the evidence

Every bet validated; citations are the load-bearing ones.

- **Slice-granular substrate (A1) — validated twice over.** Academic: verbatim chunk stores beat
  extraction pipelines by 16–22 points ("Fidelity Before Structure," arXiv 2601.00821);
  boundary-segmented units beat turn/session/summary granularity (SeCom arXiv 2502.05589, ES-Mem
  arXiv 2601.07582); small units must carry inherited context (late chunking arXiv 2409.04701;
  Anthropic contextual retrieval, −49% retrieval failures). Production: 256–512-token chunks with
  prepended context are the 2026 standard; Sibyl's ~1K-char slices sit in the child-chunk class of
  parent-child retrieval, and the 3-slice window is the small-to-big pattern. Frontier caveat →
  **new offline arm**: contextualized chunk embedders (voyage-context-4, $0.12/1M) beat
  manual-header embeddings ~7% chunk-level; headers stay for BM25 + reader visibility.
- **Character-budgeted packs (A1) — validated.** Context-rot research (Chroma 2025-07, 18 models;
  "More Documents, Same Length" arXiv 2503.04388: more retrieved docs at constant length cuts
  performance up to 20%) makes budget, not item-count, the correct control knob. "Fishing for
  Answers" (arXiv 2509.04820) shows budget-constrained one-shot selection competitive with iterative
  retrieval.
- **Dense-first fusion (A1) — mechanism confirmed, one exposed flank.** The industry abandoned naive
  RRF for score-aware fusion (Weaviate relativeScoreFusion default since v1.24; Qdrant DBSF) because
  rank-only fusion weights a weak arm equally — exactly the measured poisoning. BM25's b=0.75 is
  mistuned for <100-word chunks (b≈0.3 advised), and inherited headers crater IDF discrimination on
  slice corpora — the mechanism behind our offline result. **But** the field's fix of record is
  retrieve-wide + cross-encoder rerank (Anthropic ladder −35/−49/−67%; Zep runs RRF→cross-encoder;
  rerank cost +50–200ms, zerank-2 / Cohere Rerank 4 current leaders), not dense-only. → **A2
  requirement**: lexical escape hatch or rerank stage for identifier-shaped queries.
- **Reserved 3-slot notes lane (A1/A2) — specifically vindicated.** TriMem (arXiv 2605.19952) wins
  with three _parallel_ layers (raw + facts + profiles); 2601.00821's explicit recommendation is
  "structured memory should augment verbatim text rather than replace it"; production working-memory
  injections are tiny and always-on (Mem0 ~130–166 tokens/call). Absolute slots are the right shape
  under context rot. Cost caution (ICLR 2026 MemAgents, arXiv 2603.02473): write strategy moves
  accuracy 3–8 points while retrieval moves 20 — distillation must stay cheap relative to retrieval
  work.
- **Bounded agentic traversal (A3) — validated on both halves, plus a routing rule.**
  Substrate-first: Claude Code's pivot to agentic search worked because the primitives were fast and
  precise — the loop is only as good as its substrate. Bounded: "Dissecting Agentic RAG" (arXiv
  2606.21553) — two retrieval iterations capture 95% of the gain of five, and fixed pipelines beat
  un-learned adaptive routing (the accurate-mode −4pp/2.5× result is textbook, predicted by the
  literature); LatentRAG (arXiv 2605.06285) measures token-space agentic loops at ~15× single-step
  latency. Interactive round budgets in the wild: 2–5. → **Route single-hop queries around the
  loop**; expect the win in rounds 1–2 on genuinely multi-hop questions; a sufficiency-style stop
  beats a fixed iteration count.
- **Graph retrieval routing (A3) — supported with the same caveat.** HippoRAG 2 (ICML 2025) shows
  graphs pay off on associative/multi-hop (+7 F1) while flat dense wins factoid lookups; GraphRAG
  evaluations (arXiv 2502.11371) show complementarity, not dominance. Route, don't force every query
  through the graph.

## 4. Track B — the projection-layer window

**The riskiest-looking part of the plan turned out to be the industry-standard part.** OpenAI
(Agents SDK `memory_summary.md` + `MEMORY.md` + rollout summaries; Codex CLI `~/.codex/memories/`),
Anthropic (Claude Code auto-memory `MEMORY.md` + topic files; client-side API memory tool), and
Google (Gemini CLI `GEMINI.md`; OKF) all independently converged on distilled-summary +
greppable-index + detail-files-on-demand in Feb–May 2026. B1 (handbook) and B2 (`.sibyl/memory/`)
are the same shape — projected from a graph instead of siloed per harness.

- **OKF v0.1 (Google Cloud, 2026-06-12)**: vendor-neutral Markdown + YAML frontmatter in
  git-shippable directories. Sibyl's OKF export now targets a Google-backed public spec. B2's
  materialized files should align with it where cheap.
- **The window is ~6–12 months.** AGENTS.md went launch → 60k repos → Linux Foundation stewardship
  in about a year; the learned-memory-layer contest opened Feb–May 2026 and no cross-tool convention
  has won. A repo-local `.sibyl/memory/` is greppable by every harness in the table with zero
  integration work — the only graph-backed entrant in the file conventions race. W3C AI Agent Memory
  Interop CG exists (June 2026) but is small and 12+ months from anything normative; MCP is
  explicitly not standardizing memory (2026 roadmap).
- **Platform-risk verdict**: the labs commoditized within-platform memory while actively
  manufacturing cross-platform fragmentation (three default-on silos learning the same lessons
  separately in one repo). "Your agent forgets" is dead as a pitch; the wedge is cross-agent + graph
  structure + scope/provenance + sovereignty. Nobody big has claimed the "memory sovereignty"
  phrase; the discourse (arXiv 2604.16548, OWASP) is arriving at it.

## 5. Track C — security tailwind

The June 2026 research wave handed Track C a public blueprint that matches what it already planned:
OWASP Agentic Top 10 formalized **ASI06 Memory & Context Poisoning** with provenance-per-write +
tenancy-separation guidance; TMA-NM (arXiv 2606.24322) demonstrates origin-bound write authority
with 0% attack success; incidents pile up on incumbents (Tenable HackedGPT memory injection, Radware
ZombieAgent, Claude Code MemoryTrap). No cross-tenant CVE exists against Mem0/Zep/Letta/Cognee, but
MemTrust (arXiv 2601.07004) explicitly flags their soft-label isolation as latent risk — the 1.1.3
scope closure puts Sibyl ahead of the disclosure curve, not behind it. Enterprise buyers now name
per-write provenance and verifiable deletion as purchase criteria; Sibyl's basis/provenance fields
and org-namespace isolation map directly onto what auditors test.

## 6. Coalescence window (the v1.3 deferral)

**Holds, and is narrowing measurably.** Team _scoping_ went from differentiator to table stakes
(Cognee dataset RBAC Oct 2025 → OpenAI shared projects / workspace agents Apr 2026), but the giants
chose **containment over gradation** (project-only walls; "per user and per agent … not a single
shared brain") — they validated demand and declined to build the hard thing. Within-scope
consolidation is commoditized (Mem0 ADD/UPDATE/DELETE, Graphiti entity resolution, AgentCore
strategies, Microsoft Memora); **cross-user merge under privacy constraints is shipped by zero
products**. Closest threats are papers, not products: MemClaw ("Governed Shared Memory," arXiv
2606.24535 — right vocabulary, agent-fleet focus, unknown adoption) and AgentPrizm (July 2026
launch, weak credibility signals).

Academic support for the deferral order: "Retain or Consolidate?" (arXiv 2607.17545) — retention
beats consolidation at loose token budgets; consolidation wins (up to +48%) only under tight ones.
Substrate-first is the measured order. **Two named tripwires**: (1) the crossover means
consolidation becomes load-bearing exactly when packs stop fitting budgets — watch that trigger
during A1; (2) interference research (arXiv 2605.08538, 2603.11768) says an unconsolidated store
eventually degrades retrieval _precision_, so the deferral is time-boxed, not indefinite. The
strategic risk is not someone shipping coalescence first — it is someone credible claiming the
"governed team memory" _narrative_ before Sibyl's version is demonstrable, which argues for keeping
the scope/provenance story visibly intact in every v1.2 surface.

## 7. Watch list

- **Cognee release cadence** (weekly ships, enterprise logos) — the OSS competitor to track.
- **LME-V2 leaderboard first entry** — if someone else lands a v2 number first, the first-mover
  payoff of `baseline-beat-gate` shrinks; the gate's numbers don't change.
- **Zep bolting ACLs onto Graphiti group graphs** — shortest path for a fast follower on governed
  team memory.
- **MemClaw / AgentPrizm adoption signals** — narrative-capture risk on "governed memory."
- **voyage-context-class embedders** — if the A1 offline arm confirms, the header strategy for the
  _vector_ side may be superseded within the release.
- **Learned memory policies (RL for write/retrieve/stop) and latent-space loops** — the research
  frontier moving past hand-designed architectures; nothing shippable yet.

## 8. Wave provenance

Six research lanes, run 2026-07-25, each with dated primary-source citations (GitHub repos, arXiv
abstracts, official docs fetched same-day). Full lane reports lived in session scratchpad; the
load-bearing claims and all primary sources are carried inline above. Key benchmark/paper anchors:
LME-V2 arXiv 2605.12493 + project leaderboard page · Penfield LoCoMo audit (2026-04-04) · MemDelta
2606.29914 · Fidelity-Before-Structure 2601.00821 · SeCom 2502.05589 · TriMem 2605.19952 ·
Dissecting Agentic RAG 2606.21553 · Retain-or- Consolidate 2607.17545 · MemClaw 2606.24535 · TMA-NM
2606.24322 · GroupMemBench 2605.14498 · OKF (Google Cloud, 2026-06-12) · MCP 2026 roadmap +
2026-07-28 spec RC · Anthropic contextual retrieval + context engineering posts · Chroma Context Rot
(2025-07).
