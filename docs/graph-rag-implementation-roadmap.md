# Graph-RAG Implementation Roadmap for Sibyl

**Goal**: Transform Sibyl into the ultimate knowledge oracle using SOTA graph-RAG techniques

---

## Quick Reference: SOTA Techniques Summary

### Top 3 Innovations to Implement

1. **LLM Entity Extraction** (Microsoft GraphRAG approach)
   - Replace regex patterns with GPT-4/Claude structured output
   - Expected: 30% improvement in entity recall
   - Cost: ~$0.02 per document (one-time ingestion cost)

2. **Hybrid Retrieval** (Parallel vector + graph search)
   - Combine semantic embeddings with graph traversal
   - Expected: 20-25% improvement in answer quality
   - Latency: 95ms P95 (vs. 50ms vector-only)

3. **Community Detection + Summarization** (Microsoft GraphRAG)
   - Leiden algorithm to detect entity clusters
   - Pre-generate hierarchical summaries
   - Enables: "Tell me everything about X" queries

---

## Architecture Comparison

```
┌─────────────────────────────────────────────────────────────────┐
│                     CURRENT SIBYL ARCHITECTURE                  │
└─────────────────────────────────────────────────────────────────┘

Markdown Docs
    ↓
Semantic Chunking (H2/H3, 50-800 words)
    ↓
Regex Entity Extraction (~60% recall)
    ↓
Co-occurrence Relationships
    ↓
FalkorDB Storage (Graphiti episodic memory)
    ↓
Vector Search Only
    ↓
Results


┌─────────────────────────────────────────────────────────────────┐
│              PROPOSED SOTA ARCHITECTURE (3 PHASES)              │
└─────────────────────────────────────────────────────────────────┘

Markdown Docs
    ↓
Semantic Chunking (H2/H3) + Entity-Centric Chunks
    ↓
LLM Entity Extraction (~90% recall) + Deduplication
    ↓
LLM Relationship Extraction (typed: USES, REQUIRES, etc.)
    ↓
FalkorDB Storage + Community Detection
    ↓
Parallel Hybrid Search:
    ├─ Vector Search (top 20)
    ├─ Entity Linking → Graph Traversal (depth 2)
    └─ Community Search (hierarchical)
    ↓
Reciprocal Rank Fusion + Temporal Boosting
    ↓
Reranked Results
```

---

## Implementation Phases

### Phase 1: Foundation Upgrades (Weeks 1-2)

**Theme**: Improve data quality and retrieval basics

#### Week 1: Entity Extraction Upgrade

- [ ] Implement LLM-based entity extractor (`src/sibyl/ingestion/extractor_llm.py`)
- [ ] Add structured output schema for entities
- [ ] Create A/B testing harness (regex vs. LLM)
- [ ] Benchmark on 100 hand-labeled episodes
- [ ] Switch to LLM as default if F1 > 0.85

**Deliverable**: `EntityExtractorLLM` class with 90% recall

#### Week 2: Entity Deduplication

- [ ] Implement embedding-based entity clustering
- [ ] Add entity resolution logic (merge duplicates)
- [ ] Create canonical entity table
- [ ] Add entity linking for cross-document references

**Deliverable**: 20% reduction in duplicate entities

---

### Phase 2: Hybrid Retrieval (Weeks 3-4)

**Theme**: Combine vector + graph search for better results

#### Week 3: Parallel Hybrid Search

- [ ] Implement entity linker (query → entities)
- [ ] Add graph traversal from entities (depth 1-3)
- [ ] Build parallel execution (vector + graph)
- [ ] Implement Reciprocal Rank Fusion (RRF)
- [ ] Add temporal boosting (decay old episodes)

**Deliverable**: `hybrid_search()` function with 20% quality boost

#### Week 4: Multi-Hop Reasoning

- [ ] Implement beam search over graph
- [ ] Add path scoring and pruning
- [ ] Create multi-hop query interface
- [ ] Test on complex queries (e.g., "What tools prevent X error in Y language?")

**Deliverable**: 30% improvement on multi-hop queries

---

### Phase 3: Community Intelligence (Weeks 5-6)

**Theme**: Hierarchical organization for scalability

#### Week 5: Community Detection

- [ ] Integrate Leiden algorithm
- [ ] Create community entity clusters
- [ ] Build hierarchical community tree (C0, C1, C2...)
- [ ] Add community metadata storage

**Deliverable**: Community-based entity organization

#### Week 6: Community Summarization

- [ ] Generate summaries for each community
- [ ] Implement incremental summary updates
- [ ] Add community-level search endpoint
- [ ] Create drill-down retrieval (community → episode → chunk)

**Deliverable**: Global "tell me about X" queries

