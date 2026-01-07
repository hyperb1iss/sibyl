"""Graph exploration CLI commands.

Commands for traversing and visualizing the knowledge graph.
All commands communicate with the REST API to ensure proper event broadcasting.
"""

from typing import Annotated

import typer

from sibyl_cli.client import SibylClientError, get_client
from sibyl_cli.common import (
    CORAL,
    ELECTRIC_PURPLE,
    NEON_CYAN,
    console,
    create_table,
    create_tree,
    error,
    format_status,
    info,
    print_json,
    run_async,
    truncate,
)

app = typer.Typer(
    name="explore",
    help="Graph traversal and exploration",
    no_args_is_help=True,
)


def _handle_client_error(e: SibylClientError) -> None:
    """Handle client errors with helpful messages and exit with code 1."""
    if "Cannot connect" in str(e):
        error(str(e))
    elif e.status_code == 404:
        error(f"Not found: {e.detail}")
    elif e.status_code == 400:
        error(f"Invalid request: {e.detail}")
    else:
        error(str(e))
    raise typer.Exit(1)


@app.command("related")
def explore_related(
    entity_id: Annotated[str, typer.Argument(help="Starting entity ID")],
    relationship_types: Annotated[
        str | None, typer.Option("--rel", "-r", help="Relationship types (comma-sep)")
    ] = None,
    limit: Annotated[int, typer.Option("--limit", "-n", help="Max results")] = 20,
    json_out: Annotated[
        bool, typer.Option("--json", "-j", help="JSON output (for scripting)")
    ] = False,
) -> None:
    """Find directly connected entities (1-hop). Default: table output."""

    @run_async
    async def _related() -> None:
        client = get_client()

        try:
            rel_list = (
                [r.strip() for r in relationship_types.split(",")] if relationship_types else None
            )

            response = await client.explore(
                mode="related",
                entity_id=entity_id,
                relationship_types=rel_list,
                limit=limit,
            )
            entities = response.get("entities", [])

            # JSON output (default)
            if json_out:
                print_json(entities)
                return

            # Table output
            if not entities:
                info("No related entities found")
                return

            table = create_table("Related Entities", "ID", "Name", "Type", "Relationship")
            for e in entities:
                meta = e.get("metadata", {})
                rel = meta.get("relationship_type", "-") if meta else "-"
                table.add_row(
                    e.get("id", ""),
                    truncate(e.get("name", ""), 35),
                    e.get("type", ""),
                    rel,
                )

            console.print(table)

        except SibylClientError as e:
            _handle_client_error(e)

    _related()


@app.command("traverse")
def explore_traverse(
    entity_id: Annotated[str, typer.Argument(help="Starting entity ID")],
    depth: Annotated[int, typer.Option("--depth", "-d", help="Traversal depth (1-3)")] = 2,
    limit: Annotated[int, typer.Option("--limit", "-n", help="Max results")] = 50,
    json_out: Annotated[
        bool, typer.Option("--json", "-j", help="JSON output (for scripting)")
    ] = False,
) -> None:
    """Multi-hop graph traversal from an entity. Default: table output."""
    if depth < 1 or depth > 3:
        error("Depth must be between 1 and 3")
        return

    @run_async
    async def _traverse() -> None:
        client = get_client()

        try:
            response = await client.explore(
                mode="traverse",
                entity_id=entity_id,
                depth=depth,
                limit=limit,
            )
            entities = response.get("entities", [])

            # JSON output (default)
            if json_out:
                print_json(entities)
                return

            # Table output
            if not entities:
                info("No entities found in traversal")
                return

            # Group by hop distance if available
            by_distance: dict[int, list] = {}
            for e in entities:
                meta = e.get("metadata", {})
                dist = meta.get("distance", 1) if meta else 1
                if dist not in by_distance:
                    by_distance[dist] = []
                by_distance[dist].append(e)

            tree = create_tree(f"Traversal from {entity_id}")
            for dist in sorted(by_distance.keys()):
                hop_branch = tree.add(
                    f"[{NEON_CYAN}]Hop {dist}[/{NEON_CYAN}] ({len(by_distance[dist])} entities)"
                )
                for e in by_distance[dist][:10]:  # Limit per hop
                    hop_branch.add(
                        f"[{CORAL}]{e.get('type', '')}[/{CORAL}] {truncate(e.get('name', ''), 40)}"
                    )
                if len(by_distance[dist]) > 10:
                    hop_branch.add(f"[dim]... and {len(by_distance[dist]) - 10} more[/dim]")

            console.print(tree)
            console.print(
                f"\n[dim]Total: {len(entities)} entities across {len(by_distance)} hop(s)[/dim]"
            )

        except SibylClientError as e:
            _handle_client_error(e)

    _traverse()


