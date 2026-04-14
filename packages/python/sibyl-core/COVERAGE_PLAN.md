# sibyl-core Test Coverage Plan

**Current State:** 12% coverage (652/5627 statements)
**Target:** 80% coverage (~4500 statements)
**Gap:** ~3850 statements to cover

## Coverage by Module

| Module | Lines | Current | Priority | Testability |
|--------|-------|---------|----------|-------------|
| **graph/entities.py** | 1843 | 0% | P0 | Medium (needs mocks) |
| **graph/communities.py** | 1510 | 0% | P2 | Medium (needs mocks) |
| **tools/manage.py** | 1180 | 0% | P1 | Medium (needs mocks) |
| **tools/admin.py** | 1173 | 0% | P2 | Medium (needs mocks) |
| **tasks/workflow.py** | 752 | 0% | P1 | High (mostly pure) |
| **graph/relationships.py** | 641 | 0% | P1 | Medium (needs mocks) |
| **tools/explore.py** | 568 | 0% | P1 | Medium (needs mocks) |
| **retrieval/dedup.py** | 524 | 0% | P0 | High (pure algorithms) |
| **tools/add.py** | 517 | 0% | P1 | Medium (needs mocks) |
| **tools/helpers.py** | 485 | 0% | P0 | High (pure functions) |
| **graph/client.py** | 472 | 0% | P1 | Low (infra) |
| **tools/search.py** | 467 | 0% | P1 | Medium (needs mocks) |
| **tasks/dependencies.py** | 439 | 0% | P1 | High (pure algorithms) |
| **models/tasks.py** | 407 | 83% | P0 | High (pure) |
| **tasks/manager.py** | 403 | 0% | P1 | Medium (async) |
| **retrieval/bm25.py** | 380 | 0% | P0 | High (pure algorithms) |
| **retrieval/hybrid.py** | 364 | 0% | P1 | High (mostly pure) |
| **models/agents.py** | 357 | 73% | P0 | High (pure) |
| **retrieval/fusion.py** | 313 | 0% | P1 | High (pure algorithms) |
| **retrieval/temporal.py** | 252 | 0% | P1 | High (pure algorithms) |
| **utils/resilience.py** | 216 | 0% | P0 | High (pure async) |
| **auth/jwt.py** | 165 | 0% | P0 | High (security-critical) |
| **auth/passwords.py** | 52 | 0% | P0 | High (security-critical) |

---

## Phase 1: Pure Functions & Security (Target: +40% coverage)

**Goal:** Cover security-critical code + pure algorithms that need zero mocking.

### 1.1 Auth Module (Security Critical) — ~220 statements

```
auth/jwt.py         - JWT creation, verification, decode
auth/passwords.py   - Password hashing, verification
auth/context.py     - Request context (mock FastAPI)
```

**Test file:** `tests/test_auth.py`
**Tests needed:**
- `test_create_access_token_*` (valid, with org, with extras)
- `test_verify_access_token_*` (valid, expired, wrong type, tampered)
- `test_create_refresh_token_*` (valid, with session)
- `test_verify_refresh_token_*` (valid, expired, grace period)
- `test_hash_password_*` (valid, empty error)
- `test_verify_password_*` (correct, wrong, malformed)

### 1.2 Retrieval Algorithms — ~1300 statements

```
retrieval/bm25.py      - BM25 keyword search (pure)
retrieval/dedup.py     - Deduplication (pure)
retrieval/fusion.py    - RRF score fusion (pure)
retrieval/temporal.py  - Temporal decay (pure)
retrieval/hybrid.py    - Hybrid search orchestration
```

**Test file:** `tests/test_retrieval.py`
**Tests needed:**
- `test_tokenize_*` (basic, stop words, min length)
- `test_bm25_index_*` (add docs, search, empty)
- `test_dedup_*` (exact, fuzzy, semantic)
- `test_rrf_fusion_*` (basic merge, weights, empty)
- `test_temporal_decay_*` (recent boost, old decay)

### 1.3 Utility Functions — ~700 statements

```
tools/helpers.py        - String formatting, ID generation
utils/resilience.py     - Retry decorators, circuit breaker
tasks/dependencies.py   - DAG operations, topological sort
```

