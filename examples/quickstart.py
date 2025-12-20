"""Quick start example for Sibyl MCP tools.

This demonstrates basic usage of all 4 tools:
- search: Find knowledge
- explore: Navigate the graph
- add: Create knowledge
- manage: Handle workflows
"""

import asyncio

from sibyl.tools.core import add, explore, search
from sibyl.tools.manage import manage


async def main():
    """Demonstrate basic Sibyl usage."""

    print("=" * 60)
    print("SIBYL QUICK START")
    print("=" * 60)

    # -------------------------------------------------------------------------
    # 1. Add Knowledge
    # -------------------------------------------------------------------------
    print("\n1. Adding knowledge...")

    # Add an episode (learning)
    result = await add(
        title="Redis connection pool sizing",
        content="""
        For high-throughput apps, set pool size to 2x CPU cores.
        Default of 10 is too small for production workloads.
        Symptoms: intermittent timeouts under load.
        """.strip(),
        entity_type="episode",
        category="debugging",
        languages=["python", "redis"],
        tags=["performance", "connection-pooling"],
    )
    print(f"   Added: {result.message}")

    # Add a pattern
    result = await add(
        title="Retry with exponential backoff",
        content="""
        Always retry transient failures with exponential backoff:
        1. Start with 100ms delay
        2. Double each retry (100, 200, 400, 800...)
        3. Add jitter (random 0-50ms) to prevent thundering herd
        4. Cap at 30 seconds max delay
        5. Give up after 5 retries
        """.strip(),
        entity_type="pattern",
        category="reliability",
        languages=["python", "typescript"],
    )
    print(f"   Added: {result.message}")

    # -------------------------------------------------------------------------
    # 2. Search Knowledge
    # -------------------------------------------------------------------------
    print("\n2. Searching for patterns...")

    results = await search(
        query="retry failures gracefully",
        types=["pattern"],
        limit=5,
    )
    print(f"   Found {results.total} results:")
    for r in results.results:
        print(f"   - [{r.score:.2f}] {r.name}")

    # -------------------------------------------------------------------------
    # 3. Explore the Graph
    # -------------------------------------------------------------------------
    print("\n3. Exploring graph...")

    # List all patterns
    explore_result = await explore(
        mode="list",
        types=["pattern"],
        limit=10,
    )
    print(f"   Found {explore_result.total} patterns")

    # If we have an entity, explore its relationships
    if explore_result.entities:
        entity = explore_result.entities[0]
        print(f"   First pattern: {entity.name}")

        # Find related knowledge
        related = await explore(
            mode="related",
            entity_id=entity.id,
            limit=5,
        )
        print(f"   Related entities: {related.total}")

    # -------------------------------------------------------------------------
    # 4. Manage Operations
    # -------------------------------------------------------------------------
    print("\n4. Running manage operations...")

    # Health check
    health = await manage(action="health")
    print(f"   Health: {health.data.get('status', 'unknown')}")

    # Get stats
    stats = await manage(action="stats")
    if stats.success:
        print(f"   Stats: {stats.data}")

    # -------------------------------------------------------------------------
    # Summary
    # -------------------------------------------------------------------------
    print("\n" + "=" * 60)
    print("QUICK START COMPLETE")
    print("=" * 60)
    print("\nYou've used all 4 Sibyl tools:")
    print("  - search: Semantic discovery across the knowledge graph")
    print("  - explore: Navigate and browse graph structure")
    print("  - add: Create new knowledge entities")
    print("  - manage: Handle workflows and admin operations")
    print("\nSee examples/task_workflow_example.py for task management.")


if __name__ == "__main__":
    asyncio.run(main())