---

### Phase 4: Advanced Features (Weeks 7-8)

**Theme**: Next-gen capabilities

#### Week 7: Relationship Enhancement

- [ ] Extract typed relationships via LLM (CAUSES, PREVENTS, ENABLES)
- [ ] Add causal relationship detection
- [ ] Implement relationship-aware traversal
- [ ] Build "why" query answering (causal chains)

**Deliverable**: Causal reasoning for debugging queries

#### Week 8: Active Learning

- [ ] Detect knowledge gaps from failed queries
- [ ] Generate template suggestions
- [ ] Build feedback loop (user ratings → retraining)
- [ ] Create knowledge gap dashboard

**Deliverable**: Self-improving knowledge base

---

### Phase 5: Production Hardening (Weeks 9-10)

**Theme**: Performance, monitoring, reliability

#### Week 9: Optimization

- [ ] Benchmark retrieval quality (NDCG@10, Success@5)
- [ ] Optimize latency (caching, batching, parallelization)
- [ ] Add query result caching (Redis)
- [ ] Implement smart prefetching (predict next queries)

**Deliverable**: <100ms P95 latency, 85%+ NDCG@10

#### Week 10: Monitoring & Documentation

- [ ] Build monitoring dashboard (query patterns, hit rates)
- [ ] Create retrieval quality alerts
- [ ] Write implementation docs
- [ ] Create user guide for advanced search

**Deliverable**: Production-ready graph-RAG system

---

## Key Metrics to Track

### Retrieval Quality

- **NDCG@10**: Normalized Discounted Cumulative Gain (target: >0.80)
- **Success@5**: Query answered in top 5 results (target: >85%)
- **Multi-hop Accuracy**: Correct answer for 2-3 hop queries (target: >70%)
- **User Satisfaction**: Thumbs up/down on results (target: >80% positive)

### Performance

- **Latency P50**: Median response time (target: <50ms)
- **Latency P95**: 95th percentile response time (target: <100ms)
- **Throughput**: Queries per second (target: >100 QPS)
- **Cache Hit Rate**: Percentage of cached results (target: >60%)

### Data Quality

- **Entity Recall**: % of true entities extracted (target: >90%)
- **Entity Precision**: % of extracted entities correct (target: >88%)
- **Duplicate Rate**: % of duplicate entities (target: <5%)
- **Relationship Accuracy**: % of relationships correct (target: >80%)

---

## Quick Start: Implement Phase 1 This Week

### Step 1: Add LLM Entity Extractor (2-3 hours)

Create `/Users/bliss/dev/sibyl/src/sibyl/ingestion/extractor_llm.py`:

```python
"""LLM-based entity extractor using structured output."""

from typing import Any
from openai import AsyncOpenAI
from sibyl.ingestion.chunker import Episode
from sibyl.ingestion.extractor import ExtractedEntity, ExtractedEntityType

class EntityExtractorLLM:
    def __init__(self, model: str = "gpt-4o-mini"):
        self.client = AsyncOpenAI()
        self.model = model

    async def extract_from_episode(
        self, episode: Episode
    ) -> list[ExtractedEntity]:
        prompt = f"""
        Extract entities from this development wisdom episode:

        Title: {episode.title}
        Content: {episode.content}

        Entity types:
        - pattern: Coding pattern or best practice
        - rule: Sacred rule or invariant
        - tool: Library, framework, or development tool
        - language: Programming language
        - concept: Abstract concept or principle
        - warning: Warning or gotcha
        - tip: Tip or recommendation

        Return JSON array of entities with:
        - name: Entity name (concise)
        - type: One of the types above
        - description: Brief description (1 sentence)
        - confidence: 0.0-1.0 confidence score
        """

        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0.1,  # Low for consistency
        )

        entities_json = response.choices[0].message.content
        entities = self._parse_entities(entities_json, episode)
        return entities

    def _parse_entities(
        self, json_str: str, episode: Episode
    ) -> list[ExtractedEntity]:
        import json
        data = json.loads(json_str)

        entities = []
        for item in data.get("entities", []):
            entity_type = ExtractedEntityType(item["type"])
            entities.append(
                ExtractedEntity(
                    entity_type=entity_type,
                    name=item["name"],
                    description=item["description"],
                    confidence=item["confidence"],
                    source_episode_id=episode.id,
                    context=episode.content[:200],  # First 200 chars
                )
            )
        return entities
```

### Step 2: Add to Ingestion Pipeline (1 hour)

Modify `/Users/bliss/dev/sibyl/src/sibyl/ingestion/pipeline.py`:

