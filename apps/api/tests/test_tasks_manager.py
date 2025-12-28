"""Tests for TaskManager class."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from sibyl.models.entities import Entity, EntityType, Relationship, RelationshipType
from sibyl.models.tasks import Task, TaskStatus
from sibyl.tasks.manager import TaskManager


@pytest.fixture
def mock_entity_manager() -> MagicMock:
    """Create a mock EntityManager."""
    manager = MagicMock()
    manager.create = AsyncMock(return_value="task_123")
    manager.search = AsyncMock(return_value=[])
    manager.get = AsyncMock()
    return manager


@pytest.fixture
def mock_relationship_manager() -> MagicMock:
    """Create a mock RelationshipManager."""
    manager = MagicMock()
    manager.create = AsyncMock()
    manager.get_for_entity = AsyncMock(return_value=[])
    return manager


@pytest.fixture
def task_manager(
    mock_entity_manager: MagicMock, mock_relationship_manager: MagicMock
) -> TaskManager:
    """Create a TaskManager with mocked dependencies."""
    return TaskManager(mock_entity_manager, mock_relationship_manager)


@pytest.fixture
def sample_task() -> Task:
    """Create a sample task for testing."""
    return Task(
        id="task_sample",
        entity_type=EntityType.TASK,
        title="Implement OAuth",
        name="Implement OAuth",
        description="Add OAuth2 authentication flow",
        content="Full implementation of OAuth2 with refresh tokens",
        status=TaskStatus.TODO,
        priority="high",
        project_id="proj_123",
        domain="authentication",
        technologies=["python", "oauth2", "redis"],
    )


@pytest.fixture
def sample_entity() -> Entity:
    """Create a sample entity for testing."""
    return Entity(
        id="entity_123",
        entity_type=EntityType.PATTERN,
        name="Auth Pattern",
        description="Authentication pattern",
        content="Best practices for auth",
    )


class TestTaskManagerInit:
    """Tests for TaskManager initialization."""

    def test_init_stores_managers(
        self, mock_entity_manager: MagicMock, mock_relationship_manager: MagicMock
    ) -> None:
        """Should store entity and relationship managers."""
        manager = TaskManager(mock_entity_manager, mock_relationship_manager)
        assert manager._entity_manager is mock_entity_manager
        assert manager._relationship_manager is mock_relationship_manager


class TestCreateTaskWithKnowledgeLinks:
    """Tests for create_task_with_knowledge_links method."""

    @pytest.mark.asyncio
    async def test_creates_task(
        self, task_manager: TaskManager, sample_task: Task, mock_entity_manager: MagicMock
    ) -> None:
        """Should create the task entity."""
        result = await task_manager.create_task_with_knowledge_links(sample_task)

        assert result == "task_123"
        mock_entity_manager.create.assert_called_once_with(sample_task)

    @pytest.mark.asyncio
    async def test_searches_for_related_knowledge(
        self, task_manager: TaskManager, sample_task: Task, mock_entity_manager: MagicMock
    ) -> None:
        """Should search for related knowledge entities."""
        await task_manager.create_task_with_knowledge_links(sample_task)

        # Should search for patterns, rules, templates, episodes (first call)
        mock_entity_manager.search.assert_called()
        first_call = mock_entity_manager.search.call_args_list[0]
        assert EntityType.PATTERN in first_call.kwargs["entity_types"]
        assert EntityType.RULE in first_call.kwargs["entity_types"]

    @pytest.mark.asyncio
    async def test_auto_links_high_relevance_knowledge(
        self,
        task_manager: TaskManager,
        sample_task: Task,
        mock_entity_manager: MagicMock,
        mock_relationship_manager: MagicMock,
        sample_entity: Entity,
    ) -> None:
        """Should create relationships for high-relevance knowledge."""
        # Mock search returning high-relevance entity
        mock_entity_manager.search = AsyncMock(
            return_value=[(sample_entity, 0.85)]  # Above threshold
        )

        await task_manager.create_task_with_knowledge_links(sample_task)

        # Should create relationship for high-relevance match
        mock_relationship_manager.create.assert_called()

    @pytest.mark.asyncio
    async def test_skips_low_relevance_knowledge(
        self,
        task_manager: TaskManager,
        sample_task: Task,
        mock_entity_manager: MagicMock,
        mock_relationship_manager: MagicMock,
        sample_entity: Entity,
    ) -> None:
        """Should not link low-relevance knowledge."""
        # Mock search returning low-relevance entity (below default 0.75 threshold)
        mock_entity_manager.search = AsyncMock(return_value=[(sample_entity, 0.50)])
        # Remove project_id to avoid project link
        sample_task.project_id = None
        sample_task.domain = None

        await task_manager.create_task_with_knowledge_links(sample_task)

        # Should not create any relationships
        mock_relationship_manager.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_links_to_domain_topic(
        self,
        task_manager: TaskManager,
        sample_task: Task,
        mock_entity_manager: MagicMock,
        mock_relationship_manager: MagicMock,
    ) -> None:
        """Should link to domain topic if specified."""
        topic_entity = Entity(
            id="topic_auth",
            entity_type=EntityType.TOPIC,
            name="Authentication",
            description="Auth topic",
            content="",
        )

        # First call returns empty (knowledge search), second call returns topic
        mock_entity_manager.search = AsyncMock(side_effect=[[], [(topic_entity, 1.0)]])

        await task_manager.create_task_with_knowledge_links(sample_task)

        # Should search for topic and create relationship
        assert mock_entity_manager.search.call_count >= 2
        mock_relationship_manager.create.assert_called()

    @pytest.mark.asyncio
    async def test_links_to_project(
        self,
        task_manager: TaskManager,
        sample_task: Task,
        mock_entity_manager: MagicMock,
        mock_relationship_manager: MagicMock,
    ) -> None:
        """Should link to project if project_id specified."""
        sample_task.domain = None  # Skip domain linking

        await task_manager.create_task_with_knowledge_links(sample_task)

        # Should create BELONGS_TO relationship to project
        mock_relationship_manager.create.assert_called()
        call = mock_relationship_manager.create.call_args
        rel = call[0][0]
        assert rel.target_id == "proj_123"
        assert rel.relationship_type == RelationshipType.BELONGS_TO

    @pytest.mark.asyncio
    async def test_custom_threshold(
        self,
        task_manager: TaskManager,
        sample_task: Task,
        mock_entity_manager: MagicMock,
        mock_relationship_manager: MagicMock,
        sample_entity: Entity,
    ) -> None:
        """Should respect custom auto_link_threshold."""
        sample_task.project_id = None
        sample_task.domain = None

        # 0.60 is below default 0.75 but above custom 0.50
        mock_entity_manager.search = AsyncMock(return_value=[(sample_entity, 0.60)])

        await task_manager.create_task_with_knowledge_links(sample_task, auto_link_threshold=0.50)

        # Should create relationship with lower threshold
        mock_relationship_manager.create.assert_called()


class TestSuggestTaskKnowledge:
    """Tests for suggest_task_knowledge method."""

    @pytest.mark.asyncio
    async def test_returns_suggestion_structure(
        self, task_manager: TaskManager, mock_entity_manager: MagicMock
    ) -> None:
        """Should return TaskKnowledgeSuggestion with all categories."""
        result = await task_manager.suggest_task_knowledge(
            task_title="Fix auth bug",
            task_description="Token refresh fails",
            technologies=["python"],
        )

        assert hasattr(result, "patterns")
        assert hasattr(result, "rules")
        assert hasattr(result, "templates")
        assert hasattr(result, "past_learnings")
        assert hasattr(result, "error_patterns")

    @pytest.mark.asyncio
    async def test_searches_all_knowledge_types(
        self, task_manager: TaskManager, mock_entity_manager: MagicMock
    ) -> None:
        """Should search for patterns, rules, templates, episodes, error_patterns."""
        await task_manager.suggest_task_knowledge(
            task_title="Implement feature",
            task_description="New feature",
            technologies=["python"],
        )

        # Should make 5 search calls (one per knowledge type)
        assert mock_entity_manager.search.call_count == 5

    @pytest.mark.asyncio
    async def test_formats_results_as_id_score_tuples(
        self, task_manager: TaskManager, mock_entity_manager: MagicMock
    ) -> None:
        """Should format results as (id, score) tuples."""
        pattern = Entity(
            id="pattern_1",
            entity_type=EntityType.PATTERN,
            name="Pattern",
            description="",
            content="",
        )
        mock_entity_manager.search = AsyncMock(return_value=[(pattern, 0.9)])

        result = await task_manager.suggest_task_knowledge(
            task_title="Test", task_description="", technologies=[]
        )

        # Patterns should be list of (id, score) tuples
        assert result.patterns == [("pattern_1", 0.9)]

    @pytest.mark.asyncio
    async def test_respects_limit(
        self, task_manager: TaskManager, mock_entity_manager: MagicMock
    ) -> None:
        """Should pass limit to search calls."""
        await task_manager.suggest_task_knowledge(
            task_title="Test",
            task_description="",
            technologies=[],
            limit=3,
        )

        # All search calls should use the specified limit
        for call in mock_entity_manager.search.call_args_list:
            assert call.kwargs.get("limit") == 3


class TestFindSimilarTasks:
    """Tests for find_similar_tasks method."""

    @pytest.mark.asyncio
    async def test_returns_similar_tasks(
        self, task_manager: TaskManager, sample_task: Task, mock_entity_manager: MagicMock
    ) -> None:
        """Should return list of similar tasks with scores."""
        similar_entity = Entity(
            id="task_other",
            entity_type=EntityType.TASK,
            name="Similar Task",
            description="Related task",
            content="",
            metadata={"status": "todo", "priority": "medium"},
        )
        mock_entity_manager.search = AsyncMock(return_value=[(similar_entity, 0.85)])

        result = await task_manager.find_similar_tasks(sample_task)

        assert len(result) == 1
        assert result[0][1] == 0.85  # Score

    @pytest.mark.asyncio
    async def test_excludes_self(
        self, task_manager: TaskManager, sample_task: Task, mock_entity_manager: MagicMock
    ) -> None:
        """Should exclude the task itself from results."""
        # Return the same task in search results
        self_entity = Entity(
            id=sample_task.id,
            entity_type=EntityType.TASK,
            name=sample_task.name,
            description=sample_task.description,
            content="",
            metadata={"status": "todo"},
        )
        mock_entity_manager.search = AsyncMock(return_value=[(self_entity, 1.0)])

        result = await task_manager.find_similar_tasks(sample_task)

        assert len(result) == 0

    @pytest.mark.asyncio
    async def test_filters_by_status(
        self, task_manager: TaskManager, sample_task: Task, mock_entity_manager: MagicMock
    ) -> None:
        """Should filter by task status if specified."""
        done_entity = Entity(
            id="task_done",
            entity_type=EntityType.TASK,
            name="Done Task",
            description="",
            content="",
            metadata={"status": "done"},
        )
        todo_entity = Entity(
            id="task_todo",
            entity_type=EntityType.TASK,
            name="Todo Task",
            description="",
            content="",
            metadata={"status": "todo"},
        )
        mock_entity_manager.search = AsyncMock(
            return_value=[(done_entity, 0.9), (todo_entity, 0.8)]
        )

        result = await task_manager.find_similar_tasks(sample_task, status_filter=[TaskStatus.DONE])

        assert len(result) == 1
        assert result[0][0].status == TaskStatus.DONE

    @pytest.mark.asyncio
    async def test_respects_limit(
        self, task_manager: TaskManager, sample_task: Task, mock_entity_manager: MagicMock
    ) -> None:
        """Should respect limit parameter."""
        entities = [
            Entity(
                id=f"task_{i}",
                entity_type=EntityType.TASK,
                name=f"Task {i}",
                description="",
                content="",
                metadata={"status": "todo"},
            )
            for i in range(10)
        ]
        mock_entity_manager.search = AsyncMock(
            return_value=[(e, 0.9 - i * 0.05) for i, e in enumerate(entities)]
        )

        result = await task_manager.find_similar_tasks(sample_task, limit=3)

        assert len(result) == 3


class TestEstimateTaskEffort:
    """Tests for estimate_task_effort method."""

    @pytest.mark.asyncio
    async def test_no_similar_tasks(
        self, task_manager: TaskManager, sample_task: Task, mock_entity_manager: MagicMock
    ) -> None:
        """Should return no estimate when no similar tasks found."""
        mock_entity_manager.search = AsyncMock(return_value=[])

        result = await task_manager.estimate_task_effort(sample_task)

        assert result.estimated_hours is None
        assert result.confidence == 0.0
        assert "No similar" in result.reason

    @pytest.mark.asyncio
    async def test_similar_tasks_no_time_tracking(
        self, task_manager: TaskManager, sample_task: Task, mock_entity_manager: MagicMock
    ) -> None:
        """Should return no estimate when similar tasks have no time tracking."""
        # Similar task without actual_hours
        entity = Entity(
            id="task_other",
            entity_type=EntityType.TASK,
            name="Similar",
            description="",
            content="",
            metadata={"status": "done", "actual_hours": None},
        )
        mock_entity_manager.search = AsyncMock(return_value=[(entity, 0.8)])

        result = await task_manager.estimate_task_effort(sample_task)

        assert result.estimated_hours is None
        assert "time tracking" in result.reason

    @pytest.mark.asyncio
    async def test_calculates_weighted_average(
        self, task_manager: TaskManager, sample_task: Task, mock_entity_manager: MagicMock
    ) -> None:
        """Should calculate weighted average of similar task hours."""
        entities = [
            Entity(
                id="task_1",
                entity_type=EntityType.TASK,
                name="Task 1",
                description="",
                content="",
                metadata={"status": "done", "actual_hours": 4.0},
            ),
            Entity(
                id="task_2",
                entity_type=EntityType.TASK,
                name="Task 2",
                description="",
                content="",
                metadata={"status": "done", "actual_hours": 8.0},
            ),
        ]
        # Higher similarity weight for first task
        mock_entity_manager.search = AsyncMock(
            return_value=[(entities[0], 0.9), (entities[1], 0.6)]
        )

        result = await task_manager.estimate_task_effort(sample_task)

        # Weighted avg: (4*0.9 + 8*0.6) / (0.9+0.6) = 8.4/1.5 = 5.6
        assert result.estimated_hours is not None
        assert result.estimated_hours == 5.6
        assert result.based_on_tasks == 2

    @pytest.mark.asyncio
    async def test_includes_similar_tasks_info(
        self, task_manager: TaskManager, sample_task: Task, mock_entity_manager: MagicMock
    ) -> None:
        """Should include info about similar tasks used for estimate."""
        entity = Entity(
            id="task_ref",
            entity_type=EntityType.TASK,
            name="Reference Task",
            description="",
            content="",
            metadata={"status": "done", "actual_hours": 5.0},
        )
        mock_entity_manager.search = AsyncMock(return_value=[(entity, 0.85)])

        result = await task_manager.estimate_task_effort(sample_task)

        assert len(result.similar_tasks) >= 1
        assert result.similar_tasks[0]["id"] == "task_ref"
        assert result.similar_tasks[0]["hours"] == 5.0


class TestGetTaskDependencies:
    """Tests for get_task_dependencies method."""

    @pytest.mark.asyncio
    async def test_returns_dependencies(
        self,
        task_manager: TaskManager,
        mock_relationship_manager: MagicMock,
        mock_entity_manager: MagicMock,
    ) -> None:
        """Should return list of dependency tasks."""
        rel = Relationship(
            id="rel_1",
            source_id="task_main",
            target_id="task_dep",
            relationship_type=RelationshipType.DEPENDS_ON,
        )
        dep_entity = Entity(
            id="task_dep",
            entity_type=EntityType.TASK,
            name="Dependency Task",
            description="",
            content="",
            metadata={"status": "done"},
        )
        mock_relationship_manager.get_for_entity = AsyncMock(return_value=[rel])
        mock_entity_manager.get = AsyncMock(return_value=dep_entity)

        result = await task_manager.get_task_dependencies("task_main")

        assert len(result) == 1
        assert result[0][0].id == "task_dep"
        assert result[0][1] == "DEPENDS_ON"

    @pytest.mark.asyncio
    async def test_queries_outgoing_depends_on(
        self,
        task_manager: TaskManager,
        mock_relationship_manager: MagicMock,
    ) -> None:
        """Should query outgoing DEPENDS_ON relationships."""
        await task_manager.get_task_dependencies("task_123")

        mock_relationship_manager.get_for_entity.assert_called_once()
        call_args = mock_relationship_manager.get_for_entity.call_args
        assert call_args[0][0] == "task_123"
        assert RelationshipType.DEPENDS_ON in call_args.kwargs["relationship_types"]
        assert call_args.kwargs["direction"] == "outgoing"


class TestGetBlockingTasks:
    """Tests for get_blocking_tasks method."""

    @pytest.mark.asyncio
    async def test_returns_blocked_tasks(
        self,
        task_manager: TaskManager,
        mock_relationship_manager: MagicMock,
        mock_entity_manager: MagicMock,
    ) -> None:
        """Should return list of tasks blocked by this task."""
        rel = Relationship(
            id="rel_1",
            source_id="task_blocked",
            target_id="task_main",
            relationship_type=RelationshipType.DEPENDS_ON,
        )
        blocked_entity = Entity(
            id="task_blocked",
            entity_type=EntityType.TASK,
            name="Blocked Task",
            description="",
            content="",
            metadata={"status": "todo"},
        )
        mock_relationship_manager.get_for_entity = AsyncMock(return_value=[rel])
        mock_entity_manager.get = AsyncMock(return_value=blocked_entity)

        result = await task_manager.get_blocking_tasks("task_main")

        assert len(result) == 1
        assert result[0].id == "task_blocked"

    @pytest.mark.asyncio
    async def test_queries_incoming_depends_on(
        self,
        task_manager: TaskManager,
        mock_relationship_manager: MagicMock,
    ) -> None:
        """Should query incoming DEPENDS_ON relationships."""
        await task_manager.get_blocking_tasks("task_123")

        mock_relationship_manager.get_for_entity.assert_called_once()
        call_args = mock_relationship_manager.get_for_entity.call_args
        assert call_args[0][0] == "task_123"
        assert RelationshipType.DEPENDS_ON in call_args.kwargs["relationship_types"]
        assert call_args.kwargs["direction"] == "incoming"


class TestDetermineRelationshipType:
    """Tests for _determine_relationship_type method."""

    def test_pattern_maps_to_references(self, task_manager: TaskManager) -> None:
        """PATTERN should map to REFERENCES."""
        result = task_manager._determine_relationship_type(EntityType.PATTERN)
        assert result == RelationshipType.REFERENCES

    def test_rule_maps_to_requires(self, task_manager: TaskManager) -> None:
        """RULE should map to REQUIRES."""
        result = task_manager._determine_relationship_type(EntityType.RULE)
        assert result == RelationshipType.REQUIRES

    def test_template_maps_to_references(self, task_manager: TaskManager) -> None:
        """TEMPLATE should map to REFERENCES."""
        result = task_manager._determine_relationship_type(EntityType.TEMPLATE)
        assert result == RelationshipType.REFERENCES

    def test_episode_maps_to_references(self, task_manager: TaskManager) -> None:
        """EPISODE should map to REFERENCES."""
        result = task_manager._determine_relationship_type(EntityType.EPISODE)
        assert result == RelationshipType.REFERENCES

    def test_error_pattern_maps_to_references(self, task_manager: TaskManager) -> None:
        """ERROR_PATTERN should map to REFERENCES."""
        result = task_manager._determine_relationship_type(EntityType.ERROR_PATTERN)
        assert result == RelationshipType.REFERENCES

    def test_unknown_maps_to_related_to(self, task_manager: TaskManager) -> None:
        """Unknown types should map to RELATED_TO."""
        result = task_manager._determine_relationship_type(EntityType.PROJECT)
        assert result == RelationshipType.RELATED_TO


class TestEntityToTask:
    """Tests for _entity_to_task method."""

    def test_converts_basic_fields(self, task_manager: TaskManager) -> None:
        """Should convert basic entity fields to task."""
        entity = Entity(
            id="entity_123",
            entity_type=EntityType.TASK,
            name="Test Task",
            description="Description",
            content="Content",
            created_at=datetime(2024, 1, 1, tzinfo=UTC),
            updated_at=datetime(2024, 1, 2, tzinfo=UTC),
            metadata={},
        )

        result = task_manager._entity_to_task(entity)

        assert result.id == "entity_123"
        assert result.title == "Test Task"
        assert result.description == "Description"
        assert result.created_at == datetime(2024, 1, 1, tzinfo=UTC)

    def test_extracts_metadata_fields(self, task_manager: TaskManager) -> None:
        """Should extract task-specific fields from metadata."""
        entity = Entity(
            id="entity_123",
            entity_type=EntityType.TASK,
            name="Test Task",
            description="",
            content="",
            metadata={
                "status": "doing",
                "priority": "high",
                "project_id": "proj_456",
                "feature": "auth",
                "assignees": ["alice", "bob"],
                "technologies": ["python", "redis"],
                "actual_hours": 4.5,
            },
        )

        result = task_manager._entity_to_task(entity)

        assert result.status == TaskStatus.DOING
        assert result.priority == "high"
        assert result.project_id == "proj_456"
        assert result.feature == "auth"
        assert result.assignees == ["alice", "bob"]
        assert result.technologies == ["python", "redis"]
        assert result.actual_hours == 4.5

    def test_uses_defaults_for_missing_fields(self, task_manager: TaskManager) -> None:
        """Should use defaults for missing metadata fields."""
        entity = Entity(
            id="entity_123",
            entity_type=EntityType.TASK,
            name="Test",
            description="",
            content="",
            metadata={},
        )

        result = task_manager._entity_to_task(entity)

        assert result.status == TaskStatus.TODO
        assert result.priority == "medium"
        assert result.assignees == []
        assert result.technologies == []
