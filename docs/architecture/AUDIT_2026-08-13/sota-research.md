# Agent Memory Research SOTA — August 2026

Research-side survey (papers, benchmarks, empirical findings) for Sibyl 1.3 representation/retrieval decisions. Products are covered by a sibling report. Compiled 2026-08-13.

Reading key: [REALISTIC] = evaluated on realistic agent workloads (long trajectories, tool use, latency budgets). [CHAT] = long-conversation QA benchmarks (LoCoMo, LongMemEval-S/M). [TOY] = synthetic or narrow probes.

---

## 1. Representation research

### Temporal knowledge graphs: the Zep/Graphiti lineage

- **Zep: A Temporal Knowledge Graph Architecture for Agent Memory** (Rasmussen et al., arXiv:2501.13956) remains the canonical TKG-memory paper: bi-temporal edges (valid time + transaction time), episodic/entity/community node types. Graphiti crossed 20K GitHub stars in 2026. But its headline numbers have aged badly: on LongMemEval-V1, Zep scores 71.2% while a plain RAG pipeline with reranking hits 86% (Emergence AI, below). https://arxiv.org/abs/2501.13956
- **HippoRAG 2 — "From RAG to Memory: Non-Parametric Continual Learning for LLMs"** (Gutierrez et al., OSU, arXiv:2502.14802) is the strongest graph-retrieval result with a controlled story: dual-node KG (phrase nodes + passage nodes), Personalized PageRank seeded by embedding scores, LLM triple filtering. +7 F1 on associative (multi-hop) QA over SOTA dense retrievers. Critically, HippoRAG 2 names "context loss during indexing" as the fatal flaw of HippoRAG 1 — the graph works only because passage nodes keep verbatim text in the loop. [CHAT/multi-hop QA] https://arxiv.org/pdf/2502.14802
- Successors in the graph lane: **GAAMA: Graph Augmented Associative Memory** (arXiv:2603.27910), **Synapse** (arXiv:2601.02744, Feb 2026) which models memory as a dynamic graph where relevance emerges from *spreading activation* rather than pre-computed links, and **"Implicit Graph, Explicit Retrieval"** (arXiv:2601.03417) arguing the graph should be latent while retrieval stays cheap and interpretable. Direction of travel: away from expensive explicit triple extraction, toward graphs as a traversal/activation structure over preserved text.
- **Association Is Not Similarity** (arXiv:2604.20850) — learns corpus-specific association weights for multi-hop retrieval; relevant if we ever score edges for traversal.

### Typed vs untyped structure — what the controlled evidence actually says

Two different claims get conflated; the evidence splits cleanly:

- **Typed STORE separation (episodic/semantic/procedural routing) has strong evidence.** **ENGRAM: Effective, Lightweight Memory Orchestration for Conversational Agents** (arXiv:2511.12960, also OpenReview): routes conversation into three canonical memory types with a single router+retriever. LoCoMo LLM-judge 77.55%, beating Mem0, MemOS, LangMem, Zep. The ablation is the money shot: collapsing the three stores into one undifferentiated store drops to **46.56%** (−31 points). [CHAT] https://arxiv.org/abs/2511.12960
- **Memanto: Typed Semantic Memory with Information-Theoretic Retrieval** (Abtahi et al., arXiv:2604.22085, Apr 2026) extends this to 13 semantic categories (facts, preferences, decisions, commitments, goals, events, instructions, relationships, context, learning, observations, errors, artifacts), each with distinct retrieval semantics and priority weighting. LongMemEval 89.8%, LoCoMo 87.1%. No direct typed-vs-untyped ablation of its own; leans on ENGRAM's. [CHAT] https://arxiv.org/html/2604.22085v1
- **Typed EDGES/ontologies: evidence is thinner and conditional.** **Ontology Learning and KG Construction: A Comparison of Approaches and Their Impact on RAG Performance** (arXiv:2511.05991) is the best controlled comparison: vector RAG vs GraphRAG vs several ontology-grounded KGs, holding corpus/retrieval/eval constant. Ontology-grounded approaches win — but specifically the variants **that keep textual chunk information attached**; the win is interpretability + hallucination reduction, and DB-derived ontologies match text-extracted ones at far lower LLM cost. [CHAT/QA corpora] https://arxiv.org/abs/2511.05991
- **OntoKG** (arXiv:2604.02618) isolates type annotations on controlled mentions (same candidate entities both arms): type annotations, not candidate coverage, drive the accuracy difference. [TOY-leaning, entity-linking probe]
- **For code specifically**: **"Reliable Graph-RAG for Codebases: AST-Derived Graphs vs LLM-Extracted Knowledge Graphs"** (arXiv:2601.08773) — deterministic AST-derived structure beats LLM-extracted KGs for codebase QA. Structure you can compute exactly beats structure you hallucinate. [REALISTIC for coding]