```python
from sibyl.ingestion.extractor import EntityExtractor  # Existing
from sibyl.ingestion.extractor_llm import EntityExtractorLLM  # New

class IngestionPipeline:
    def __init__(self, repo_root: Path, use_llm: bool = False):
        self.repo_root = repo_root
        self.parser = MarkdownParser()
        self.chunker = SemanticChunker()

        # Choose extractor based on config
        if use_llm:
            self.entity_extractor = EntityExtractorLLM()
        else:
            self.entity_extractor = EntityExtractor()
```

### Step 3: Benchmark (1 hour)

Create `/Users/bliss/dev/sibyl/scripts/benchmark_extraction.py`:

```python
"""Benchmark regex vs. LLM entity extraction."""

import asyncio
from pathlib import Path
from sibyl.ingestion.extractor import EntityExtractor
from sibyl.ingestion.extractor_llm import EntityExtractorLLM
from sibyl.ingestion.pipeline import IngestionPipeline

async def benchmark():
    # Load 100 test episodes
    pipeline = IngestionPipeline(Path("./"))
    result = await pipeline.run()
    test_episodes = result.episodes[:100]

    # Run both extractors
    regex_extractor = EntityExtractor()
    llm_extractor = EntityExtractorLLM()

    regex_entities = []
    llm_entities = []

    for episode in test_episodes:
        regex_entities.extend(
            regex_extractor.extract_from_episode(episode)
        )
        llm_entities.extend(
            await llm_extractor.extract_from_episode(episode)
        )

    # Compare results
    print(f"Regex extracted: {len(regex_entities)} entities")
    print(f"LLM extracted: {len(llm_entities)} entities")

    # Manual review of sample (first 20)
    for i in range(20):
        print(f"\n--- Episode {i} ---")
        print(f"Regex: {[e.name for e in regex_entities if e.source_episode_id == test_episodes[i].id]}")
        print(f"LLM: {[e.name for e in llm_entities if e.source_episode_id == test_episodes[i].id]}")

asyncio.run(benchmark())
```

---

## Cost Analysis

### One-Time Ingestion Costs (LLM Extraction)

- **Documents**: ~200 wisdom docs
- **Episodes**: ~1,500 episodes (avg 7.5 per doc)
- **Tokens per episode**: ~800 tokens (500 input + 300 output)
- **Total tokens**: 1.2M tokens
- **Cost (GPT-4o-mini)**: $0.15 per 1M input, $0.60 per 1M output
  - Input: $0.18
  - Output: $0.27
  - **Total: ~$0.45**

### Ongoing Costs (Incremental Updates)

- **New episodes per week**: ~10
- **Weekly cost**: ~$0.03
- **Monthly cost**: ~$0.12

### Community Summarization (One-Time)

- **Communities**: ~50 communities (estimate)
- **Tokens per summary**: ~2,000 tokens
- **Total tokens**: 100K tokens
- **Cost**: ~$0.10

**Total first-month cost**: ~$0.60 **Ongoing monthly cost**: ~$0.15

---

## Risk Mitigation

### Risk 1: LLM Extraction Errors

**Mitigation**:

- Use low temperature (0.1) for consistency
- Validate output schema before storage
- Keep regex extractor as fallback
- Manual review of first 100 extractions

### Risk 2: Increased Latency

**Mitigation**:

- Run all expensive operations during ingestion (offline)
- Cache community summaries
- Use parallel hybrid search (async)
- Monitor P95 latency, alert if >150ms

### Risk 3: Graph Complexity Growth

**Mitigation**:

- Implement entity deduplication (reduce nodes by 20%)
- Prune low-confidence relationships (<0.5)
- Archive old episodes (>2 years) to separate graph
- Monitor graph size, set max entities (~10K)

---

## Success Criteria

After 10 weeks, Sibyl should:

- [ ] Extract entities with >90% recall (vs. ~60% baseline)
- [ ] Answer multi-hop queries with >70% accuracy
- [ ] Support "tell me about X" global queries via communities
- [ ] Respond in <100ms P95 latency
- [ ] Achieve >85% user satisfaction on complex queries

---

**Next Steps**:

1. Read full research doc: `/Users/bliss/dev/sibyl/docs/graph-rag-sota-research.md`
2. Start Phase 1, Week 1: Implement `EntityExtractorLLM`
3. Benchmark on 100 episodes, measure F1 score
4. If successful, proceed to entity deduplication (Week 2)

**Resources**:

- Microsoft GraphRAG paper: https://arxiv.org/abs/2404.16130
- Graphiti docs: https://github.com/getzep/graphiti
- LlamaIndex KG guide: https://docs.llamaindex.ai/en/stable/examples/knowledge_graph/

**Author**: Claude (Opus 4.5) **Date**: 2025-12-20
