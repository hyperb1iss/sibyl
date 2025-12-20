"""Example usage of the task management system.

This demonstrates the full lifecycle of a task with knowledge integration.
"""

import asyncio
from datetime import UTC, datetime, timedelta

from sibyl.graph.client import GraphClient
from sibyl.graph.entities import EntityManager
from sibyl.graph.relationships import RelationshipManager
from sibyl.models.tasks import (
    Project,
    ProjectStatus,
    Task,
    TaskComplexity,
    TaskPriority,
    TaskStatus,
)
from sibyl.tasks.manager import TaskManager
from sibyl.tasks.workflow import TaskWorkflowEngine


async def main():
    """Demonstrate task management workflow."""

    # Initialize graph client and managers
    graph_client = GraphClient()
    await graph_client.connect()

    entity_manager = EntityManager(graph_client)
    relationship_manager = RelationshipManager(graph_client)
    task_manager = TaskManager(entity_manager, relationship_manager)
    workflow_engine = TaskWorkflowEngine(entity_manager, relationship_manager, graph_client)

    print("=" * 80)
    print("TASK MANAGEMENT SYSTEM EXAMPLE")
    print("=" * 80)

    # -------------------------------------------------------------------------
    # Step 1: Create a Project
    # -------------------------------------------------------------------------
    print("\n1. Creating project...")

    project = Project(
        id="proj_001",
        title="E-Commerce Platform v2",
        description="Rebuild e-commerce platform with modern stack",
        status=ProjectStatus.ACTIVE,
        repository_url="https://github.com/company/ecommerce-v2",
        features=["Authentication", "Product Catalog", "Shopping Cart", "Checkout"],
        tech_stack=["TypeScript", "React", "Node.js", "PostgreSQL", "Redis"],
        knowledge_domains=["authentication", "database", "api", "frontend"],
        team_members=["alice@company.com", "bob@company.com"],
        start_date=datetime.now(UTC),
        target_date=datetime.now(UTC) + timedelta(days=90),
    )

    project_id = await entity_manager.create(project)
    print(f"   Created project: {project.title} [{project_id}]")

    # -------------------------------------------------------------------------
    # Step 2: Create Tasks with Auto-Knowledge Linking
    # -------------------------------------------------------------------------
    print("\n2. Creating tasks with automatic knowledge linking...")

    task1 = Task(
        id="task_001",
        title="Implement JWT authentication",
        description="""
        Add JWT-based authentication to the API.
        - Create login/logout endpoints
        - Implement token generation and validation
        - Add refresh token mechanism
        - Integrate with OAuth providers (Google, GitHub)
        """.strip(),
        status=TaskStatus.TODO,
        priority=TaskPriority.HIGH,
        task_order=10,
        project_id=project_id,
        feature="Authentication",
        domain="authentication",
        technologies=["typescript", "jwt", "oauth2", "passport.js"],
        complexity=TaskComplexity.COMPLEX,
        due_date=datetime.now(UTC) + timedelta(days=5),
        estimated_hours=8.0,
    )

    task1_id = await task_manager.create_task_with_knowledge_links(
        task1,
        auto_link_threshold=0.7
    )
    print(f"   Created task: {task1.title}")

    # Suggest knowledge for task
    suggestions = await task_manager.suggest_task_knowledge(
        task_title=task1.title,
        task_description=task1.description,
        technologies=task1.technologies,
        limit=3
    )

    print("\n   Suggested Knowledge:")
    print(f"   - Patterns: {len(suggestions.patterns)}")
    print(f"   - Rules: {len(suggestions.rules)}")
    print(f"   - Templates: {len(suggestions.templates)}")
    print(f"   - Past Learnings: {len(suggestions.past_learnings)}")

    # Create second task with dependency
    task2 = Task(
        id="task_002",
        title="Build user profile API endpoints",
        description="""
        Create REST API endpoints for user profile management.
        - GET /api/users/:id - Get user profile
        - PUT /api/users/:id - Update profile
        - PATCH /api/users/:id/avatar - Update avatar
        Requires JWT auth middleware from task_001.
        """.strip(),
        status=TaskStatus.TODO,
        priority=TaskPriority.MEDIUM,
        task_order=8,
        project_id=project_id,
        feature="Authentication",
        domain="api",
        technologies=["typescript", "express", "postgresql"],
        complexity=TaskComplexity.MEDIUM,
        estimated_hours=4.0,
    )

    task2_id = await task_manager.create_task_with_knowledge_links(task2)
    print(f"   Created task: {task2.title}")

    # Create dependency: task2 depends on task1
    import uuid

    from sibyl.models.entities import Relationship, RelationshipType

    await relationship_manager.create(Relationship(
        id=str(uuid.uuid4()),
        source_id=task2_id,
        target_id=task1_id,
        relationship_type=RelationshipType.DEPENDS_ON,
        weight=1.0,
        metadata={"blocking": True}
    ))
    print("   Created dependency: task_002 DEPENDS_ON task_001")

    # -------------------------------------------------------------------------
    # Step 3: Start Working on Task
    # -------------------------------------------------------------------------
    print("\n3. Starting task (alice@company.com)...")

    started_task = await workflow_engine.start_task(
        task_id=task1_id,
        assignee="alice@company.com"
    )

    print(f"   Status: {started_task.status}")
    print(f"   Assignee: {started_task.assignees[0]}")
    print(f"   Branch: {started_task.branch_name}")
    print(f"   Started: {started_task.started_at}")

    # -------------------------------------------------------------------------
    # Step 4: Simulate Development Work
    # -------------------------------------------------------------------------
    print("\n4. Simulating development work...")

    # Simulate hitting a blocker
    print("   - Working on JWT implementation...")
    print("   - Encountered blocker: OAuth callback URL mismatch")

    blocked_task = await workflow_engine.block_task(
        task_id=task1_id,
        blocker_description="Google OAuth requires exact redirect URI match including trailing slash"
    )
    print(f"   Status: {blocked_task.status}")

    # Resolve blocker
    print("   - Fixed redirect URI configuration")

    unblocked_task = await workflow_engine.unblock_task(task_id=task1_id)
    print(f"   Status: {unblocked_task.status}")

    # -------------------------------------------------------------------------
    # Step 5: Submit for Review
    # -------------------------------------------------------------------------
    print("\n5. Submitting for code review...")

    reviewed_task = await workflow_engine.submit_for_review(
        task_id=task1_id,
        commit_shas=["abc123", "def456", "ghi789"],
        pr_url="https://github.com/company/ecommerce-v2/pull/42"
    )

    print(f"   Status: {reviewed_task.status}")
    print(f"   PR: {reviewed_task.pr_url}")
    print(f"   Commits: {len(reviewed_task.commit_shas)}")

    # -------------------------------------------------------------------------
    # Step 6: Complete Task with Learnings
    # -------------------------------------------------------------------------
    print("\n6. Completing task...")

    learnings = """
    Key learnings from implementing JWT authentication:

    1. **OAuth Redirect URIs**: Google OAuth requires exact URI matching,
       including trailing slashes. Spent 2 hours debugging this - the error
       message was cryptic ("redirect_uri_mismatch").

    2. **Token Expiry**: Set access token expiry to 15 minutes and refresh
       token to 7 days. This balances security with user experience.

    3. **Passport.js Sessions**: Needed to disable session middleware for
       JWT-based auth. The default Passport examples assume sessions which
       we don't use in our stateless API.

    4. **Error Handling**: Implemented proper error responses for expired
       tokens vs invalid tokens - client needs different handling for each.

    5. **Testing**: Created test fixtures for JWT tokens to make integration
       tests easier. Stored in tests/fixtures/tokens.ts
    """

    completed_task = await workflow_engine.complete_task(
        task_id=task1_id,
        actual_hours=6.5,
        learnings=learnings.strip()
    )

    print(f"   Status: {completed_task.status}")
    print(f"   Actual time: {completed_task.actual_hours} hours")
    print(f"   Estimated: {completed_task.estimated_hours} hours")
    print(f"   Accuracy: {(completed_task.estimated_hours / completed_task.actual_hours) * 100:.1f}%")
    print("   Learning episode created from task")

    # -------------------------------------------------------------------------
    # Step 7: Find Similar Tasks
    # -------------------------------------------------------------------------
    print("\n7. Finding similar completed tasks...")

    similar_tasks = await task_manager.find_similar_tasks(
        task2,  # User profile API task
        status_filter=[TaskStatus.DONE],
        limit=5
    )

    print(f"   Found {len(similar_tasks)} similar completed tasks:")
    for similar_task, similarity in similar_tasks:
        print(f"   - [{similarity:.2f}] {similar_task.title}")

    # -------------------------------------------------------------------------
    # Step 8: Estimate Effort for New Task
    # -------------------------------------------------------------------------
    print("\n8. Estimating effort for task_002...")

    estimate = await task_manager.estimate_task_effort(task2)

    print(f"   Estimated hours: {estimate.estimated_hours}")
    print(f"   Confidence: {estimate.confidence:.0%}")
    print(f"   Based on {estimate.based_on_tasks} similar tasks")

    if estimate.similar_tasks:
        print("   Similar tasks used:")
        for similar in estimate.similar_tasks:
            print(f"   - {similar['title']}: {similar['hours']}h (similarity: {similar['similarity']:.2f})")

    # -------------------------------------------------------------------------
    # Step 9: Check Dependencies
    # -------------------------------------------------------------------------
    print("\n9. Checking task dependencies...")

    dependencies = await task_manager.get_task_dependencies(task2_id)
    print(f"   Task '{task2.title}' depends on:")
    for dep_task, rel_type in dependencies:
        print(f"   - {dep_task.title} ({dep_task.status}) [{rel_type}]")

    blocking_tasks = await task_manager.get_blocking_tasks(task1_id)
    print(f"\n   Task '{task1.title}' is blocking:")
    for blocked_task in blocking_tasks:
        print(f"   - {blocked_task.title} ({blocked_task.status})")

    # -------------------------------------------------------------------------
    # Step 10: Query Past Learnings
    # -------------------------------------------------------------------------
    print("\n10. Querying past learnings about OAuth...")

    # This would use the episode created from task completion
    from sibyl.models.entities import EntityType

    oauth_learnings = await entity_manager.search(
        query="OAuth redirect URI error troubleshooting",
        entity_types=[EntityType.EPISODE],
        limit=5
    )

    print(f"   Found {len(oauth_learnings)} relevant learnings:")
    for episode_entity, score in oauth_learnings:
        print(f"   - [{score:.2f}] {episode_entity.name}")
        if episode_entity.metadata.get("task_id"):
            print(f"     From task: {episode_entity.metadata['task_id']}")

    # -------------------------------------------------------------------------
    # Summary
    # -------------------------------------------------------------------------
    print("\n" + "=" * 80)
    print("WORKFLOW COMPLETE")
    print("=" * 80)
    print(f"\nProject: {project.title}")
    print("Tasks created: 2")
    print("Tasks completed: 1")
    print("Learning episodes: 1")
    print("Knowledge links: Auto-created based on semantic similarity")
    print("\nNext steps:")
    print(f"- Task '{task2.title}' ready to start (dependency completed)")
    print(f"- Estimated effort: {estimate.estimated_hours}h")
    print("- Past learnings available for reference")


if __name__ == "__main__":
    asyncio.run(main())