Net: no paper shows that *typed relations on a memory graph* improve end-task accuracy over untyped edges in a controlled way. What is shown: typed top-level stores/categories with type-aware retrieval semantics help a lot; ontology structure helps when it augments rather than replaces text.

### Ontology-free / atomic-note approaches

- **A-Mem: Agentic Memory for LLM Agents** (Xu et al., arXiv:2502.12110; NeurIPS/OpenReview) — Zettelkasten-style atomic notes with LLM-generated contextual descriptions and links, memory evolution on write. Up to 2x GPT-4o-mini multi-hop on LoCoMo, 85–93% fewer memory-operation tokens than MemGPT. [CHAT] https://arxiv.org/pdf/2502.12110
- **AtomMem** (arXiv:2606.19847, Jun 2026) — "simple and effective memory via atomic facts"; the minimalist counterpoint arguing atomic facts + good retrieval match heavyweight systems. [CHAT]
- **Schema-constrained middle ground**: **"To Know is to Construct: Schema-Constrained Generation for Agent Memory"** (arXiv:2604.20117) and **"From Unstructured Recall to Schema-Grounded Memory: Reliable AI Memory via Iterative, Schema-Aware Extraction"** (arXiv:2604.27906) — extraction reliability improves when a schema constrains generation; the failure mode they target is exactly the lossy/hallucinated extraction that Fidelity-Before-Structure (Section 2) measures.

### Episodic vs semantic splits, consolidation, sleep-time compute

- **SYNAPSE** (arXiv:2601.02744) — episodic-semantic memory via spreading activation; minimal activated subsets beat strong baselines. **E-mem** (arXiv:2601.21714) — multi-agent episodic context reconstruction. **Episodic-Semantic Memory for Long-Horizon Scientific Agents** (arXiv:2605.17625) — retains semantic knowledge from old topics while discarding dated episodic detail. [CHAT/agent-sim]
- **Sleep-time compute lineage**: Letta's **"Sleep-time Compute: Beyond Inference Scaling at Test-time"** (arXiv:2504.13171, 2025) established the framing (pre-compute representations off the critical path). 2026 successors: **SCM: Sleep-Consolidated Memory** (arXiv:2604.20943) with multi-stage sleep cycles (consolidation, dreaming, intentional forgetting); **"Language Models Need Sleep: Learning to Self-Modify and Consolidate Memories"** (arXiv:2606.03979) — distill fragile short-term memory into stable long-term knowledge with replay; **TiMem: Temporal-Hierarchical Memory Consolidation** (arXiv:2601.02845) — 76.9–79.0% on LongMemEval-S depending on reader. [CHAT, some TOY]
- **"Sleep-Like Memory Consolidation in LLMs"** (arXiv:2605.26099) — periodic conversion of recent context into persistent fast weights; longer "sleep" decouples memory capacity from reasoning compute. [TOY + real benchmarks, weight-space so not directly applicable to an external store]

---

## 2. Storage-time vs retrieval-time intelligence

This is the most decisive cluster of 2026 results, and it cuts against expensive write-time structuring **when structure replaces raw text**:

- **"Diagnosing Retrieval vs. Utilization Bottlenecks in LLM Agent Memory"** (Yuan, Su, Yao; MemAgents Workshop @ ICLR 2026; arXiv:2603.02473). 3x3 factorial: write strategies {raw chunks, Mem0-style fact extraction, MemGPT-style summarization} x retrieval {cosine, BM25, hybrid+rerank} on LoCoMo. **Retrieval method spans 20 points (57.1%→77.2%); write strategy spans 3–8 points.** Raw chunks (zero LLM calls at write time) match or beat both lossy alternatives. Failures manifest at the retrieval stage, not utilization, in their decomposition. Conclusion verbatim: "improving retrieval quality yields larger gains than increasing write-time sophistication." [CHAT] https://arxiv.org/abs/2603.02473
- **"Fidelity Before Structure: Verbatim Chunks Beat Lossy Artifact Extraction in Long-Conversation LLM Memory"** (Tao An, arXiv:2601.00821v4, Jul 2026). Same retrieval pipeline, storage varied: LoCoMo chunks 43.9% vs artifacts 28.0% (−15.9); LongMemEval-S chunks 67.4% vs artifacts 45.4% (−22.0). Extracted artifacts fail to beat naive RAG at all (p=0.89). **Union storage (chunks + artifacts) recovers chunk-level accuracy (42.5%)** — the damage is *replacement*, not coexistence. Mechanism: accuracy tracks how much source text survives storage ("lossy distillation"). Six confound controls. Recommendation: "structure should augment verbatim text, not replace it." [CHAT] https://arxiv.org/html/2601.00821
- **Emergence AI, "SOTA on LongMemEval with RAG"** (blog, engineering result not peer-reviewed): 86% on LongMemEval-V1 (vs Zep 71.2%, vs Oracle GPT-4o 82.4%) with turn-level matching → session-level retrieval → cross-encoder rerank → CoT-before-answer. No query decomposition, no graph, no temporal module. Their own conclusion: "advanced memory architecture appears to be overkill for LongMemEval." [CHAT] https://www.emergence.ai/blog/sota-on-longmemeval-with-rag
- **Counterweight — write-time work that DOES pay**: ENGRAM/Memanto typed routing (cheap classification, not lossy rewriting); **"Beyond Static Summarization: Proactive Memory Extraction"** (arXiv:2601.04463); MemGuard write-time contamination filtering (Section 6); and Infini Memory's write-time recency rewriting for single-hop knowledge updates (Section 6). Pattern: cheap write-time *routing, tagging, verification, and superseding* pays; expensive write-time *rewriting that discards source text* does not.

### Long-context vs RAG as of mid-2026

