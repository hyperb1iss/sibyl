"""Tests for RAG evaluation metrics."""

from tests.evals.metrics import (
    EvalMetrics,
    EvalQuery,
    RetrievalResult,
    aggregate_metrics,
    compute_metrics,
    dcg_at_k,
    mean_reciprocal_rank,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
    success_at_k,
)


class TestDCG:
    """Tests for Discounted Cumulative Gain."""

    def test_perfect_ranking(self):
        """Test DCG with perfect ranking."""
        relevances = [3, 2, 1, 0]
        dcg = dcg_at_k(relevances, k=4)
        # DCG = 3 + 2/log2(3) + 1/log2(4) + 0
        expected = 3 + 2 / 1.585 + 1 / 2.0
        assert abs(dcg - expected) < 0.01

    def test_empty_list(self):
        """Test DCG with empty list."""
        assert dcg_at_k([], k=5) == 0.0

    def test_k_larger_than_list(self):
        """Test DCG when k exceeds list length."""
        relevances = [3, 2]
        dcg = dcg_at_k(relevances, k=10)
        # Should only use available elements
        expected = 3 + 2 / 1.585
        assert abs(dcg - expected) < 0.01


class TestNDCG:
    """Tests for Normalized DCG."""

    def test_perfect_ranking(self):
        """Test NDCG with perfect ranking (should be 1.0)."""
        relevances = [3, 2, 1, 0]  # Already perfectly ranked
        ndcg = ndcg_at_k(relevances, k=4)
        assert abs(ndcg - 1.0) < 0.01

    def test_worst_ranking(self):
        """Test NDCG with worst ranking (low score)."""
        relevances = [0, 0, 0, 3]  # Best result at end
        ndcg = ndcg_at_k(relevances, k=4)
        assert ndcg < 0.5

    def test_all_irrelevant(self):
        """Test NDCG with all irrelevant results."""
        relevances = [0, 0, 0, 0]
        ndcg = ndcg_at_k(relevances, k=4)
        assert ndcg == 0.0

    def test_empty_list(self):
        """Test NDCG with empty list."""
        assert ndcg_at_k([], k=5) == 0.0


class TestSuccessAtK:
    """Tests for Success@K metric."""

    def test_success_first_position(self):
        """Test success when relevant result is first."""
        relevances = [2, 0, 0, 0]
        assert success_at_k(relevances, k=5) == 1.0

    def test_success_last_position_within_k(self):
        """Test success when relevant result is at position k."""
        relevances = [0, 0, 0, 0, 2]
        assert success_at_k(relevances, k=5) == 1.0

    def test_failure_beyond_k(self):
        """Test failure when relevant result is beyond k."""
        relevances = [0, 0, 0, 0, 0, 2]
        assert success_at_k(relevances, k=5) == 0.0

    def test_custom_threshold(self):
        """Test with custom relevance threshold."""
        relevances = [1, 0, 0]  # Has relevance 1
        assert success_at_k(relevances, k=3, threshold=1) == 1.0
        assert success_at_k(relevances, k=3, threshold=2) == 0.0


class TestPrecisionAtK:
    """Tests for Precision@K metric."""

    def test_perfect_precision(self):
        """Test precision when all top k are relevant."""
        relevances = [1, 1, 1, 1, 1]
        assert precision_at_k(relevances, k=5) == 1.0

    def test_zero_precision(self):
        """Test precision when none are relevant."""
        relevances = [0, 0, 0, 0, 0]
        assert precision_at_k(relevances, k=5) == 0.0

    def test_partial_precision(self):
        """Test precision with mixed results."""
        relevances = [1, 0, 1, 0, 0]
        assert precision_at_k(relevances, k=5) == 0.4

    def test_empty_list(self):
        """Test precision with empty list."""
        assert precision_at_k([], k=5) == 0.0


class TestRecallAtK:
    """Tests for Recall@K metric."""

    def test_full_recall(self):
        """Test recall when all relevant docs are retrieved."""
        relevances = [1, 1, 1, 0, 0]  # 3 relevant, all retrieved
        assert recall_at_k(relevances, k=5, total_relevant=3) == 1.0

    def test_partial_recall(self):
        """Test recall with some relevant docs missing."""
        relevances = [1, 0, 1, 0, 0]  # 2 out of 5 relevant
        assert recall_at_k(relevances, k=5, total_relevant=5) == 0.4

    def test_zero_recall(self):
        """Test recall when no relevant docs retrieved."""
        relevances = [0, 0, 0, 0, 0]
        assert recall_at_k(relevances, k=5, total_relevant=10) == 0.0

    def test_zero_total_relevant(self):
        """Test recall when there are no relevant docs."""
        relevances = [0, 0, 0]
        assert recall_at_k(relevances, k=3, total_relevant=0) == 0.0


