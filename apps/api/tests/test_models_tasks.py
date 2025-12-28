"""Tests for task management models."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from sibyl.models.entities import EntityType
from sibyl.models.tasks import (
    Epic,
    EpicStatus,
    ErrorPattern,
    Milestone,
    Project,
    ProjectStatus,
    Task,
    TaskComplexity,
    TaskEstimate,
    TaskKnowledgeSuggestion,
    TaskPriority,
    TaskStatus,
    Team,
    TimeEntry,
)


class TestTaskStatusEnum:
    """Tests for TaskStatus enum."""

    def test_all_status_values(self) -> None:
        """Verify all expected status values exist."""
        expected = {"backlog", "todo", "doing", "blocked", "review", "done", "archived"}
        actual = {s.value for s in TaskStatus}
        assert actual == expected

    def test_status_string_conversion(self) -> None:
        """Test enum to string conversion."""
        assert str(TaskStatus.DOING) == "doing"
        assert TaskStatus.REVIEW == "review"


class TestTaskPriorityEnum:
    """Tests for TaskPriority enum."""

    def test_all_priority_values(self) -> None:
        """Verify all expected priority values exist."""
        expected = {"critical", "high", "medium", "low", "someday"}
        actual = {p.value for p in TaskPriority}
        assert actual == expected


class TestTaskComplexityEnum:
    """Tests for TaskComplexity enum."""

    def test_all_complexity_values(self) -> None:
        """Verify all expected complexity values exist."""
        expected = {"trivial", "simple", "medium", "complex", "epic"}
        actual = {c.value for c in TaskComplexity}
        assert actual == expected


class TestTask:
    """Tests for Task entity model."""

    def test_minimal_task_creation(self) -> None:
        """Test creating a task with minimal fields."""
        task = Task(
            id="task-001",
            title="Implement auth",
            project_id="proj-001",  # Required field
        )
        assert task.id == "task-001"
        assert task.title == "Implement auth"
        assert task.entity_type == EntityType.TASK
        assert task.status == TaskStatus.TODO
        assert task.priority == TaskPriority.MEDIUM
        assert task.complexity == TaskComplexity.MEDIUM

    def test_full_task_creation(self) -> None:
        """Test creating a task with all fields."""
        now = datetime.now(UTC)
        task = Task(
            id="task-002",
            title="Add OAuth 2.0 login",
            description="Implement OAuth with Google and GitHub",
            status=TaskStatus.DOING,
            priority=TaskPriority.HIGH,
            complexity=TaskComplexity.COMPLEX,
            project_id="proj-001",
            feature="Authentication",
            sprint="Sprint 24",
            assignees=["alice@example.com", "bob@example.com"],
            due_date=now,
            estimated_hours=8.0,
            actual_hours=6.5,
            domain="auth",
            technologies=["typescript", "oauth2", "passport.js"],
            branch_name="feature/oauth-login",
            commit_shas=["abc123", "def456"],
            pr_url="https://github.com/org/repo/pull/42",
            learnings="OAuth redirect URIs must match exactly",
            blockers_encountered=["Google OAuth config issue"],
            started_at=now,
            completed_at=now,
            reviewed_at=now,
        )
        assert task.status == TaskStatus.DOING
        assert task.priority == TaskPriority.HIGH
        assert len(task.assignees) == 2
        assert "typescript" in task.technologies
        assert task.pr_url == "https://github.com/org/repo/pull/42"

    def test_task_name_property(self) -> None:
        """Test that name property returns title."""
        task = Task(id="t1", title="Test Task", project_id="proj-001")
        assert task.name == "Test Task"

    def test_task_content_property(self) -> None:
        """Test that content property returns description."""
        task = Task(
            id="t1", title="Test", description="Detailed description", project_id="proj-001"
        )
        assert task.content == "Detailed description"

    def test_task_title_max_length(self) -> None:
        """Test title max length validation."""
        with pytest.raises(ValidationError):
            Task(id="t1", title="x" * 201, project_id="proj-001")

    def test_task_order_default(self) -> None:
        """Test task_order default value."""
        task = Task(id="t1", title="Test", project_id="proj-001")
        assert task.task_order == 0


class TestProject:
    """Tests for Project entity model."""

    def test_minimal_project_creation(self) -> None:
        """Test creating a project with minimal fields."""
        project = Project(
            id="proj-001",
            title="Sibyl Enhancement",
        )
        assert project.id == "proj-001"
        assert project.title == "Sibyl Enhancement"
        assert project.entity_type == EntityType.PROJECT
        assert project.status == ProjectStatus.ACTIVE

    def test_full_project_creation(self) -> None:
        """Test creating a project with all fields."""
        now = datetime.now(UTC)
        project = Project(
            id="proj-002",
            title="Graph-RAG Implementation",
            description="Implement SOTA graph-RAG features",
            status=ProjectStatus.ACTIVE,
            repository_url="https://github.com/org/sibyl",
            features=["Entity Model", "Unified Tools", "Crawling"],
            tech_stack=["Python", "FalkorDB", "FastMCP"],
            knowledge_domains=["graph-rag", "nlp", "knowledge-graphs"],
            team_members=["alice@example.com"],
            tags=["priority", "q4"],
            start_date=now,
            target_date=now,
            total_tasks=50,
            completed_tasks=10,
            in_progress_tasks=5,
        )
        assert len(project.features) == 3
        assert project.total_tasks == 50
        assert project.name == "Graph-RAG Implementation"

    def test_project_status_enum(self) -> None:
        """Test all project status values."""
        expected = {"planning", "active", "on_hold", "completed", "archived"}
        actual = {s.value for s in ProjectStatus}
        assert actual == expected


class TestEpicStatusEnum:
    """Tests for EpicStatus enum."""

    def test_all_status_values(self) -> None:
        """Verify all expected status values exist."""
        expected = {"planning", "in_progress", "blocked", "completed", "archived"}
        actual = {s.value for s in EpicStatus}
        assert actual == expected

    def test_status_string_conversion(self) -> None:
        """Test enum to string conversion."""
        assert str(EpicStatus.IN_PROGRESS) == "in_progress"
        assert EpicStatus.COMPLETED == "completed"


class TestEpic:
    """Tests for Epic entity model."""

    def test_minimal_epic_creation(self) -> None:
        """Test creating an epic with minimal fields."""
        epic = Epic(
            id="epic-001",
            title="OAuth Implementation",
            project_id="proj-001",
        )
        assert epic.id == "epic-001"
        assert epic.title == "OAuth Implementation"
        assert epic.entity_type == EntityType.EPIC
        assert epic.status == EpicStatus.PLANNING
        assert epic.priority == TaskPriority.MEDIUM
        assert epic.project_id == "proj-001"

    def test_full_epic_creation(self) -> None:
        """Test creating an epic with all fields."""
        now = datetime.now(UTC)
        epic = Epic(
            id="epic-002",
            title="Authentication System",
            description="Complete auth system with OAuth, JWT, and session management",
            status=EpicStatus.IN_PROGRESS,
            priority=TaskPriority.HIGH,
            project_id="proj-001",
            start_date=now,
            target_date=now,
            completed_date=now,
            total_tasks=12,
            completed_tasks=5,
            assignees=["alice@example.com", "bob@example.com"],
            tags=["security", "core"],
            learnings="OAuth redirect URIs must match exactly",
        )
        assert epic.status == EpicStatus.IN_PROGRESS
        assert epic.priority == TaskPriority.HIGH
        assert len(epic.assignees) == 2
        assert epic.total_tasks == 12
        assert epic.completed_tasks == 5
        assert "security" in epic.tags

    def test_epic_name_property(self) -> None:
        """Test that name property returns title."""
        epic = Epic(id="e1", title="Test Epic", project_id="proj-001")
        assert epic.name == "Test Epic"

    def test_epic_content_property(self) -> None:
        """Test that content property returns description."""
        epic = Epic(
            id="e1",
            title="Test",
            description="Detailed epic description",
            project_id="proj-001",
        )
        assert epic.content == "Detailed epic description"

    def test_epic_title_max_length(self) -> None:
        """Test title max length validation."""
        with pytest.raises(ValidationError):
            Epic(id="e1", title="x" * 201, project_id="proj-001")

    def test_epic_requires_project_id(self) -> None:
        """Test that project_id is required."""
        with pytest.raises(ValidationError):
            Epic(id="e1", title="Test Epic")  # Missing project_id


class TestTeam:
    """Tests for Team entity model."""

    def test_team_creation(self) -> None:
        """Test creating a team."""
        team = Team(
            id="team-001",
            name="Platform Team",
            description="Core platform development",
            members=["alice@example.com", "bob@example.com"],
            focus_areas=["infrastructure", "api"],
        )
        assert team.entity_type == EntityType.TEAM
        assert len(team.members) == 2
        assert team.content == "Core platform development"


class TestErrorPattern:
    """Tests for ErrorPattern entity model."""

    def test_error_pattern_creation(self) -> None:
        """Test creating an error pattern."""
        pattern = ErrorPattern(
            id="err-001",
            error_message="TypeError: Cannot read property 'x' of undefined",
            root_cause="Accessing object before initialization",
            solution="Add null check or use optional chaining",
            prevention="Use TypeScript strict mode",
            languages=["javascript", "typescript"],
            technologies=["react", "node"],
            occurrence_count=5,
        )
        assert pattern.entity_type == EntityType.ERROR_PATTERN
        assert pattern.occurrence_count == 5
        assert "TypeError" in pattern.name

    def test_error_pattern_content_format(self) -> None:
        """Test error pattern content property formatting."""
        pattern = ErrorPattern(
            id="err-002",
            error_message="Connection refused",
            root_cause="Service not running",
            solution="Start the service",
            prevention="Add health checks",
        )
        content = pattern.content
        assert "Connection refused" in content
        assert "Service not running" in content
        assert "Start the service" in content


class TestMilestone:
    """Tests for Milestone entity model."""

    def test_milestone_creation(self) -> None:
        """Test creating a milestone."""
        now = datetime.now(UTC)
        milestone = Milestone(
            id="ms-001",
            name="Sprint 24",
            description="Q4 sprint focusing on auth",
            project_id="proj-001",
            start_date=now,
            end_date=now,
            target_date=now,
            total_tasks=10,
            completed_tasks=3,
        )
        assert milestone.entity_type == EntityType.MILESTONE
        assert milestone.project_id == "proj-001"
        assert milestone.name == "Sprint 24"


class TestTimeEntry:
    """Tests for TimeEntry model."""

    def test_time_entry_creation(self) -> None:
        """Test creating a time entry."""
        now = datetime.now(UTC)
        entry = TimeEntry(
            task_id="task-001",
            user="alice@example.com",
            duration_minutes=90,
            description="Implemented OAuth flow",
            started_at=now,
            ended_at=now,
            tags=["coding", "auth"],
        )
        assert entry.duration_minutes == 90
        assert len(entry.tags) == 2


class TestTaskEstimate:
    """Tests for TaskEstimate model."""

    def test_task_estimate_creation(self) -> None:
        """Test creating a task estimate."""
        estimate = TaskEstimate(
            estimated_hours=6.5,
            confidence=0.85,
            based_on_tasks=8,
            similar_tasks=[{"id": "t1", "title": "Similar task", "hours": 6.0}],
            reason="Based on 8 similar auth tasks",
        )
        assert estimate.estimated_hours == 6.5
        assert estimate.confidence == 0.85

    def test_confidence_bounds(self) -> None:
        """Test confidence value bounds."""
        # Valid bounds
        TaskEstimate(confidence=0.0)
        TaskEstimate(confidence=1.0)

        # Invalid bounds
        with pytest.raises(ValidationError):
            TaskEstimate(confidence=1.5)
        with pytest.raises(ValidationError):
            TaskEstimate(confidence=-0.1)


class TestTaskKnowledgeSuggestion:
    """Tests for TaskKnowledgeSuggestion model."""

    def test_suggestion_creation(self) -> None:
        """Test creating knowledge suggestions."""
        suggestion = TaskKnowledgeSuggestion(
            patterns=[("pat-001", 0.92), ("pat-002", 0.85)],
            rules=[("rule-001", 0.88)],
            templates=[("tpl-001", 0.75)],
            past_learnings=[("ep-001", 0.82)],
            error_patterns=[("err-001", 0.90)],
        )
        assert len(suggestion.patterns) == 2
        assert suggestion.patterns[0][1] == 0.92