- **"Long Context vs. RAG for LLMs: An Evaluation and Revisits"** (arXiv:2501.01880) and **LongBench v2** (arXiv:2412.15204): long context wins when the corpus fits and the model is frontier-class; RAG wins on cost and on mid-window content. Position sensitivity persists: relevant content buried mid-window still costs 30%+ accuracy in 2026 practice write-ups.
- **"Context Length Alone Hurts LLM Performance Despite Perfect Retrieval"** (arXiv:2510.05381, EMNLP 2025 Findings): even with perfect retrieval, sheer input length degrades answers — independent of distraction. This is a reader-side result with direct implications for pack sizing. [Controlled, semi-TOY]
- **ConvoMem** (Pakhomov, Nijkamp, Xiong — Salesforce; arXiv:2511.10523): "why your first 150 conversations don't need RAG" — full-context is competitive up to ~150 conversations; memory systems only earn their keep beyond that. Implication: memory-system evaluation must be at scale or it measures nothing. [CHAT]
- **BEAM** (Mem0's benchmark line, 1M and 10M token scales): designed to be unsaturated; Mem0 self-reports 64.1 (BEAM-1M) and 48.6 (BEAM-10M) vs 94.4 on LongMemEval-V1 — the headroom benchmark. Vendor-reported. https://mem0.ai/blog/ai-memory-benchmarks-in-2026
- 2026 consensus pattern (multiple industry write-ups): hybrid — retrieval to narrow, long window to reason across what survived. Nobody serious argues pure long-context for persistent multi-month memory.

---

## 3. Retrieval research: what's winning on the benchmarks

### LongMemEval-V1 (500 q, ~115K-token histories) [CHAT]

Saturating. Reported numbers (mix of peer-reviewed and vendor): OMEGA 95.4% (vendor), Mem0 94.4% (vendor), Memanto 89.8%, Emergence RAG 86%, MemForest 79.8% (30B model), TiMem 76.9–79.0%, Zep 71.2%. The spread between "simple RAG done well" (86%) and the top vendor claims (~90–95%) is within eval-protocol noise given LLM-judge variance; treat vendor deltas above ~85% as marketing precision. The Emergence result plus the bottleneck-diagnosis paper make the method-family story clear: **hybrid sparse+dense retrieval, session/document-scope expansion, cross-encoder reranking, and CoT-before-answer account for nearly all of the win**; graph traversal and heavyweight write pipelines account for little on this benchmark.

### LongMemEval-V2 (Wu, Ji, Kawatkar, Kwan, Gu, Peng, Chang; arXiv:2605.12493) [REALISTIC]

- 451 manually curated questions over agent trajectories (up to 115M-token haystacks, up to 500 trajectories/haystack); five abilities: static state recall, dynamic state tracking, workflow knowledge, environment gotchas, premise awareness. Scored on accuracy AND latency (LAFS: latency-accuracy frontier score). Naive RAG small-tier baseline: 42.8%. Readers pinned (Qwen3.5-9B reader, Qwen3-Embedding-8B).
- The paper's own failure decomposition splits errors into retrieval failures vs reader failures and concludes systems need advances in both — some methods win via retrieval, others via reading. No public leaderboard results table yet (repo confirms entries measure LAFS gain over the released baseline frontier; the board remains effectively open as of this week). https://github.com/xiaowu0162/LongMemEval-V2
- This is the one benchmark in the set that matches Sibyl's actual workload (coding-agent trajectories, latency budget). Everything scoring 90%+ above is scoring on V1-style chat QA, not this.

### LoCoMo — increasingly distrusted [CHAT, flawed]

- **Locomo-Plus** (arXiv:2602.10715) documents gold-answer errors (e.g., "Graphic Design" evidence keyed to a "Political science" gold), task-disclosed prompting artifacts, and string-matching metric distortion.
- Statistical power is weak: 10 conversations; LongMemEval-V1 has only 30 preference questions (per benchmark-methodology critiques). Simple filesystem operations hit 74% on LoCoMo, matching sophisticated systems — the benchmark barely separates trivial from intelligent memory.
- **MemoryAgentBench** (arXiv:2507.05257 lineage): four capabilities — accurate retrieval, test-time learning, long-range understanding, **selective forgetting**. No current system masters all four; most fail conspicuously on selective forgetting.
- **AMA-Bench** (arXiv:2602.22769) and **"Agent Memory: Characterization and System Implications of Stateful Long-Horizon Workloads"** (arXiv:2606.06448) push evaluation toward real agentic workloads (the latter is a systems-side characterization worth reading for storage design).

### Method families

- **Hybrid sparse+dense + rerank**: dominant, per Section 2. BM25 alone often beats cosine alone on conversational memory.
- **Graph traversal**: HippoRAG 2's PPR-over-phrase/passage-graph is the validated pattern; wins concentrated on multi-hop/associative questions (+7 F1), roughly neutral elsewhere.
- **Query decomposition**: notably absent from the winning LongMemEval pipelines; Emergence explicitly skipped it. Survives mainly in multi-hop QA literature.
- **Note-taking/distillation**: helps as an *additional* retrieval surface (union storage, trajectory notes — LME-V2's own rag+notes baseline), hurts as a replacement (Section 2).
- **Navigation as retrieval**: HORMA (Section 5) — agentic filesystem navigation instead of embedding search; also **"From Passive Retrieval to Active Memory Navigation"** (arXiv:2607.05794) and **"Beyond RAG for Agent Memory: Retrieval by Decoupling and Aggregation"** (arXiv:2602.02007). This family trades latency for temporal/causal grounding.

---

## 4. Reader conversion (evidence present, answer wrong)

The field now formally recognizes this as a separate loss bucket:

- **"Sufficient Context: A New Lens on RAG Systems"** (Joren et al., Google; arXiv:2411.06037, ICLR 2025) — the foundational decomposition. An autorater classifies whether context sufficed; frontier models (GPT-4o, Gemini 1.5 Pro, Claude 3.5) still answer wrong on a non-trivial fraction of *sufficient-context* instances, and hallucinate rather than abstain on insufficient ones. Conclusion: open-book QA cannot be solved by retrieval improvements alone. Their selective-generation trick (sufficiency signal + self-confidence gating) improves correct-when-answering by 2–10%. [Controlled, realistic corpora]
- **"Context Length Alone Hurts LLM Performance Despite Perfect Retrieval"** (arXiv:2510.05381) — length itself is a reader tax, no distractors needed. Directly supports tight packs over big packs at fixed recall.
- **ActMem** (Zhang, Sun, Yang, Jin, Zhang, Hu; arXiv:2603.00026, Jun 2026) — names the "reader-conversion gap" explicitly and couples retrieval with active reasoning over retrieved memories rather than one-shot stuffing. Evaluated on LongMemEval/BABILong-family tasks. [CHAT]
- **LongMemEval-V2's own error decomposition** (Section 3) institutionalizes retrieval-vs-reader failure attribution in the benchmark harness.
- **What measurably helps the reader**:
  - **CoT-before-answer**: part of Emergence's 86% pipeline; the cheapest validated lever.
  - **Two-stage read (extract facts → answer)**: Emergence's "Simple Fast" variant, 79% at 3.59s — a latency-friendly conversion booster.
  - **Citation forcing**: FRONT-style fine-grained grounded citations (arXiv:2408.04568) and cite-before-you-speak (arXiv:2503.04830) improve grounding ~13.8% in e-commerce agents; GopherCite lineage. **Caveat**: "Are Finer Citations Always Better?" (arXiv:2604.01432) shows over-fine citation granularity *fractures semantic dependencies* and degrades both retrieval and faithful generation — force citations at span/passage level, not sentence-shard level.
  - **Verifiable-reward training for groundedness** (arXiv:2506.15522) — post-training lever, not applicable to us directly but explains reader-model drift across versions.
- **Interference as a reader failure**: **"Transformers Remember First, Forget Last: Dual-Process Interference in LLMs"** (arXiv:2603.00270) and SleepGate (Section 6): stale associations in context degrade retrieval of current values **log-linearly** as they accumulate — packing superseded memory versions actively poisons the reader.

---

## 5. Tagging / categorization vs flat embeddings

No paper directly tests user-style tags vs flat embeddings. The adjacent evidence:

- **Typed-store routing** (ENGRAM, Section 1): the strongest categorization result — 3-way canonical typing with type-aware retrieval beats flat storage by 31 points on LoCoMo. This is category-as-retrieval-semantics, not category-as-label.
- **HORMA — "Organize then Retrieve: Hierarchical Memory Navigation for Efficient Agents"** (Hsu, Kuang, Liu, Yao, He; Duke/Snowflake; arXiv:2606.11680, Jun 2026): filesystem-like hierarchy (timestamped directories, synthesized notes linked to raw traces) navigated by an agent with ls/grep/cd/cat. Beats embedding retrieval on ALFWorld (56.7%→73.9% by context size), LoCoMo LLM-judge 51.6 vs 32.2 truncation at 3–22% of baseline tokens, LongMemEval 55.9 best-overall at 1.2–16% tokens. Wins concentrate on **temporal consistency and staleness avoidance** — exactly where similarity search fails. Cost: agentic navigation latency. [Mixed CHAT/agent-sim]
- **xMemory** (per survey arXiv:2602.06052) and **"Filesystem-Based Memory for LLM Agents"** (arXiv:2607.26637): hierarchy that disentangles episodic traces into semantic components, drill-down retrieval shrinking search space.
- The sober caveat, stated in the hierarchical-memory literature itself: **simple retrieval outperforms complex hierarchies on standard benchmarks**; adopt hierarchy only for retrieval failures flat search demonstrably can't solve (temporal ordering, causal chains, scope narrowing).
- Read across ENGRAM + HORMA + Memanto: categories earn accuracy only when they change retrieval behavior (routing, priority weighting, navigation paths, temporal scoping). Labels that are only filters at query time have no published accuracy evidence behind them.

## 6. What the research says matters that Sibyl likely ignores

1. **Selective forgetting / knowledge updates as a first-class operation.** MemoryAgentBench: the capability most systems conspicuously fail. **Infini Memory** (arXiv:2606.10677): single-hop forgetting is settled cheaply at *write time* via recency-override rewriting within a topic; multi-hop forgetting (chains across documents updated at different times) cannot be, and remains open. **TOKI** (arXiv:2606.06240) gives a bitemporal operator algebra for contradiction resolution — the formal version of what Graphiti's edge invalidation gestures at.
2. **Proactive interference.** SleepGate — **"Learning to Forget: Sleep-Inspired Memory Consolidation for Resolving Proactive Interference"** (arXiv:2603.14517): stale associations degrade retrieval log-linearly; conflict-aware temporal tagging + eviction/consolidation fixes it. A memory system that never supersedes gets *worse* with age, not just bigger.
3. **Write-time self-verification.** **MemGuard** (Ha, Kim, Qian, ..., Hakkani-Tur, Ji; arXiv:2605.28009): contamination (irrelevant/outdated/erroneous writes) accumulates and degrades long-horizon accuracy; verification gates at write time beat retrieval-time compensation. Evaluated on LongMemEval, PerLTQA, AgentBoard.
4. **Reconsolidation / memory rewriting on retrieval.** ACT-R-inspired architectures (HAI 2025, dl.acm.org/10.1145/3765766.3765803) and the sleep-cycle papers implement retrieval-triggered re-encoding; nothing in our loop touches a memory after it's written except manual edits.
5. **Procedural memory as a separate lane.** **ReasoningBank** (Google; arXiv:2509.25140): distills transferable reasoning strategies from both successes AND failures, self-judged without ground truth; **Memp** (procedural instructions + script abstractions with build/retrieve/update phases); **"Managing Procedural Memory in LLM Agents"** (arXiv:2606.23127); **"Compiling User Corrections into Runtime Enforcement for Coding Agents"** (arXiv:2606.13174) — user corrections as compiled enforcement rules, very close to our standing-grants/no-touch-zone capture. [REALISTIC — web/SWE agent workloads]
6. **Benchmark hygiene.** LoCoMo gold errors and 74%-via-filesystem; ConvoMem's finding that memory systems only differentiate past ~150 conversations; BEAM as the unsaturated headroom line. Chat-benchmark wins do not transfer claims to trajectory-scale workloads — LME-V2 is the only public benchmark in our shape.

---

## Cross-cutting synthesis for Sibyl 1.3

- Our own measurements (selection ceiling 82.6% vs best render 65.2%; reader-conversion loss bucket) reproduce the field's central 2026 finding: retrieval selection and reader conversion dominate; write-time representation sophistication is third.
- Passage projection is on the right side of the Fidelity-Before-Structure result *if* projected spans preserve verbatim source text and coexist with any distilled note (union, never replacement). The 42.5%-recovery number is the design rule.
- Mostly-untyped edges: no evidence we're leaving accuracy on the table with untyped *relations*. Type-aware *retrieval semantics* over our existing type enum (ENGRAM/Memanto pattern: per-type priority weighting, temporal scoping) is the evidenced upgrade, and it's a retrieval-side change, not a schema migration.
- Reader levers with receipts: CoT-before-answer, extract-then-answer two-stage, span-level citation forcing, sufficiency-gated abstention, and *not* packing superseded versions (interference is log-linear in stale entries).
- Biggest ignored-by-us lane: supersede/forget as a write-path operation with temporal tagging, plus write-time contamination gating.