class TestMRR:
    """Tests for Mean Reciprocal Rank."""

    def test_first_position(self):
        """Test MRR when first result is relevant."""
        relevances = [1, 0, 0, 0]
        assert mean_reciprocal_rank(relevances) == 1.0

    def test_second_position(self):
        """Test MRR when second result is relevant."""
        relevances = [0, 1, 0, 0]
        assert mean_reciprocal_rank(relevances) == 0.5

    def test_third_position(self):
        """Test MRR when third result is relevant."""
        relevances = [0, 0, 1, 0]
        assert abs(mean_reciprocal_rank(relevances) - 1 / 3) < 0.01

    def test_no_relevant(self):
        """Test MRR when no results are relevant."""
        relevances = [0, 0, 0, 0]
        assert mean_reciprocal_rank(relevances) == 0.0


class TestComputeMetrics:
    """Tests for compute_metrics function."""

    def test_basic_computation(self):
        """Test basic metrics computation."""
        results = [
            RetrievalResult(id="doc1", content="...", score=0.9),
            RetrievalResult(id="doc2", content="...", score=0.8),
            RetrievalResult(id="doc3", content="...", score=0.7),
        ]

        query = EvalQuery(
            query="test query",
            expected_ids=["doc1", "doc2"],
            relevance_grades={"doc1": 3, "doc2": 2},
        )

        metrics = compute_metrics(results, query, latency_ms=50.0)

        assert metrics.latency_ms == 50.0
        assert 5 in metrics.ndcg_at_k
        assert 10 in metrics.ndcg_at_k
        assert metrics.mrr == 1.0  # doc1 is first

    def test_with_custom_k_values(self):
        """Test with custom k values."""
        results = [
            RetrievalResult(id="doc1", content="...", score=0.9),
        ]

        query = EvalQuery(
            query="test",
            expected_ids=["doc1"],
        )

        metrics = compute_metrics(results, query, k_values=[1, 2, 3])

        assert 1 in metrics.ndcg_at_k
        assert 2 in metrics.ndcg_at_k
        assert 3 in metrics.ndcg_at_k
        assert 5 not in metrics.ndcg_at_k  # Not in k_values


class TestAggregateMetrics:
    """Tests for aggregate_metrics function."""

    def test_average_computation(self):
        """Test that aggregation computes averages."""
        metrics1 = EvalMetrics(
            ndcg_at_k={5: 0.8, 10: 0.9},
            mrr=1.0,
            latency_ms=50.0,
        )

        metrics2 = EvalMetrics(
            ndcg_at_k={5: 0.6, 10: 0.7},
            mrr=0.5,
            latency_ms=100.0,
        )

        aggregated = aggregate_metrics([metrics1, metrics2])

        assert aggregated.ndcg_at_k[5] == 0.7
        assert aggregated.ndcg_at_k[10] == 0.8
        assert aggregated.mrr == 0.75
        assert aggregated.latency_ms == 75.0

    def test_empty_list(self):
        """Test aggregation with empty list."""
        aggregated = aggregate_metrics([])
        assert aggregated.mrr == 0.0


class TestEvalMetrics:
    """Tests for EvalMetrics dataclass."""

    def test_to_dict(self):
        """Test conversion to dictionary."""
        metrics = EvalMetrics(
            ndcg_at_k={5: 0.8, 10: 0.9},
            success_at_k={5: 1.0, 10: 1.0},
            precision_at_k={5: 0.6, 10: 0.5},
            recall_at_k={5: 0.8, 10: 1.0},
            mrr=1.0,
            latency_ms=50.0,
        )

        d = metrics.to_dict()

        assert d["ndcg@5"] == 0.8
        assert d["ndcg@10"] == 0.9
        assert d["mrr"] == 1.0
        assert d["latency_ms"] == 50.0


class TestRetrievalResult:
    """Tests for RetrievalResult dataclass."""

    def test_creation(self):
        """Test result creation."""
        result = RetrievalResult(
            id="doc-123",
            content="Sample content",
            score=0.95,
            relevance=3,
        )

        assert result.id == "doc-123"
        assert result.score == 0.95
        assert result.relevance == 3

    def test_defaults(self):
        """Test default values."""
        result = RetrievalResult(id="doc", content="", score=0.5)

        assert result.relevance == 0
        assert result.metadata == {}


class TestEvalQuery:
    """Tests for EvalQuery dataclass."""

    def test_creation(self):
        """Test query creation."""
        query = EvalQuery(
            query="test query",
            expected_ids=["doc1", "doc2"],
            relevance_grades={"doc1": 3, "doc2": 2},
        )

        assert query.query == "test query"
        assert len(query.expected_ids) == 2
        assert query.relevance_grades["doc1"] == 3

    def test_defaults(self):
        """Test default values."""
        query = EvalQuery(
            query="test",
            expected_ids=[],
        )

        assert query.relevance_grades == {}
        assert query.metadata == {}
