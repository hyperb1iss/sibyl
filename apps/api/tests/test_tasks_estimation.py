"""Tests for task effort estimation."""

from sibyl.tasks.estimation import (
    SimilarTask,
    TaskEstimate,
    calculate_project_estimate,
)


class TestSimilarTask:
    """Tests for SimilarTask dataclass."""

    def test_basic_similar_task(self) -> None:
        """Test creating a basic similar task."""
        task = SimilarTask(
            task_id="task-123",
            title="Implement OAuth",
            similarity_score=0.85,
            actual_hours=4.5,
        )
        assert task.task_id == "task-123"
        assert task.title == "Implement OAuth"
        assert task.similarity_score == 0.85
        assert task.actual_hours == 4.5


class TestTaskEstimate:
    """Tests for TaskEstimate dataclass."""

    def test_successful_estimate(self) -> None:
        """Test a successful estimate result."""
        estimate = TaskEstimate(
            estimated_hours=5.0,
            confidence=0.75,
            similar_tasks=[
                SimilarTask("t1", "Task 1", 0.9, 4.0),
                SimilarTask("t2", "Task 2", 0.8, 6.0),
            ],
            sample_count=2,
            message="Estimated from 2 similar task(s)",
        )
        assert estimate.estimated_hours == 5.0
        assert estimate.confidence == 0.75
        assert estimate.sample_count == 2
        assert len(estimate.similar_tasks) == 2

    def test_failed_estimate(self) -> None:
        """Test a failed estimate result."""
        estimate = TaskEstimate(
            estimated_hours=0,
            confidence=0,
            message="No similar completed tasks found",
        )
        assert estimate.estimated_hours == 0
        assert estimate.confidence == 0
        assert estimate.similar_tasks == []
        assert estimate.sample_count == 0

    def test_default_values(self) -> None:
        """Test default values for TaskEstimate."""
        estimate = TaskEstimate(
            estimated_hours=3.0,
            confidence=0.5,
        )
        assert estimate.similar_tasks == []
        assert estimate.sample_count == 0
        assert estimate.message == ""


class TestCalculateProjectEstimate:
    """Tests for calculate_project_estimate function."""

    def test_aggregate_estimates(self) -> None:
        """Test aggregating multiple task estimates."""
        estimates = [
            TaskEstimate(estimated_hours=4.0, confidence=0.8, sample_count=3),
            TaskEstimate(estimated_hours=6.0, confidence=0.6, sample_count=2),
            TaskEstimate(estimated_hours=2.0, confidence=0.9, sample_count=4),
        ]

        result = calculate_project_estimate(estimates)

        # Total hours should be sum
        assert result.estimated_hours == 12.0
        # Confidence is weighted average by hours
        expected_confidence = (4.0 * 0.8 + 6.0 * 0.6 + 2.0 * 0.9) / 12.0
        assert abs(result.confidence - round(expected_confidence, 2)) < 0.01
        # Sample count is sum
        assert result.sample_count == 9

    def test_empty_estimates(self) -> None:
        """Test with no estimates."""
        result = calculate_project_estimate([])
        assert result.estimated_hours == 0
        assert result.confidence == 0
        assert "No task estimates" in result.message

    def test_single_estimate(self) -> None:
        """Test with single estimate."""
        estimates = [
            TaskEstimate(estimated_hours=5.0, confidence=0.7, sample_count=2),
        ]

        result = calculate_project_estimate(estimates)
        assert result.estimated_hours == 5.0
        assert result.confidence == 0.7


class TestEstimationAlgorithm:
    """Tests for estimation algorithm logic."""

    def test_weighted_average_calculation(self) -> None:
        """Test weighted average calculation for estimates."""
        # Simulate: two tasks with different similarities and hours
        similar_tasks = [
            SimilarTask("t1", "High similarity", 0.9, 4.0),  # weight 0.9
            SimilarTask("t2", "Lower similarity", 0.6, 8.0),  # weight 0.6
        ]

        total_weight = sum(t.similarity_score for t in similar_tasks)
        weighted_hours = sum(t.actual_hours * t.similarity_score for t in similar_tasks)
        expected = weighted_hours / total_weight

        # (0.9 * 4.0 + 0.6 * 8.0) / (0.9 + 0.6) = (3.6 + 4.8) / 1.5 = 5.6
        assert abs(expected - 5.6) < 0.01

    def test_confidence_calculation(self) -> None:
        """Test confidence calculation formula."""
        # Confidence = avg_similarity * sample_factor
        # sample_factor = min(sample_count / 5, 1.0)

        # 3 samples with avg similarity 0.8
        avg_similarity = 0.8
        sample_count = 3
        sample_factor = min(sample_count / 5, 1.0)
        confidence = avg_similarity * sample_factor

        assert sample_factor == 0.6
        assert confidence == 0.48

        # 5+ samples reaches max sample_factor
        sample_count = 5
        sample_factor = min(sample_count / 5, 1.0)
        confidence = avg_similarity * sample_factor

        assert sample_factor == 1.0
        assert confidence == 0.8

    def test_high_confidence_scenario(self) -> None:
        """Test scenario that should produce high confidence."""
        # Many samples with high similarity
        similar_tasks = [SimilarTask(f"t{i}", f"Task {i}", 0.9, 5.0) for i in range(10)]

        total_weight = sum(t.similarity_score for t in similar_tasks)
        avg_similarity = total_weight / len(similar_tasks)
        sample_factor = min(len(similar_tasks) / 5, 1.0)
        confidence = avg_similarity * sample_factor

        # 10 samples at 0.9 similarity = 0.9 * 1.0 = 0.9
        assert confidence == 0.9

    def test_low_confidence_scenario(self) -> None:
        """Test scenario that should produce low confidence."""
        # Few samples with low similarity
        similar_tasks = [
            SimilarTask("t1", "Task 1", 0.5, 3.0),
        ]

        total_weight = sum(t.similarity_score for t in similar_tasks)
        avg_similarity = total_weight / len(similar_tasks)
        sample_factor = min(len(similar_tasks) / 5, 1.0)
        confidence = avg_similarity * sample_factor

        # 1 sample at 0.5 similarity = 0.5 * 0.2 = 0.1
        assert confidence == 0.1