**Test file:** `tests/test_utils.py`
**Tests needed:**
- `test_format_*` helpers
- `test_retry_with_backoff_*` (success, retry, max attempts)
- `test_circuit_breaker_*` (open, half-open, close)
- `test_dependency_graph_*` (add, remove, cycle detection, topo sort)

---

## Phase 2: Models & Business Logic (Target: +15% coverage)

**Goal:** Complete model coverage + task workflow logic.

### 2.1 Complete Model Coverage — ~400 statements

```
models/tasks.py     - Task validation, transitions (83% → 100%)
models/agents.py    - Agent records, status (73% → 100%)
models/sources.py   - Source types (72% → 100%)
```

**Test file:** `tests/test_models.py` (extend existing)
**Tests needed:**
- Edge cases for existing models
- Status transition validations
- Serialization round-trips

### 2.2 Task Workflow — ~750 statements

```
tasks/workflow.py    - State machine, transitions
tasks/estimation.py  - Complexity estimation
```

**Test file:** `tests/test_tasks.py`
**Tests needed:**
- `test_workflow_transition_*` (valid, invalid, guards)
- `test_estimate_complexity_*` (trivial, complex, epic)

---

## Phase 3: Graph Operations (Target: +20% coverage)

**Goal:** Test EntityManager with FalkorDB mocks.

### 3.1 Mock Infrastructure

Create `tests/conftest.py` with:
```python
@pytest.fixture
def mock_falkordb():
    """Mock FalkorDBClient for unit tests."""
    ...

@pytest.fixture
def entity_manager(mock_falkordb):
    """EntityManager with mocked graph."""
    ...
```

### 3.2 Entity Operations — ~1800 statements

```
graph/entities.py      - CRUD, queries, bulk ops
graph/relationships.py - Relationship management
```

**Test file:** `tests/test_graph_entities.py`
**Tests needed:**
- `test_create_entity_*` (task, project, pattern)
- `test_get_entity_*` (by id, by type, not found)
- `test_update_entity_*` (partial, full, validation)
- `test_delete_entity_*` (exists, cascade)
- `test_query_entities_*` (filters, pagination, sorting)

### 3.3 Community Detection — ~1500 statements

**Test file:** `tests/test_communities.py`
**Tests needed:**
- `test_detect_communities_*` (small graph, large graph)
- `test_community_metrics_*` (modularity, cohesion)

---

## Phase 4: Tools Integration (Target: +5% coverage)

**Goal:** Test MCP tool implementations with mocked dependencies.

### 4.1 Tool Tests

```
tools/search.py   - Search tool
tools/explore.py  - Explore tool
tools/add.py      - Add tool
tools/manage.py   - Manage tool
```

**Test file:** `tests/test_tools.py`
**Tests needed:**
- Input validation tests
- Response formatting tests
- Error handling tests

---

## Test File Structure

```
tests/
├── conftest.py           # Shared fixtures (mocks, factories)
├── test_models.py        # Existing + extensions
├── test_auth.py          # NEW: JWT + passwords
├── test_retrieval.py     # NEW: BM25, dedup, fusion
├── test_utils.py         # NEW: Helpers, resilience
├── test_tasks.py         # NEW: Workflow, estimation
├── test_graph_entities.py # NEW: EntityManager
├── test_graph_client.py  # NEW: FalkorDBClient
├── test_communities.py   # NEW: Community detection
└── test_tools.py         # NEW: MCP tools
```

---

## Implementation Order

| Week | Phase | Files | Expected Coverage |
|------|-------|-------|-------------------|
| 1 | 1.1 Auth | test_auth.py | +4% → 16% |
| 1 | 1.2 Retrieval | test_retrieval.py | +23% → 39% |
| 2 | 1.3 Utils | test_utils.py | +12% → 51% |
| 2 | 2.1 Models | test_models.py | +7% → 58% |
| 3 | 2.2 Tasks | test_tasks.py | +13% → 71% |
| 3 | 3.1 Mocks | conftest.py | — |
| 4 | 3.2 Entities | test_graph_entities.py | +15% → 86% |

---

## Quick Wins (Can Do Now)

1. **auth/passwords.py** — 52 lines, pure, security-critical
2. **retrieval/bm25.py tokenize()** — 30 lines, pure function
3. **tools/helpers.py** — string utilities, no deps
4. **utils/resilience.py** — async retry logic

These 4 targets would add ~15% coverage immediately.
