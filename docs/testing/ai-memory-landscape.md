---
title: AI Memory Landscape
description: Honest competitive positioning for Sibyl in the June 2026 AI memory systems field
---

# AI Memory Landscape

This page positions Sibyl against the June 2026 AI memory systems field. The headline result
([500/500 hit@5, 96.96% strict R@5, 98.90% R@10 on LongMemEval-S](./longmemeval.md)) is one number
in a noisy field. The point of this page is to make the comparison legible without overclaim and
without burying real competitive strengths.

The single most important framing for the rest of this page comes first.

## The Apples-and-Oranges Problem

"LongMemEval score" is two different numbers depending on which lane the publisher is running in.
Most public comparison tables conflate them.

**Retrieval recall (the axis Sibyl reports).** Did the retriever surface the right answer
session(s)? Measured as `Recall@K` and `NDCG@K` directly against the gold `answer_session_ids` in
the dataset. No model generation, no LLM judge.

**End-to-end QA accuracy (a different axis).** Did the full system answer the question correctly?
Measured by retrieving sessions, generating an answer with a strong reader model, then grading that
answer with GPT-4o as judge using the per-question rubric from the
[original LongMemEval paper](https://arxiv.org/abs/2410.10813). Combines retrieval + reading +
generation + judging into a single percentage.

The retrieval number is **strictly easier** than the QA accuracy number on the same dataset because
finding the right sessions is necessary but not sufficient to answer correctly. Reading the
sessions, reasoning across them, producing a judged-correct answer all add failure modes that
retrieval alone does not have. Vectorize's
[Agent Memory Benchmark Manifesto](https://hindsight.vectorize.io/blog/2026/03/23/agent-memory-benchmark)
and rohitg00's
[LONGMEMEVAL.md analysis](https://github.com/rohitg00/agentmemory/blob/main/benchmark/LONGMEMEVAL.md)
both call out this conflation as the leading source of leaderboard noise.

There is a second distinction inside the retrieval lane:

- **`recall_any@K`** (sometimes called "hit@K"): did _any_ gold session appear in the top K?
- **`recall_all@K`** (strict R@K): for multi-answer questions, did the retriever surface _every_
  gold session? Many LongMemEval-S questions have multiple correct sessions (250 of 500 have exactly
  2, 41 have 3, the rest more). `recall_any` is strictly easier than `recall_all`.

Sibyl reports both: `hit@5 = 100%` (the easier metric, equivalent to `recall_any@5`) and
`recall@5 = 96.96%` (the strict multi-answer metric). When MemPalace and agentmemory report "R@5",
they generally mean `recall_any@5`.

## Where Sibyl Sits

Sibyl's defensible position is the intersection of six properties. No competitor hits all six.

| Property                                                        | Sibyl | Notes                                                        |
| --------------------------------------------------------------- | :---: | ------------------------------------------------------------ |
| Self-hosted, open source                                        |   ✓   | Apache-2.0. No mandatory cloud.                              |
| Graph-native runtime (graph + vector + full-text + traversal)   |   ✓   | SurrealDB unified.                                           |
| Physical tenant isolation (namespace-per-org, not filter-based) |   ✓   | SurrealDB namespace boundary.                                |
| Source-preserving memory records                                |   ✓   | Session entities keep original content.                      |
| Live API path benchmarking with reproducible CI artifacts       |   ✓   | The full eval runs against `POST /api/search`.               |
| No LLM in the retrieval or extraction path (for the benchmark)  |   ✓   | OpenAI embeddings used; no LLM extraction, no LLM reranking. |

Going around the field on those six dimensions:

- [**Cognee**](https://www.cognee.ai/) gets closest. Self-hosted, graph-native, tenant isolation via
  its permission model (Users/Tenants/Roles plus dataset ACLs, toggled on via the Enable Backend
  Access Control (EBAC) env flag, with physical isolation only on the Kuzu/LanceDB/FalkorDB
  backends), live benchmarking. Trails on LLM-free retrieval: extraction is LLM-driven.
- [**Graphiti**](https://github.com/getzep/graphiti) (Zep's underlying engine) is the closest
  architectural sibling. Zep itself
  [deprecated self-hosted Community Edition](https://vectorize.io/articles/zep-alternatives) in
  April 2025; Graphiti the library still runs, but you bring your own DB, your own multi-tenancy,
  and your own LLM extraction.
- [**Memweave**](https://github.com/sachinsharma9780/memweave) hits source-preserving, no-LLM, live,
  and self-hosted. Trails on graph-native and multi-tenant: it's single-process file-on-disk.
- [**Mastra**](https://mastra.ai/) Observational Memory hits self-hosted and live benchmarking but
  runs two LLMs (Observer + Reflector) continuously to compress conversations into observations. Not
  source-preserving by design.
- [**Mem0**](https://mem0.ai/) hits live benchmarking on a hosted product. Trails on graph-native
  (vector-first with entity linking), source-preserving (single-pass LLM extraction is the default),
  and LLM-free retrieval.
- [**Letta**](https://www.letta.com/) hits self-hosted but is a stateful-agent runtime, not a memory
  substrate; has not published LongMemEval numbers.

The combination is the position. No single property is unique.

## Retrieval-Axis Comparison (Sibyl's Lane)

These are systems publishing retrieval-layer numbers on LongMemEval-S. Apples-to-apples or close to
it.

| System           | Headline                        | Metric type  | Strict multi-answer | LLM in retrieval | Live API | Tenant isolation |
| ---------------- | ------------------------------- | ------------ | :-----------------: | :--------------: | :------: | :--------------: |
| **Sibyl**        | 96.96% R@5, 98.90% R@10         | strict R@K   |          ✓          |        ✗         |    ✓     |        ✓         |
| MemPalace raw    | 96.6% R@5                       | recall_any@K |          ✗          |        ✗         |    ✗     |        ✗         |
| MemPalace hybrid | 100% R@5 (full), 98.4% held-out | recall_any@K |          ✗          |   yes (Haiku)    |    ✗     |        ✗         |
| Memweave         | 98.0% R@5, 99.11% R@10          | recall_any@K |       unclear       |        ✗         |    ✗     |    filesystem    |
| agentmemory      | 95.2% R@5, 98.6% R@10           | recall_any@K |          ✗          |        ✗         |    ✗     |        ✗         |

A few honest readings of this table:

[**Memweave**](https://github.com/sachinsharma9780/memweave) is the cleanest direct competitor on
retrieval quality. Its 98.0% R@5 / 99.11% R@10 on a 450-question held-out split is real, well
documented, cross-validated (±0.12% std dev), and methodologically transparent. The held-out split
excludes 50 questions used for tuning, and the metric is `recall_any` rather than strict
`recall_all`, so the comparison is not perfectly apples-to-apples. Still, Memweave is the system to
point at when someone asks "is anyone close to Sibyl on this axis?". Its real edge is brutal
simplicity: plain Markdown source files, SQLite + sqlite-vec + FTS5 index, zero infrastructure,
graceful degradation. For a single developer on a laptop, Memweave is a defensible choice.

[**MemPalace**](https://github.com/MemPalace/mempalace) had the loudest 2026 launch and the public
methodology hasn't held up under independent review. The 96.6% raw number is
[a ChromaDB + `all-MiniLM-L6-v2` baseline](https://github.com/MemPalace/mempalace/issues/214); the
palace architecture is not actually exercised in the benchmark, and turning the palace features on
_reduces_ recall (89.4% with rooms, 84.2% with AAAK compression). The 100% hybrid result was overfit
by iteratively patching failing questions until they passed
([Vectorize critique](https://vectorize.io/articles/mempalace-benchmarks),
[MemPalace #875](https://github.com/MemPalace/mempalace/issues/875)). The honest MemPalace number is
the 98.4% held-out result. Even that is `recall_any@5` over a single-tenant local benchmark, not a
strict-multi-answer live-API run.

[**agentmemory**](https://github.com/rohitg00/agentmemory) by rohitg00 is the small, clean reference
point. BM25 + `all-MiniLM-L6-v2` hybrid, no LLM in the loop, explicitly flags the `recall_any` vs
strict distinction in its own README. Less prominent than MemPalace but its numbers are believable
and its methodology disclosure is exemplary.

## QA-Accuracy Comparison (Different Lane)

These systems report end-to-end QA accuracy on LongMemEval-S with an LLM judge. Sibyl does not
currently publish a citable number on this axis. Listing them here keeps the contrast explicit and
keeps the comparison from being misread.

| System                 |   Headline | Reader model   | Judge          | LLM extraction | LLM reranker |
| ---------------------- | ---------: | -------------- | -------------- | :------------: | :----------: |
| OMEGA                  |      95.4% | GPT-4.1        | likely GPT-4o  |     likely     |      ✓       |
| Mastra OM              |     94.87% | GPT-5-mini     | GPT-4o         |       ✓        |      —       |
| Mem0 (Apr 2026 algo)   |      94.4% | (managed)      | GPT-4o         |       ✓        |      ✓       |
| Hindsight (Vectorize)  |      91.4% | Gemini 3 Pro   | GPT-4o         |       ✓        |      ✓       |
| Memoria (MatrixOrigin) |     88.78% | GPT-5.4        | GPT-5.4        |   not stated   |  not stated  |
| ByteRover              |      92.8% | Gemini 3.1 Pro | Gemini 3 Flash |       ✓        |      —       |
| Emergence AI           |        86% | GPT-4o         | GPT-4o         |       ✓        |      ✓       |
| Supermemory            | 81.6–85.4% | various        | GPT-4o         |       ✓        |   unknown    |
| RetainDB               |        79% | (in-context)   | GPT-4o         |       ✓        |      ✗       |
| Zep (Cloud)            |      71.2% | GPT-4o         | GPT-4o         |       ✓        |      —       |

Putting Sibyl's 96.96% R@5 next to Mem0's 94.4% QA-accuracy or Mastra's 94.87% as if they were the
same metric is the exact category error MemPalace was called out for. The two axes answer different
questions:

- Retrieval R@K answers: "did we find the right context?"
- QA accuracy answers: "did the whole pipeline produce a correct answer that GPT-4o agreed with?"

Both matter. Sibyl is the retrieval substrate; a downstream reader model and prompt determine the QA
accuracy on top of it. Sibyl now has a gated LongMemEval-S QA lane with a `gpt-4o` reader, `gpt-5.2`
judge, pinned prompts, schema, rubric, accounting, and `qa_accuracy` no-regression gate. That lane
becomes comparable only after a pinned model-backed artifact is published.

## The Architectural Landscape

Six rough clusters cover most of the field as of June 2026.

### Cluster 1: Hosted-First Commercial Platforms

[Mem0](https://mem0.ai/) (Cloud + OpenMemory self-host), [Zep Cloud](https://www.getzep.com/),
[AWS AgentCore Memory](https://aws.amazon.com/bedrock/agentcore/). Vector-first or hybrid. All three
want you on their cloud. Zep
[killed self-hosted Community Edition](https://vectorize.io/articles/zep-alternatives) in 2025;
Mem0's April 2026 algorithm rewrite quietly dropped advertised graph-store integrations in favor of
internal entity linking. Physical tenant isolation is usually an Enterprise-tier feature.

### Cluster 2: Self-Hostable Graph-Native Engines

[Cognee](https://www.cognee.ai/), [MemOS (MemTensor)](https://github.com/MemTensor/MemOS), and raw
[Graphiti](https://github.com/getzep/graphiti) as a library. Sibyl's closest architectural siblings.
Cognee in particular ships per-tenant graph + vector store isolation (a permission model of
Users/Tenants/Roles plus dataset ACLs, toggled on via its Enable Backend Access Control (EBAC) env
flag, with physical isolation only on the Kuzu/LanceDB/FalkorDB backends), the most direct
production analog to Sibyl's namespace-per-org pattern.

### Cluster 3: Agent Runtimes That Bundle Memory

[Letta](https://www.letta.com/) (formerly MemGPT), [Mastra](https://mastra.ai/), and
[Agno](https://github.com/agno-agi/agno) (formerly Phidata). These compete on the "stateful agent
platform" axis, with memory as one of several primitives. Letta is the most mature; Mastra's
[Observational Memory](https://mastra.ai/research/observational-memory) is the only architecture in
this cluster posting credible >94% LongMemEval QA-accuracy scores.

### Cluster 4: Framework-Coupled Memory Libraries

[LangMem](https://github.com/langchain-ai/langmem) (LangGraph), CrewAI memory, third-party shims for
Pydantic AI. Cheap if you're already in the framework, irrelevant if you're not. Note: **AutoGen
entered maintenance mode in Q1 2026**; Microsoft moved active development to the
[Microsoft Agent Framework](https://learn.microsoft.com/en-us/agent-framework/migration-guide/from-autogen/).

### Cluster 5: Source-Preserving Local Libraries

[Memweave](https://github.com/sachinsharma9780/memweave) and
[MemPalace](https://github.com/MemPalace/mempalace). Both reject vector DBs as required
infrastructure and store source artifacts as the truth. Memweave is the honest, methodologically
clean version; MemPalace is the viral version where benchmark methodology
[fell apart on independent review](https://vectorize.io/articles/mempalace-benchmarks).

### Cluster 6: Provider-Bundled Consumer Memory

[Claude memory](https://www.anthropic.com/news/memory-managed-agents) (consumer + Managed Agents
filesystem memory, launched April 2026),
[ChatGPT memory](https://openai.com/index/memory-and-new-controls-for-chatgpt/). Not direct
competitors to a self-hosted developer-facing memory system, but they shape user expectations.
Anthropic's filesystem-mounted Managed Agents memory plus
[1M-token context GA](https://www.anthropic.com/news) is the platform-level threat: as context
grows, fewer problems require dedicated memory infrastructure.

### Cluster 7: Research Frontier

[Memory-R1](https://arxiv.org/abs/2508.19828), [A-MEM](https://arxiv.org/abs/2502.12110),
[Mem-α](https://arxiv.org/abs/2509.25911), [MAGMA](https://arxiv.org/abs/2601.03236),
[Kumiho](https://arxiv.org/abs/2603.17244), [FiFA](https://arxiv.org/html/2512.12856v1),
[MemOS](https://arxiv.org/abs/2507.03724). None production-ready in June 2026. The next paradigm
shift is RL-learned memory operations (store, retrieve, update, summarize, discard as tools the
agent uses, with policy learned via PPO or GRPO).

## Academic Frontier: Where Sibyl Trails

Sibyl is at LongMemEval-S retrieval ceiling. The field has moved on. Honest assessment of where
Sibyl trails academic SOTA:

- **Cross-encoder reranker.** [BGE-reranker-v2-m3](https://huggingface.co/BAAI/bge-reranker-v2-m3)
  and ColBERT add another +33–40% accuracy at 50–100 ms latency cost on most public benchmarks.
  Sibyl uses interpretable query-aware ranking instead. The trade-off is real observability and
  cost-per-query; the cap is strict-recall ranking quality on diffuse-evidence questions
  (single-session-preference at 79.26% NDCG@5 illustrates this).
- **Principled forgetting and consolidation.** [FadeMem](https://arxiv.org/pdf/2601.18642) reports
  45% storage reduction with biologically inspired exponential decay;
  [FiFA](https://arxiv.org/html/2512.12856v1) introduces six forgetting policies (FIFO, LRU,
  priority decay, reflection-summary, random-drop, hybrid) with privacy sensitivity scores. Sibyl
  ships a `priority_decay` consolidation job that archives low-importance, stale entities
  (importance × recency, reversible via `include_archived`), so forgetting is partial rather than
  absent; the gap is tuning and benchmarking it against FadeMem/FiFA-style policies and privacy
  sensitivity.
- **Procedural memory and skill learning.**
  [Letta's skill learning](https://www.letta.com/blog/skill-learning) reports +36.8% relative on
  Terminal-Bench 2.0. Sibyl has task `learnings` but no procedural-memory primitive that an agent
  can update its own behavior from.
- **Temporal decay scoring applied uniformly.** Sibyl applies temporal boosting by default in the
  hybrid search path (`apply_temporal=True`, 365-day half-life) but not in the context/recall path
  (which passes `temporal_target=None`), so decay is unevenly wired rather than a uniform ranking
  signal. The Stanford generative agents recency + importance + relevance scoring model has become a
  de facto standard.
- **Multi-graph disentanglement.** [MAGMA](https://arxiv.org/abs/2601.03236) splits memory into four
  orthogonal graphs (semantic, temporal, causal, entity) with intent-aware query routing, achieving
  +45.5% accuracy on LOCOMO at 0.7–4.2k tokens per query vs 101k for full context. Sibyl uses a
  single graph.
- **RL-learned memory operations.** [Memory-R1](https://arxiv.org/abs/2508.19828) trains
  store/retrieve/update/summarize/discard as RL-tuned tools with 152 training pairs, gaining 31% F1
  / 49% BLEU / 36% LLM-judge over Mem0. Sibyl uses hand-crafted query frames.
- **Belief revision semantics.** [Kumiho](https://arxiv.org/abs/2603.17244) proves AGM
  belief-revision postulates over a property graph runtime. Sibyl handles contradictions implicitly
  through projection.
- **LongMemEval-V2.** The [agent-shaped successor benchmark](https://arxiv.org/abs/2605.12493) (May
  2026, 451 questions, up to 115M tokens) tests workflow knowledge, environment gotchas, and premise
  awareness, closer to operational competence than conversational recall. Sibyl has no published
  number here. Best published is AgentRunbook-C at 72.5%.
- **BEAM (10M-token long-memory).**
  [Hindsight](https://hindsight.vectorize.io/blog/2026/04/02/beam-sota) reports 64.1% at 10M tokens,
  +58% over the next system. Sibyl has not been evaluated at this scale.

## Honest Gaps

Things we do not yet have, and want to be explicit about:

1. **No published QA-accuracy number.** Adding a thin reader pass over Sibyl's retrieved sessions
   plus the official LongMemEval GPT-4o judge would let us publish a number on the same axis as
   Mem0, Mastra, OMEGA, Hindsight, and Zep. This is on the roadmap, not the benchmark we lead with.
2. **No public local-embedding variant.** The full run uses OpenAI embeddings. A
   `text-embedding-3-small`-free variant with `all-MiniLM-L6-v2` or BGE-M3 would be directly
   comparable to MemPalace raw and agentmemory's measurements.
3. **No published LongMemEval-V2 number.** The official full-suite harness path is wired and
   internal runs exist (see [LongMemEval-V2](./longmemeval-v2.md)), but nothing is published or
   leaderboard-submitted yet, and nothing should be cited until a pinned receipt exists.
4. **No LOCOMO, BEAM, FiFA numbers.** LongMemEval-S is one dataset. The field is broader.
5. **No published latency-cost trade-off curve.** Search p95 is 1,115 ms in the full run; that's a
   working number but not yet contextualized against competitors' published latency-cost envelopes.
6. **No cross-encoder reranker.** We chose interpretable ranking; that choice has a cost on
   strict-recall ranking quality.
7. **Forgetting is partial, not principled.** A `priority_decay` consolidation job archives
   low-importance, stale entities (reversibly), but it is not yet tuned, benchmarked, or applied as
   a uniform decay signal across the context/recall path, so old facts still compete with new ones
   in the main recall scoring.

## How To Read This Page

If you remember one thing: retrieval R@K and end-to-end QA accuracy are different axes, and most
LongMemEval leaderboard tables mix them. Sibyl's 96.96% strict R@5 is in the retrieval lane. The
numbers from Mem0, Mastra, OMEGA, Hindsight, Zep, ByteRover, RetainDB, Supermemory, and Emergence AI
are QA-accuracy numbers, and they answer a different question. Sibyl's QA lane is wired, but it
should not be cited until the pinned artifact exists.

The retrieval-lane comparison Sibyl can defend right now: Sibyl reaches LongMemEval-S retrieval
ceiling on a stricter metric (full multi-answer recall, not lenient hit-rate), on the live
production API path, with per-question physical tenant isolation, and with no LLM in the retrieval
or extraction path. The retrieval-lane systems with credible published numbers in the same
neighborhood are MemPalace's honest 98.4% held-out, Memweave's 98.0% held-out, and agentmemory's
95.2%, all on `recall_any`, all single-tenant, all offline notebook measurements.

That is the position. We are happy to be wrong about anything in this page if a reader brings a
primary source that contradicts it.

## Related

- [LongMemEval Results](./longmemeval.md): the headline eval and methodology
- [Benchmark Methodology](./benchmark-methodology.md): the broader eval ladder, gates, reporting
  rules
- [Retrieval System Architecture](../architecture/retrieval-system.md): how the eval-passing path
  actually works
