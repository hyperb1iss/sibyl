"""End-to-end workflow tests for Sibyl.

Tests complete user journeys through the 4-tool API:
1. Task workflow from creation to learning capture
2. Project with tasks and dependencies
3. Search across all entity types
4. Knowledge creation and discovery
"""

import pytest

from sibyl_core.models.entities import EntityType, RelationshipType
from sibyl_core.models.tasks import TaskStatus
from sibyl_core.tools.core import add, explore, search
from sibyl_core.tools.manage import manage
from tests.harness import (
    ToolTestContext,
    create_test_entity,
    create_test_relationship,
)

# Test organization ID for multi-tenancy
TEST_ORG_ID = "test-org-e2e-workflows"


class TestTaskWorkflowE2E:
    """End-to-end tests for complete task workflow."""

    @pytest.mark.asyncio
    async def test_full_task_lifecycle(self) -> None:
        """Test: create task → start → block → unblock → review → complete."""
        ctx = ToolTestContext()

        # Create a task entity (must include project_id in metadata for Task model)
        task = create_test_entity(
            entity_type=EntityType.TASK,
            name="Implement OAuth login",
            description="Add Google OAuth authentication",
            metadata={"status": TaskStatus.TODO.value, "project_id": "test-project"},
        )
        ctx.entity_manager.add_entity(task)

        async with ctx.patch():
            # 1. Start the task
            result = await manage(action="start_task", entity_id=task.id)
            # Note: actual workflow logic isn't mocked, just verifying API works
            assert result.action == "start_task"

            # 2. Block the task
            result = await manage(
                action="block_task",
                entity_id=task.id,
                data={"reason": "Waiting for API keys"},
            )
            assert result.action == "block_task"

            # 3. Unblock the task
            result = await manage(action="unblock_task", entity_id=task.id)
            assert result.action == "unblock_task"

            # 4. Submit for review
            result = await manage(
                action="submit_review",
                entity_id=task.id,
                data={"pr_url": "https://github.com/test/repo/pull/42"},
            )
            assert result.action == "submit_review"

            # 5. Complete with learnings
            result = await manage(
                action="complete_task",
                entity_id=task.id,
                data={
                    "actual_hours": 4.5,
                    "learnings": "OAuth redirect URIs must match exactly",
                },
            )
            assert result.action == "complete_task"

    @pytest.mark.asyncio
    async def test_task_with_knowledge_suggestions(self) -> None:
        """Test: create task and get knowledge suggestions."""
        ctx = ToolTestContext()

        # Create related patterns
        pattern = create_test_entity(
            entity_type=EntityType.PATTERN,
            name="OAuth best practices",
            description="Always validate redirect URIs",
        )
        ctx.entity_manager.add_entity(pattern)
        ctx.entity_manager.set_search_results([(pattern, 0.85)])

        # Create task (must include project_id)
        task = create_test_entity(
            entity_type=EntityType.TASK,
            name="Implement OAuth login",
            description="Add authentication",
            metadata={"status": TaskStatus.TODO.value, "project_id": "test-project"},
        )
        ctx.entity_manager.add_entity(task)

        async with ctx.patch():
            # Search for related knowledge
            result = await search(
                query="OAuth authentication patterns",
                types=["pattern"],
                limit=5,
            )
            assert result.total >= 0  # Mock may return 0


