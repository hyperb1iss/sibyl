"""Graph exploration CLI commands.

Commands for traversing and visualizing the knowledge graph.
"""

from typing import Annotated

import typer

from sibyl.cli.common import (
    CORAL,
    ELECTRIC_PURPLE,
    NEON_CYAN,
    console,
    create_table,
    create_tree,
    error,
    format_status,
    info,
    print_db_hint,
    run_async,
    spinner,
    truncate,
)

app = typer.Typer(
    name="explore",
    help="Graph traversal and exploration",
    no_args_is_help=True,
)


@app.command("related")
def explore_related(
    entity_id: Annotated[str, typer.Argument(help="Starting entity ID")],
    relationship_types: Annotated[str | None, typer.Option("--rel", "-r", help="Relationship types (comma-sep)")] = None,
    limit: Annotated[int, typer.Option("--limit", "-n", help="Max results")] = 20,
) -> None:
    """Find directly connected entities (1-hop)."""

    @run_async
    async def _related() -> None:
        from sibyl.tools.core import explore

        try:
            rel_list = [r.strip() for r in relationship_types.split(",")] if relationship_types else None

            with spinner("Exploring relationships...") as progress:
                progress.add_task("Exploring relationships...", total=None)
                response = await explore(
                    mode="related",
                    entity_id=entity_id,
                    relationship_types=rel_list,
                    limit=limit,
                )

            entities = response.entities or []

            if not entities:
                info("No related entities found")
                return

            table = create_table("Related Entities", "ID", "Name", "Type", "Relationship")
            for e in entities:
                rel = e.metadata.get("relationship_type", "-") if e.metadata else "-"
                table.add_row(
                    e.id[:8] + "...",
                    truncate(e.name, 35),
                    e.type,
                    rel,
                )

            console.print(table)

        except Exception as e:
            error(f"Exploration failed: {e}")
            print_db_hint()

    _related()


@app.command("traverse")
def explore_traverse(
    entity_id: Annotated[str, typer.Argument(help="Starting entity ID")],
    depth: Annotated[int, typer.Option("--depth", "-d", help="Traversal depth (1-3)")] = 2,
    limit: Annotated[int, typer.Option("--limit", "-n", help="Max results")] = 50,
) -> None:
    """Multi-hop graph traversal from an entity."""
    if depth < 1 or depth > 3:
        error("Depth must be between 1 and 3")
        return

    @run_async
    async def _traverse() -> None:
        from sibyl.tools.core import explore

        try:
            with spinner(f"Traversing {depth} hops...") as progress:
                progress.add_task(f"Traversing {depth} hops...", total=None)
                response = await explore(
                    mode="traverse",
                    entity_id=entity_id,
                    depth=depth,
                    limit=limit,
                )

            entities = response.entities or []

            if not entities:
                info("No entities found in traversal")
                return

            # Group by hop distance if available
            by_distance: dict[int, list] = {}
            for e in entities:
                dist = e.metadata.get("distance", 1) if e.metadata else 1
                if dist not in by_distance:
                    by_distance[dist] = []
                by_distance[dist].append(e)

            tree = create_tree(f"Traversal from {entity_id[:8]}...")
            for dist in sorted(by_distance.keys()):
                hop_branch = tree.add(f"[{NEON_CYAN}]Hop {dist}[/{NEON_CYAN}] ({len(by_distance[dist])} entities)")
                for e in by_distance[dist][:10]:  # Limit per hop
                    hop_branch.add(f"[{CORAL}]{e.type}[/{CORAL}] {truncate(e.name, 40)}")
                if len(by_distance[dist]) > 10:
                    hop_branch.add(f"[dim]... and {len(by_distance[dist]) - 10} more[/dim]")

            console.print(tree)
            console.print(f"\n[dim]Total: {len(entities)} entities across {len(by_distance)} hop(s)[/dim]")

        except Exception as e:
            error(f"Traversal failed: {e}")
            print_db_hint()

    _traverse()


