# SOTA: Agent Memory Systems & Products — 2026-08-13

Scope: products and open-source systems (papers are a sibling lane). Delta-focused against
`docs/architecture/SOTA_LANDSCAPE_2026-07-25.md`. Star counts and release dates below were pulled
live from the GitHub API on 2026-08-13 (verified); vendor benchmark numbers are labeled as claims.

## Executive verdict

The field's center of gravity in August 2026 is **typed epistemic categories + flat text payloads +
heavy multi-signal retrieval** — not ontologies and not writer-declared relation vocabularies. The
two systems that doubled down on rich typed graphs (cognee ontology grounding, Graphiti Pydantic
edge types) are the exception; the volume leader (Mem0) just **retreated** from graph memory
entirely, removing its queryable `relations` interface in favor of entity-linking that only
influences ranking. Meanwhile a new lane opened that is exactly Sibyl's lane: **team-level memory
hubs for coding agents** (TencentDB Agent Memory v2.0, Aug 3; Hindsight's `hindsight-coding-agents`
harness plugin, v0.9.0, Aug 7). The LongMemEval-V2 leaderboard is **still empty** ("Leaderboard
entries coming soon" on both tiers, verified on the project page today) — the first-mover slot
remains open, 3 weeks after our July read said the same.

---

## 1. Landscape delta since 2026-07-25 (3 weeks)

- **TencentDB Agent Memory v2.0** (2026-08-03, verified release tag): the biggest new entrant, and
  the closest thing to a direct Sibyl competitor to appear all year. Team-level memory hub for AI
  coding agents; 21,170 stars 90 days after open-sourcing (verified; GitHub license field reads
  NOASSERTION while press says MIT — unverified licensing). Four memory asset types: Chat Memory
  (L0→L3 layered distillation to Core/Persona), Skill (versioned reusable procedures), LLM-Wiki
  (docs → structured pages with link graphs), Code-Graph (symbols/calls/impact paths). Retrieval:
  BM25 + vector + RRF under strict token/character budgets. Governance: private/team/restricted/
  agent-targeted visibility, "private is owner-only, not readable even by team admins," explicit
  sharing. Self-reported PersonaMem 48%→76%; no independent reproduction. Repo:
  github.com/TencentCloud/TencentDB-Agent-Memory. **This is our scope ladder, our budgeted packs,
  and our team story, shipped by Tencent with a 20k-star megaphone.** What it does NOT have: a
  knowledge graph, provenance-per-write semantics, or (apparently) an LLM-free write path.
- **Hindsight went after coding agents.** v0.9.0 (2026-08-07, verified release notes): a
  `hindsight-coding-agents` package — "harness-pluggable long-term memory for coding agents" — plus
  transcript readers for Copilot CLI and Devin, OpenClaw plugins, Obsidian vault ingestion,
  client-managed Knowledge Pages, and "Mental Models" (consolidated per-scope syntheses with dry-run
  refresh). 19,898 stars (verified). Release cadence is ~weekly.
- **Mem0 published its "State of AI Agent Memory 2026" report today (2026-08-13)** and it contains a
  load-bearing architecture confession: Mem0 moved from external graph stores (Neo4j) to "built-in
  entity linking" — the `relations` field is **removed**, there is "no longer a queryable graph
  interface," entities influence retrieval ranking but cannot be traversed. Their own words: for
  teams needing traversable graphs this is "a regression." (Fetched from
  mem0.ai/blog/state-of-ai-agent-memory-2026.)
- **Elastic entered** (missed by the July sweep): Atlas agent memory, open-sourced 2026-06-30
  (InfoQ). Cognitive-science triad — episodic / semantic / procedural memories in separate
  Elasticsearch indices with distinct lifecycles; episodic decays, LLM consolidation promotes
  episodes into semantic facts. Hybrid BM25 + Jina v5 embeddings + RRF + cross-encoder rerank.
  Reported 0.89 Recall@10 on their own QA eval (self-reported). Repo not at elastic/atlas (404);
  exact location unverified.
- **Cursor killed its Memories feature.** Auto-extracted per-project memories (introduced mid-2025)
  were removed around v2.1.x in late 2025; users told to export memories and convert to Rules
  (static, human-maintained text). A big-IDE data point AGAINST auto-extracted memory and FOR
  curated flat files. (Multiple secondary sources; not verified against Cursor's own changelog.)
- **LME-V2 leaderboard: still zero entries** (verified today on xiaowu0162.github.io/longmemeval-v2
  — "Leaderboard entries coming soon," both tiers). Reference frontier unchanged (Small: RAG 51.0%
  @0.2s → AgentRunbook-C 74.9% @108.3s).
- Star deltas since 2026-07-25 (verified): Mem0 61.7k→63.2k, cognee 29.3k→30.0k, Graphiti
  29.2k→29.9k, Supermemory →28.9k, Hindsight 18.8k→19.9k, MemOS →10.7k, EverOS 11.5k→12.0k,
  MemMachine →3.3k, OpenMemory →4.4k (repo banner: "currently being rewritten"). Letta core 24.2k
  but last push Aug 1 (legacy-mode confirmed); Letta Code 2,996 and active daily.

---

## 2. System profiles

### Zep / Graphiti — the typed-relations flagship

- **Representation** (verified from help.getzep.com custom-entity-and-edge-types doc): temporal
  knowledge graph. Facts are edges with `valid_at`/`invalid_at` bitemporal handling (invalidation
  preserves history + provenance). Custom entity AND edge types are **Pydantic models with typed
  attributes** (e.g. an `Employment` edge carrying `position`, `start_date`, `salary`,
  `is_current`). An `edge_type_map` constrains which edge types may form between entity-type pairs,
  e.g. `("Person","Company"): ["Employment"]`. Untyped pairs fall back to **generic RELATES_TO** —
  the default graph is exactly Sibyl's shape; typing is opt-in schema work by the _developer_, not
  the writing agent. Extraction pipeline: LLM detects → classifies against the map → populates
  attributes → Pydantic validates.
- **Tagging**: no user-facing tag system; typing IS the categorization. Group graphs for scoping.
- **Retrieval**: hybrid vector + BM25 + graph traversal, single ranked answer, no LLM rerank; Zep
  claims P95 300ms. `SearchFilters` accept `node_labels=[...]` and `edge_types=[...]` — typed
  filters are a real retrieval feature, the strongest structure-pays-off mechanism in the field.
- **Positioning**: Zep is now fully enterprise "Context Lake" (SOC 2 Type II, ABAC, retention/legal
  hold, BYOC; customers AWS, Samsung, Writer). Graphiti stays Apache-2.0 engine-only (29.9k stars,
  v0.29.3 2026-07-27, FalkorDB backend work; releases verified). Self-host product door remains
  closed.
- **Benchmarks**: old LongMemEval-v1 claims (71.2–90.2 across eras); nothing new since July.

### Mem0 — volume leader, retreating from structure

- **Representation** (their Aug 13 report, fetched): extracted **facts** as flat text, single-pass
  ADD-only extraction at `add()` time; agent-generated facts now first-class alongside user facts.
  Graph memory: **removed as a queryable surface** (see delta above). Temporal: admits "temporal
  abstraction at scale" is a weakness (25% drop from BEAM-1M to BEAM-10M).
- **Tagging** (verified from docs.mem0.ai custom-categories): 15 default categories
  (`personal_details`, `food`, `travel`, ... `misc`), replaceable with custom lists; auto-classified
  asynchronously post-ingestion. Docs show no filter-by-category retrieval — categories are for
  reporting/organization. **Vestigial for retrieval.**
- **Retrieval**: multi-signal — semantic + BM25 + entity matching; entity links boost ranking only.
- **Numbers** (self-published, Aug 13 report): LoCoMo 92.5 @~6,956 tok/query, LongMemEval-v1 94.4
  @~6,787, BEAM-1M 64.1, BEAM-10M 48.6. Admitted open problems: identity resolution, staleness,
  privacy/consent architecture "still undefined," no app-level eval framework.
- **Releases**: v2.0.18 (2026-08-11, verified). 63.2k stars.

### cognee — the ontology camp

- **Representation** (verified via docs + search): semantic graph of **Pydantic DataPoint** typed
  nodes; optional **OWL ontology grounding** (RDF/XML): entity types fuzzy-matched to OWL classes
  (80% cutoff), individuals matched, names canonicalized to URI-derived forms (killing
  cross-document duplicates), then BFS injects `rdfs:subClassOf` hierarchies and
  `owl:ObjectProperty` edges into the graph; every node tagged `ontology_valid` true/false. This is
  the deepest writer/developer-declared structure in the field.
- **Retrieval**: graph-completion retrievers (GRAPH_COMPLETION_COT), tunable graph construction.
- **Numbers** (self-published): BEAM-100k 79% vs prior SOTA 73.4%; BEAM-10M 67% vs Hindsight's
  64.1%, flat token usage. Jan 2026 head-to-head (24 HotPotQA q, 45 runs): cognee 0.85 DeepEval
  correctness vs Graphiti 0.74, LightRAG 0.67, Mem0 0.54 — their claim: "structured recall beats
  chunk retrieval" on multi-hop. Tiny n, self-run.
- **Cadence**: v1.4.2 (2026-08-08, verified), near-weekly. 30.0k stars, passed Graphiti this month.

### Hindsight (Vectorize) — typed epistemic categories, benchmark-marketing crown

- **Representation** (verified via docs/search + arXiv 2512.12818): memory **banks** organized into
  four networks by epistemic role — **World** (objective facts), **Experience** (first-person agent
  biography), **Opinion** (subjective judgments with confidence scores + formation timestamps),
  **Observation** (preference-neutral entity summaries consolidated from the other networks). Facts
  are structured text with entity resolution (pg_trgm fuzzy matching, configurable threshold) and
  temporal extraction — NOT triples, NOT a relation vocabulary. Notably v0.9.0 **removed** the
  "deprecated entity schema from memory_links" — they simplified link structure. Banks carry
  mission/directives/disposition traits that steer Reflect.
- **Tagging**: a tags column exists (referenced in v0.9.0 changelog) but consolidation was just
  fixed to key on resolved scope "not the tags column" — tags look secondary; scope is load-bearing.
- **Retrieval**: Recall = parallel semantic + BM25 + **graph traversal** + temporal strategies,
  cross-encoder rerank (now with reranker failover chains); fact-type filters on recall (invalid
  types 422). Reflect = agentic reasoning over recalled memories.
- **Numbers**: LongMemEval-v1 94.6% "independently reproduced by Virginia Tech's Sanghani Center and
  The Washington Post" (their claim; reproduction reports not fetched); BEAM-10M 64.1% #1 claim from
  April. New blog "How to Actually Evaluate an Agent-Memory System" — they're playing the integrity
  discourse too.
- **Direction**: hard pivot into coding-agent memory (see delta). Postgres-native, MIT, 19.9k stars.

### Letta / Letta Code — memory blocks + git-backed files

- **Representation**: memory **blocks** — labeled, always-in-context, agent-editable sections
  (structured prompt real estate, not a datastore); Letta Code adds git-tracked context rewriting,
  markdown **skill files**, and `/init`, `/remember` learning commands. New `ai-memory-sdk`
  (verified repo, 45 stars, Apache-2.0): Subjects → Blocks → Messages, a "subconscious agent"
  updates blocks asynchronously; archival memory (vector) optional behind it.
- **Tagging**: block labels are the taxonomy; agent-defined, load-bearing (the agent routes writes
  by label).
- **Retrieval**: mostly no retrieval — blocks are pinned; archival search (vector/full-text/hybrid)
  for overflow.
- **Positioning**: flagship repo confirmed legacy (last push Aug 1); Letta Code claims #1
  model-agnostic OSS coding harness on Terminal-Bench (their claim). They sell the harness now, not
  the memory layer.

### MemOS (MemTensor) — "memory OS," Chinese ecosystem

- **Representation** (verified README): MemOS 2.0 "Stardust"; graph-structured unified memory API
  ("inspectable and editable by design, not a black-box embedding store"), multi-modal (text,
  images, tool traces, personas), **MemCube** composable knowledge bases, layered self-evolving
  memory (L1 traces → L2 policies → L3 world models → crystallized Skills). Neo4j + Qdrant self-host
  stack.
- **Numbers** (self-published via their own OmniMemEval harness, 14 commercial products, 10
  datasets): LoCoMo 88.83, LongMemEval-v1 89.20, BEAM-10M 56.75, PersonaMem-v2 40.58; OpenClaw task
  completion 36.63%→50.87%. 10.7k stars, Apache-2.0, active daily.

### Elastic Atlas — cognitive triad, new entrant

Covered in delta. The notable part is WHO: Elastic productizing episodic/semantic/procedural memory
over Elasticsearch legitimizes the typed-category (not typed-relation) representation for enterprise
buyers.

### EverMind EverOS — Markdown as source of truth

- **Representation** (verified README): canonical `.md` files (readable, diffable, git-versioned)
  - synced SQLite + LanceDB indexes; separate user tracks (episodes/profile) and agent tracks
    (cases/skills); an editable "Knowledge Wiki" with taxonomy. Orthogonal retrieval keyed by
    user/agent/app/project/session ids. 12.0k stars, Apache-2.0. Claims LoCoMo 93.05% /
    LongMemEval-S 83.00% (self-published; their blog also runs a rankings content mill — treat as
    marketing).
- The star/commit anomaly flagged in July persists as a credibility question, but the repo is active
  daily.

### Supermemory — consumer/API memory with an internal graph

- **Representation** (their docs/blog): documents → Chunks + derived Memories + Profile; "Memory
  Graph" on a custom vector-graph engine with "ontology-aware edges," contradiction resolution,
  temporal reasoning — all system-extracted, zero writer declaration. "Dreaming" ingestion param
  (May 2026) batches related docs so memories form from coherent units. Sub-300ms recall claim.
  28.9k stars, MIT. Marketing-heavy; internals unverified.

### Smaller/other OSS

- **MemMachine** (3.3k stars): arXiv 2604.04853, "ground-truth-preserving" memory; episodic +
  profile; modest traction.
- **CaviraOSS OpenMemory** (4.4k stars, verified README): "cognitive memory engine," flat add/search
  API, explainable recall traces; **entire project mid-rewrite** on a branch — unstable.
- **LangMem** (1.6k stars, v0.0.30 still latest from Oct 2025): alive but frozen; it's a thin
  extraction layer over LangGraph's BaseStore (flat namespaced JSON docs + semantic search).
  LangChain's answer to memory is the store primitive, not a memory product.

### Platform-native (the silos)

- **Anthropic**: Managed Agents memory public beta (2026-04-23; `agent-memory-2026-07-22` header for
  memory-store endpoints — a July revision): memories are **files** in a managed store,
  export/edit/delete via API + Console, full audit trails. Claude Code auto-memory (on by default
  since v2.1.59): `~/.claude/projects/<proj>/memory/` with MEMORY.md index (first 200 lines/25KB
  auto-loaded) + topic files on demand. Flat markdown, agent-written, greppable. Not synced across
  machines.
- **OpenAI**: ChatGPT "Dreaming" GA'd broadly (announced 2026-06-04): background consolidation,
  automatic capture, **self-updating temporal rewrites** ("you are going to Singapore" → "you went
  to Singapore in July 2026") — the most user-visible temporal-validity feature anywhere. Agents SDK
  evolution continues (sandboxed long-horizon agents); memory primitives remain per-platform
  files/summaries.
- **Cursor**: Memories feature REMOVED (~v2.1.x); Rules (static curated text) are the only native
  persistence. **Windsurf**: Cascade Memories persist snippets across sessions but contextual
  understanding still resets; third-party MCP memory add-ons (agentmemory, MemNexus, Hindsight's
  coding-agents package) are filling the gap — an aftermarket that proves native IDE memory is
  underdelivering.
- **Copilot Memory**: default-on since Mar 2026 (no notable August change found).

---

## 3. Benchmark state (products lens)

- **LME-V2: zero entries, both tiers, verified today.** Every vendor still markets LongMemEval-v1
  and LoCoMo numbers despite v1 saturation (94.x claims from Mem0 AND Hindsight simultaneously) and
  the Penfield LoCoMo audit. Nobody has touched the agentic-tier benchmark publicly. Sibyl's ~30.4%
  (new 3-pass anchor) against RAG-51.0/AgentRunbook-74.9 frontier remains unflattering in absolute
  terms but would still be the FIRST leaderboard number in existence.
- **BEAM is the new marketing battleground** (10M-token tier): Hindsight 64.1 → cognee claims 67 →
  MemOS 56.75 → Mem0 admits 48.6. All self-run.
- **OmniMemEval** (MemTensor) is a new cross-product harness (14 products, 10 datasets) — worth
  watching as a GLUE-style shared harness candidate, though vendor-owned.
- **PersonaMem** shows up twice (Tencent 76%, MemOS 40.58 on v2) — becoming the personalization
  yardstick.

## 4. Tagging/categorization across the field

- **Load-bearing**: Letta block labels (routing), Hindsight banks + four networks (epistemic routing
  at write AND recall filter), Elastic Atlas memory types (separate indices + lifecycles), TencentDB
  asset types (four products essentially), Graphiti node_labels/edge_types (retrieval filters).
- **Vestigial**: Mem0 categories (auto-assigned, reporting-only, no retrieval filter documented),
  Hindsight's literal tags column (consolidation just moved OFF it onto scope).
- Pattern: **coarse enumerated type systems (4–15 values) with retrieval consequences win; freeform
  tags without retrieval consequences rot.** Sibyl's entity-type enum + scope is the winning shape;
  our freeform tags match the field's vestigial tier unless they gate retrieval.

## 5. Expressiveness verdict

Is richer writer-side expressiveness paying off? **Mostly no — with one precise exception.**

- The **retreats**: Mem0 removed its queryable graph (the single loudest data point — the largest
  system by adoption measured graph maintenance cost against ranking benefit and kept only entity
  links). Hindsight removed entity schema from memory_links. Cursor removed auto-memories in favor
  of flat rules. Letta left the memory-layer market for pinned flat blocks + markdown skills.
- The **holds**: Graphiti's typed edges survive because they're _developer-declared schema_
  (Pydantic, per-deployment) validated at extraction — not agent-declared at write time — and
  because they cash out as retrieval filters. cognee's OWL grounding survives for the same reason:
  the ontology is an input artifact that canonicalizes entities (dedup) and injects hierarchy;
  agents never author triples.
- **Nobody ships agent-authored typed relations.** In every surveyed system the writer (agent) emits
  text; structure is either extracted by LLM pipeline (Graphiti, cognee, Mem0, Hindsight,
  Supermemory) or declared by the developer as schema/config (Graphiti Pydantic, cognee OWL, Letta
  block labels, TencentDB asset types). Sibyl's writer-declared retrieval keys are already more
  writer-side structure than anyone else's product asks for.
- What actually correlates with benchmark wins in 2026: (a) multi-signal retrieval with rerank, (b)
  coarse epistemic typing that routes writes and filters recalls, (c) consolidation/ observation
  layers (Hindsight Observations/Mental Models, ChatGPT Dreaming, Supermemory dreaming, TencentDB
  L0→L3), (d) temporal validity as _system-maintained fact rewriting or invalidation_ (Zep
  invalidation, ChatGPT self-updating memories) — never as writer-declared valid-time.
- **Implication for Sibyl's representation question**: typed relations and ontologies are NOT the
  gap behind our 30% vs 42.8% — the frontier baseline we're chasing is flat RAG over slices+notes.
  The evidence supports investing in (b)+(c)+(d) — type-aware retrieval filters over the enum we
  already have, a consolidation/observation layer, and system-side fact invalidation — before any
  relation-vocabulary expansion. If we ever type edges, the Graphiti pattern (developer schema +
  extraction-time validation + retrieval filters) is the only one with production proof, and its
  default fallback is literally RELATES_TO.

## 6. Differentiation map

**Crowded** (do not build a pitch here): "your agent forgets" (dead), LongMemEval-v1/LoCoMo numbers
(saturated + discredited), personal-assistant fact memory (Mem0/Supermemory/platform silos),
memory-OS framing (MemOS, MemMachine, EverOS all claim it), markdown-files memory (EverOS + every
platform native).

**Newly contested** (was open in July, now has entrants — move fast or reframe):

- _Team memory for coding agents_: TencentDB v2.0 + Hindsight coding-agents. Tencent has governance
  visibility levels but no graph, no provenance semantics, no LLM-free write path; Hindsight has no
  team/scope privacy ladder. Sibyl's combination is still unique but the phrase "team-level memory
  hub for AI coding agents" is now Tencent's headline.
- _Consolidation with receipts_: Dreaming (OpenAI), Mental Models (Hindsight), dreaming param
  (Supermemory). The v1.3 dream-cycle lane has three brand-name competitors for the narrative.

**Still open** (verified by absence across every system surveyed):

- LME-V2 leaderboard first entry (empty today).
- Schema-level memory-scope privacy ladder with grant semantics (Tencent's visibility levels are the
  closest anyone has come; still no private→team→org gradation with grants).
- Deterministic/LLM-free write path (still 100% of surveyed systems put an LLM in the write path).
- Cross-user coalescence under privacy constraints (still shipped by nobody).
- Honest multi-operating-point accuracy+latency reporting (Mem0's report gestures at tokens/query;
  nobody publishes frontiers).
- Cross-harness memory as a _graph-backed_ file convention (EverOS is files-first without a graph;
  we're still the only graph-projected entrant).

## 7. Watch list (revised)

1. **TencentDB Agent Memory** — release cadence and whether Code-Graph/Skill assets grow real
   retrieval semantics; it owns the coding-team-memory narrative megaphone right now.
2. **Hindsight coding-agents package** — harness-pluggable memory for Claude Code/Copilot/Devin is
   an adoption wedge aimed at exactly our users.
3. **LME-V2 leaderboard** — still empty; every week it stays empty, the first-entry payoff holds.
4. **cognee BEAM claims** — if independently reproduced, the ontology camp gets its first
   third-party win and the "structure wins multi-hop" story gets teeth.
5. **Mem0's entity-linking architecture** — if their numbers hold without a graph, that's the
   strongest public evidence for retrieval-over-representation; if temporal/multi-hop regresses in
   the wild, the pendulum swings back.
6. **OmniMemEval** — vendor-owned but the only live cross-product harness; check whether neutral
   parties adopt it.

## Sources (primary, fetched 2026-08-13)

- GitHub API: star counts/pushed dates/licenses for all 14 repos listed; release feeds for graphiti
  (v0.29.3), mem0 (v2.0.18), cognee (v1.4.2), hindsight (v0.9.0 full release notes),
  TencentDB-Agent-Memory (v2.0.0); READMEs for MemOS, EverOS, OpenMemory, letta-ai/ai-memory-sdk.
- help.getzep.com/graphiti/core-concepts/custom-entity-and-edge-types (typed edges mechanics).
- mem0.ai/blog/state-of-ai-agent-memory-2026 (pub 2026-08-13); docs.mem0.ai custom-categories.
- xiaowu0162.github.io/longmemeval-v2 + leaderboard/README.md (empty leaderboard + frontier).
- cognee.ai/blog/deep-dives/knowledge-graph-memory-benchmarks; docs.cognee.ai ontology guides.
- hindsight.vectorize.io comparison guide + developer docs (via search excerpts); arXiv 2512.12818.
- marktechpost.com 2026-08-07 TencentDB v2.0; Manila Times/AAP newswire 2026-08-13 (20k stars).
- infoq.com/news/2026/06/elastic-atlas-agent-memory.
- letta.com/blog/letta-code (2025-12-16); letta.com/blog/our-next-phase.
- openai.com/index/chatgpt-memory-dreaming (2026-06-04); platform.claude.com managed-agents/memory
  (+ agent-memory-2026-07-22 header); Claude Code auto-memory coverage (secondary).
- Cursor Memories removal: dredyson.com + skillwright/memnexus coverage (secondary — not verified
  against Cursor changelog).