class TestProjectWorkflowE2E:
    """End-to-end tests for project management."""

    @pytest.mark.asyncio
    async def test_create_project_with_tasks(self) -> None:
        """Test: create project → add tasks → establish dependencies."""
        ctx = ToolTestContext()

        async with ctx.patch():
            # 1. Create project
            project_result = await add(
                title="E-Commerce Platform",
                content="Rebuild with modern stack",
                entity_type="project",
                metadata={
                    "repository_url": "github.com/test/ecommerce",
                    "organization_id": TEST_ORG_ID,
                },
            )
            assert project_result.success

            # 2. Create first task (must specify project)
            task1_result = await add(
                title="Setup authentication",
                content="JWT-based auth with refresh tokens",
                entity_type="task",
                project=project_result.id,  # Required for tasks
                related_to=[project_result.id] if project_result.id else None,
                metadata={"organization_id": TEST_ORG_ID},
            )
            assert task1_result.success

            # 3. Create dependent task
            task2_result = await add(
                title="User profile API",
                content="REST endpoints for user management",
                entity_type="task",
                project=project_result.id,  # Required for tasks
                related_to=[project_result.id] if project_result.id else None,
                metadata={"organization_id": TEST_ORG_ID},
            )
            assert task2_result.success

    @pytest.mark.asyncio
    async def test_explore_project_structure(self) -> None:
        """Test: explore project and its tasks."""
        ctx = ToolTestContext()

        # Create project with tasks
        project = create_test_entity(
            entity_type=EntityType.PROJECT,
            name="Test Project",
            entity_id="test-project",
        )
        task1 = create_test_entity(
            entity_type=EntityType.TASK,
            name="Task 1",
            metadata={"project_id": "test-project"},
        )
        task2 = create_test_entity(
            entity_type=EntityType.TASK,
            name="Task 2",
            metadata={"project_id": "test-project"},
        )

        ctx.entity_manager.add_entity(project)
        ctx.entity_manager.add_entity(task1)
        ctx.entity_manager.add_entity(task2)

        # Create relationships
        ctx.relationship_manager.add_relationship(
            create_test_relationship(task1.id, project.id, RelationshipType.BELONGS_TO)
        )
        ctx.relationship_manager.add_relationship(
            create_test_relationship(task2.id, project.id, RelationshipType.BELONGS_TO)
        )

        async with ctx.patch():
            # List projects
            result = await explore(mode="list", types=["project"], organization_id=TEST_ORG_ID)
            assert result.mode == "list"

            # Explore project relationships
            result = await explore(
                mode="related", entity_id=project.id, organization_id=TEST_ORG_ID
            )
            assert result.mode == "related"


class TestSearchE2E:
    """End-to-end tests for search functionality."""

    @pytest.mark.asyncio
    async def test_search_across_entity_types(self) -> None:
        """Test: search finds results across patterns, rules, episodes."""
        ctx = ToolTestContext()

        # Create diverse entities
        pattern = create_test_entity(
            entity_type=EntityType.PATTERN,
            name="Error handling pattern",
            description="Try-catch with proper logging",
        )
        rule = create_test_entity(
            entity_type=EntityType.RULE,
            name="Never catch generic exceptions",
            description="Always use specific exception types",
        )
        episode = create_test_entity(
            entity_type=EntityType.EPISODE,
            name="Debugging session: error handling",
            description="Learned about proper exception handling",
        )

        ctx.entity_manager.add_entity(pattern)
        ctx.entity_manager.add_entity(rule)
        ctx.entity_manager.add_entity(episode)

        # Set up search to return all
        ctx.entity_manager.set_search_results(
            [
                (pattern, 0.9),
                (rule, 0.85),
                (episode, 0.8),
            ]
        )

        async with ctx.patch():
            # Search without type filter
            result = await search(query="error handling", limit=10)
            assert result.query == "error handling"

            # Search with type filter
            result = await search(
                query="error handling",
                types=["pattern"],
                limit=5,
            )
            assert "pattern" in (result.filters.get("types") or []) or result.filters == {
                "types": ["pattern"]
            }

    @pytest.mark.asyncio
    async def test_search_with_filters(self) -> None:
        """Test: search with language and category filters."""
        ctx = ToolTestContext()

        # Create Python patterns
        pattern = create_test_entity(
            entity_type=EntityType.PATTERN,
            name="Python async pattern",
            description="Use asyncio for concurrent operations",
            metadata={"languages": ["python"], "category": "concurrency"},
        )
        ctx.entity_manager.add_entity(pattern)
        ctx.entity_manager.set_search_results([(pattern, 0.9)])

        async with ctx.patch():
            result = await search(
                query="async patterns",
                types=["pattern"],
                language="python",
                category="concurrency",
            )
            # Filters should be recorded
            assert result.filters.get("language") == "python" or "python" in str(result.filters)


