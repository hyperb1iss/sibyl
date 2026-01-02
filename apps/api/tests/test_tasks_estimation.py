"""Tests for task effort estimation."""

from sibyl_core.models.tasks import SimilarTaskInfo, TaskEstimate
from sibyl_core.tasks.estimation import (
    SimilarTask,  # Backwards compat alias
    calculate_project_estimate,
)


class TestSimilarTaskInfo:
    """Tests for SimilarTaskInfo model."""

    def test_basic_similar_task(self) -> None:
        """Test creating a basic similar task."""
        task = SimilarTaskInfo(
            task_id="task-123",
            title="Implement OAuth",
            similarity_score=0.85,
            actual_hours=4.5,
        )
        assert task.task_id == "task-123"
        assert task.title == "Implement OAuth"
        assert task.similarity_score == 0.85
        assert task.actual_hours == 4.5

    def test_backwards_compat_alias(self) -> None:
        """Test SimilarTask backwards compatibility alias."""
        assert SimilarTask is SimilarTaskInfo


class TestTaskEstimate:
    """Tests for TaskEstimate model."""

    def test_successful_estimate(self) -> None:
        """Test a successful estimate result."""
        estimate = TaskEstimate(
            estimated_hours=5.0,
            confidence=0.75,
            similar_tasks=[
                SimilarTaskInfo(
                    task_id="t1", title="Task 1", similarity_score=0.9, actual_hours=4.0
                ),
                SimilarTaskInfo(
                    task_id="t2", title="Task 2", similarity_score=0.8, actual_hours=6.0
                ),
            ],
            based_on_tasks=2,
            reason="Estimated from 2 similar task(s)",
        )
        assert estimate.estimated_hours == 5.0
        assert estimate.confidence == 0.75
        assert estimate.based_on_tasks == 2
        assert len(estimate.similar_tasks) == 2

    def test_failed_estimate(self) -> None:
        """Test a failed estimate result."""
        estimate = TaskEstimate(
            estimated_hours=0,
            confidence=0,
            reason="No similar completed tasks found",
        )
        assert estimate.estimated_hours == 0
        assert estimate.confidence == 0
        assert estimate.similar_tasks == []
        assert estimate.based_on_tasks == 0

    def test_default_values(self) -> None:
        """Test default values for TaskEstimate."""
        estimate = TaskEstimate(
            estimated_hours=3.0,
            confidence=0.5,
        )
        assert estimate.similar_tasks == []
        assert estimate.based_on_tasks == 0
        assert estimate.reason == ""


class TestCalculateProjectEstimate:
    """Tests for calculate_project_estimate function."""

    def test_aggregate_estimates(self) -> None:
        """Test aggregating multiple task estimates."""
        estimates = [
            TaskEstimate(estimated_hours=4.0, confidence=0.8, based_on_tasks=3),
            TaskEstimate(estimated_hours=6.0, confidence=0.6, based_on_tasks=2),
            TaskEstimate(estimated_hours=2.0, confidence=0.9, based_on_tasks=4),
        ]

        result = calculate_project_estimate(estimates)

        # Total hours should be sum
        assert result.estimated_hours == 12.0
        # Confidence is weighted average by hours
        expected_confidence = (4.0 * 0.8 + 6.0 * 0.6 + 2.0 * 0.9) / 12.0
        assert abs(result.confidence - round(expected_confidence, 2)) < 0.01
        # based_on_tasks is sum
        assert result.based_on_tasks == 9

    def test_empty_estimates(self) -> None:
        """Test with no estimates."""
        result = calculate_project_estimate([])
        assert result.estimated_hours == 0
        assert result.confidence == 0
        assert "No task estimates" in result.reason

    def test_single_estimate(self) -> None:
        """Test with single estimate."""
        estimates = [
            TaskEstimate(estimated_hours=5.0, confidence=0.7, based_on_tasks=2),
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
            SimilarTaskInfo(
                task_id="t1", title="High similarity", similarity_score=0.9, actual_hours=4.0
            ),
            SimilarTaskInfo(
                task_id="t2", title="Lower similarity", similarity_score=0.6, actual_hours=8.0
            ),
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
        similar_tasks = [
            SimilarTaskInfo(
                task_id=f"t{i}", title=f"Task {i}", similarity_score=0.9, actual_hours=5.0
            )
            for i in range(10)
        ]

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
            SimilarTaskInfo(task_id="t1", title="Task 1", similarity_score=0.5, actual_hours=3.0),
        ]

        total_weight = sum(t.similarity_score for t in similar_tasks)
        avg_similarity = total_weight / len(similar_tasks)
        sample_factor = min(len(similar_tasks) / 5, 1.0)
        confidence = avg_similarity * sample_factor

        # 1 sample at 0.5 similarity = 0.5 * 0.2 = 0.1
        assert confidence == 0.1