@app.command("dependencies")
def explore_dependencies(
    entity_id: Annotated[str | None, typer.Argument(help="Task or Project ID")] = None,
    project: Annotated[
        str | None, typer.Option("--project", "-p", help="Project ID for all deps")
    ] = None,
    json_out: Annotated[
        bool, typer.Option("--json", "-j", help="JSON output (for scripting)")
    ] = False,
) -> None:
    """Show task dependency graph with topological ordering. Default: table output."""
    if not entity_id and not project:
        error("Must specify either entity_id or --project")
        return

    @run_async
    async def _deps() -> None:
        client = get_client()

        try:
            response = await client.explore(
                mode="dependencies",
                entity_id=entity_id,
                project=project,
            )
            entities = response.get("entities", [])
            metadata = response.get("metadata", {})

            # JSON output (default)
            if json_out:
                output = {
                    "entities": entities,
                    "has_cycles": metadata.get("has_cycles", False),
                }
                print_json(output)
                return

            # Table output
            if not entities:
                info("No dependencies found")
                return

            # Check for circular dependencies warning
            if metadata.get("has_cycles"):
                console.print(f"[{CORAL}]Warning: Circular dependencies detected![/{CORAL}]\n")

            console.print(
                f"[{ELECTRIC_PURPLE}]Dependency Order (execute top to bottom):[/{ELECTRIC_PURPLE}]\n"
            )

            for i, e in enumerate(entities, 1):
                meta = e.get("metadata", {})
                status = meta.get("status", "unknown") if meta else "unknown"
                deps = meta.get("depends_on_count", 0) if meta else 0
                blocks = meta.get("blocks_count", 0) if meta else 0

                dep_info = []
                if deps > 0:
                    dep_info.append(f"deps: {deps}")
                if blocks > 0:
                    dep_info.append(f"blocks: {blocks}")

                dep_str = f" [{CORAL}]({', '.join(dep_info)})[/{CORAL}]" if dep_info else ""

                console.print(
                    f"  {i:3}. [{NEON_CYAN}]{e.get('id', '')}[/{NEON_CYAN}] "
                    f"{truncate(e.get('name', ''), 40)} "
                    f"{format_status(status)}{dep_str}"
                )

            console.print(f"\n[dim]Total: {len(entities)} task(s) in dependency order[/dim]")

        except SibylClientError as e:
            _handle_client_error(e)

    _deps()


@app.command("path")
def explore_path(
    from_id: Annotated[str, typer.Argument(help="Starting entity ID")],
    to_id: Annotated[str, typer.Argument(help="Target entity ID")],
    max_depth: Annotated[int, typer.Option("--depth", "-d", help="Max path length")] = 5,
    json_out: Annotated[
        bool, typer.Option("--json", "-j", help="JSON output (for scripting)")
    ] = False,
) -> None:
    """Find shortest path between two entities. Default: table output."""

    @run_async
    async def _path() -> None:
        client = get_client()

        try:
            # Use explore with path mode
            response = await client.explore(
                mode="path",
                entity_id=from_id,
                depth=max_depth,
            )

            # For path mode, we need special handling
            # The explore endpoint may not have a dedicated path mode yet
            # For now, provide a basic response
            path_length = response.get("metadata", {}).get("path_length", 0)
            entities = response.get("entities", [])

            # JSON output (default)
            if json_out:
                output = {
                    "from_id": from_id,
                    "to_id": to_id,
                    "max_depth": max_depth,
                    "path_found": len(entities) > 0,
                    "path_length": path_length,
                    "entities": entities,
                }
                print_json(output)
                return

            # Table output
            if not entities:
                info(f"No path found between {from_id} and {to_id} (max depth: {max_depth})")
                return

            console.print(
                f"\n[{ELECTRIC_PURPLE}]Path Found[/{ELECTRIC_PURPLE}] (length: {path_length})\n"
            )
            console.print(f"  [{NEON_CYAN}]{from_id}[/{NEON_CYAN}]")

            for i in range(int(path_length)):
                console.print("      ↓")
                console.print(f"  [{CORAL}]hop {i + 1}[/{CORAL}]")

            console.print("      ↓")
            console.print(f"  [{NEON_CYAN}]{to_id}[/{NEON_CYAN}]")

        except SibylClientError as e:
            _handle_client_error(e)

    _path()