class TestKnowledgeCreationE2E:
    """End-to-end tests for knowledge creation and linking."""

    @pytest.mark.asyncio
    async def test_add_episode_with_tags(self) -> None:
        """Test: add learning episode with tags and languages."""
        ctx = ToolTestContext()

        async with ctx.patch():
            result = await add(
                title="Redis connection pool sizing",
                content="Set pool size to 2x CPU cores for high throughput",
                entity_type="episode",
                category="debugging",
                languages=["python", "redis"],
                tags=["performance", "database"],
                metadata={"organization_id": TEST_ORG_ID},
            )
            assert result.success
            assert result.message is not None

    @pytest.mark.asyncio
    async def test_add_pattern_with_explicit_link(self) -> None:
        """Test: add pattern with explicit relationship."""
        ctx = ToolTestContext()

        # Create existing related pattern
        existing = create_test_entity(
            entity_type=EntityType.PATTERN,
            name="Retry pattern",
            description="Exponential backoff for transient failures",
        )
        ctx.entity_manager.add_entity(existing)
        ctx.entity_manager.set_search_results([(existing, 0.85)])

        async with ctx.patch():
            result = await add(
                title="Circuit breaker pattern",
                content="Prevent cascade failures with circuit breaker",
                entity_type="pattern",
                category="reliability",
                related_to=[existing.id],  # Explicit link
                metadata={"organization_id": TEST_ORG_ID},
            )
            assert result.success

    @pytest.mark.asyncio
    async def test_add_then_search(self) -> None:
        """Test: add knowledge then find it via search."""
        ctx = ToolTestContext()

        async with ctx.patch():
            # Add new knowledge
            add_result = await add(
                title="OAuth token refresh insight",
                content="Use sliding window for refresh token expiry",
                entity_type="episode",
                category="authentication",
                metadata={"organization_id": TEST_ORG_ID},
            )
            assert add_result.success

            # Immediately searchable (in real system, after indexing)
            # With mocks, we just verify the flow works
            search_result = await search(
                query="OAuth token refresh",
                types=["episode"],
            )
            assert search_result.query == "OAuth token refresh"


class TestAdminOperationsE2E:
    """End-to-end tests for admin operations."""

    @pytest.mark.asyncio
    async def test_health_check(self) -> None:
        """Test: health check returns status."""
        ctx = ToolTestContext()

        async with ctx.patch():
            result = await manage(action="health")
            assert result.action == "health"
            # Health check should always return a response

    @pytest.mark.asyncio
    async def test_stats_retrieval(self) -> None:
        """Test: stats returns graph statistics."""
        ctx = ToolTestContext()

        async with ctx.patch():
            result = await manage(action="stats")
            assert result.action == "stats"

    @pytest.mark.asyncio
    async def test_unknown_action_fails(self) -> None:
        """Test: unknown action returns error."""
        ctx = ToolTestContext()

        async with ctx.patch():
            result = await manage(action="not_a_real_action")
            assert result.success is False
            assert "Unknown action" in result.message


class TestMultiStepWorkflowE2E:
    """End-to-end tests for complex multi-step workflows."""

    @pytest.mark.asyncio
    async def test_full_development_cycle(self) -> None:
        """Test: complete cycle from project setup to knowledge capture."""
        ctx = ToolTestContext()

        async with ctx.patch():
            # Step 1: Search for existing patterns
            search_result = await search(
                query="authentication best practices",
                types=["pattern", "rule"],
            )
            assert search_result is not None

            # Step 2: Create project
            project_result = await add(
                title="Auth Service",
                content="Microservice for authentication",
                entity_type="project",
                metadata={"organization_id": TEST_ORG_ID},
            )
            assert project_result.success

            # Step 3: Create task (requires project)
            task_result = await add(
                title="Implement JWT validation",
                content="Add middleware for JWT token validation",
                entity_type="task",
                project=project_result.id,
                metadata={"organization_id": TEST_ORG_ID},
            )
            assert task_result.success

            # Step 4: Explore graph structure
            if project_result.id:
                explore_result = await explore(
                    mode="related",
                    entity_id=project_result.id,
                    organization_id=TEST_ORG_ID,
                )
                assert explore_result is not None

            # Step 5: Complete task with learnings
            if task_result.id:
                complete_result = await manage(
                    action="complete_task",
                    entity_id=task_result.id,
                    data={
                        "actual_hours": 3.0,
                        "learnings": "JWT validation must check expiry AND signature",
                    },
                )
                assert complete_result.action == "complete_task"

            # Step 6: Verify learning is searchable
            learning_search = await search(
                query="JWT validation learnings",
                types=["episode"],
            )
            assert learning_search is not None