@app.command("dependencies")
def explore_dependencies(
    entity_id: Annotated[str | None, typer.Argument(help="Task or Project ID")] = None,
    project: Annotated[str | None, typer.Option("--project", "-p", help="Project ID for all deps")] = None,
) -> None:
    """Show task dependency graph with topological ordering."""
    if not entity_id and not project:
        error("Must specify either entity_id or --project")
        return

    @run_async
    async def _deps() -> None:
        from sibyl.tools.core import explore

        try:
            with spinner("Analyzing dependencies...") as progress:
                progress.add_task("Analyzing dependencies...", total=None)
                response = await explore(
                    mode="dependencies",
                    entity_id=entity_id,
                    project=project,
                )

            entities = response.entities or []

            if not entities:
                info("No dependencies found")
                return

            # Check for circular dependencies warning
            if response.metadata and response.metadata.get("has_cycles"):
                console.print(f"[{CORAL}]Warning: Circular dependencies detected![/{CORAL}]\n")

            console.print(f"[{ELECTRIC_PURPLE}]Dependency Order (execute top to bottom):[/{ELECTRIC_PURPLE}]\n")

            for i, e in enumerate(entities, 1):
                status = e.metadata.get("status", "unknown") if e.metadata else "unknown"
                deps = e.metadata.get("depends_on_count", 0) if e.metadata else 0
                blocks = e.metadata.get("blocks_count", 0) if e.metadata else 0

                dep_info = []
                if deps > 0:
                    dep_info.append(f"deps: {deps}")
                if blocks > 0:
                    dep_info.append(f"blocks: {blocks}")

                dep_str = f" [{CORAL}]({', '.join(dep_info)})[/{CORAL}]" if dep_info else ""

                console.print(
                    f"  {i:3}. [{NEON_CYAN}]{e.id[:8]}[/{NEON_CYAN}] "
                    f"{truncate(e.name, 40)} "
                    f"{format_status(status)}{dep_str}"
                )

            console.print(f"\n[dim]Total: {len(entities)} task(s) in dependency order[/dim]")

        except Exception as e:
            error(f"Dependency analysis failed: {e}")
            print_db_hint()

    _deps()


@app.command("path")
def explore_path(
    from_id: Annotated[str, typer.Argument(help="Starting entity ID")],
    to_id: Annotated[str, typer.Argument(help="Target entity ID")],
    max_depth: Annotated[int, typer.Option("--depth", "-d", help="Max path length")] = 5,
) -> None:
    """Find shortest path between two entities."""

    @run_async
    async def _path() -> None:
        from sibyl.graph.client import get_graph_client

        try:
            with spinner("Finding path...") as progress:
                progress.add_task("Finding path...", total=None)

                client = await get_graph_client()
                # Use Cypher to find shortest path
                query = f"""
                MATCH path = shortestPath((a)-[*1..{max_depth}]-(b))
                WHERE a.uuid = $from_id AND b.uuid = $to_id
                RETURN path, length(path) as path_length
                LIMIT 1
                """
                result = await client.driver.execute_query(query, from_id=from_id, to_id=to_id)

            if not result or not result.result_set:
                info(f"No path found between {from_id[:8]} and {to_id[:8]} (max depth: {max_depth})")
                return

            row = result.result_set[0]
            path_length = row[1] if len(row) > 1 else 0

            console.print(f"\n[{ELECTRIC_PURPLE}]Path Found[/{ELECTRIC_PURPLE}] (length: {path_length})\n")
            console.print(f"  [{NEON_CYAN}]{from_id[:8]}...[/{NEON_CYAN}]")

            for i in range(int(path_length)):
                console.print("      ↓")
                console.print(f"  [{CORAL}]hop {i + 1}[/{CORAL}]")

            console.print("      ↓")
            console.print(f"  [{NEON_CYAN}]{to_id[:8]}...[/{NEON_CYAN}]")

        except Exception as e:
            error(f"Path finding failed: {e}")
            print_db_hint()

    _path()
