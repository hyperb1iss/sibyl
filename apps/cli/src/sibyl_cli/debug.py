"""Debug commands for developer introspection.

Provides tools for debugging and troubleshooting the Sibyl system.
Requires organization OWNER role.
"""

from __future__ import annotations

from typing import Annotated

import typer

from sibyl_cli.client import SibylClientError, get_client
from sibyl_cli.common import (
    CORAL,
    NEON_CYAN,
    console,
    create_table,
    error,
    handle_client_error,
    info,
    print_json,
    run_async,
)

app = typer.Typer(
    name="debug",
    help="Debug tools for development (requires OWNER role)",
    no_args_is_help=True,
)


@app.command("query")
def query(
    cypher: Annotated[
        str,
        typer.Argument(help="Cypher query to execute"),
    ],
    json_output: Annotated[
        bool,
        typer.Option("--json", "-j", help="Output as JSON"),
    ] = False,
) -> None:
    """Execute a read-only Cypher query against the graph.

    Useful for inspecting graph data during development.
    Only read-only queries are permitted (no CREATE, SET, DELETE, etc.).

    Examples:
        sibyl debug query "MATCH (n:Entity) RETURN n.name LIMIT 5"
        sibyl debug query "MATCH (n) RETURN labels(n), count(*)" -j
        sibyl debug query "MATCH (t:Entity {entity_type: 'task'}) RETURN t.name, t.status LIMIT 10"
    """

    @run_async
    async def _run() -> None:
        try:
            async with get_client() as client:
                result = await client.post(
                    "/admin/debug/query",
                    json={"cypher": cypher},
                )

                if result.get("error"):
                    error(f"Query failed: {result['error']}")
                    raise typer.Exit(1)

                rows = result.get("rows", [])
                row_count = result.get("row_count", 0)

                if json_output:
                    print_json(result)
                    return

                if not rows:
                    info("Query returned no results")
                    return

                console.print(f"\n[bold]Query returned {row_count} rows:[/bold]\n")

                # Try to format as table if rows have consistent keys
                if rows and isinstance(rows[0], dict):
                    # Get all unique keys across all rows
                    all_keys = set()
                    for row in rows:
                        all_keys.update(row.keys())
                    columns = sorted(all_keys)

                    if columns:
                        table = create_table(None, *columns)
                        for row in rows:
                            values = [_format_value(row.get(k, "")) for k in columns]
                            table.add_row(*values)
                        console.print(table)
                    else:
                        # Fallback to raw output
                        for row in rows:
                            console.print(f"  {row}")
                else:
                    # Fallback for non-dict rows
                    for row in rows:
                        console.print(f"  {row}")

                console.print()

        except SibylClientError as e:
            if e.status_code == 403:
                error("Access denied - OWNER role required for debug queries")
                raise typer.Exit(1)
            if e.status_code == 400:
                error(f"Invalid query: {e.detail}")
                raise typer.Exit(1)
            handle_client_error(e)

    _run()


def _format_value(value: object) -> str:
    """Format a value for table display."""
    if value is None:
        return "[dim]null[/dim]"
    if isinstance(value, bool):
        return f"[{CORAL}]{value}[/{CORAL}]"
    if isinstance(value, (int, float)):
        return f"[{CORAL}]{value}[/{CORAL}]"
    if isinstance(value, str):
        # Truncate long strings
        if len(value) > 50:
            return value[:47] + "..."
        return value
    if isinstance(value, list):
        return f"[{len(value)} items]"
    if isinstance(value, dict):
        return f"[{len(value)} keys]"
    return str(value)[:50]


@app.command("schema")
def schema(
    json_output: Annotated[
        bool,
        typer.Option("--json", "-j", help="Output as JSON"),
    ] = False,
) -> None:
    """Show entity types and counts in the graph.

    Lists all distinct entity types with their counts.
    """

    @run_async
    async def _run() -> None:
        try:
            async with get_client() as client:
                # Query for entity types and counts
                result = await client.post(
                    "/admin/debug/query",
                    json={
                        "cypher": """
                        MATCH (n:Entity)
                        RETURN n.entity_type AS type, count(*) AS count
                        ORDER BY count DESC
                        """
                    },
                )

                if result.get("error"):
                    error(f"Query failed: {result['error']}")
                    raise typer.Exit(1)

                rows = result.get("rows", [])

                if json_output:
                    print_json(rows)
                    return

                if not rows:
                    info("No entities found in graph")
                    return

                console.print("\n[bold]Entity Types:[/bold]\n")
                table = create_table(None, "Type", "Count")
                for row in rows:
                    table.add_row(
                        f"[{NEON_CYAN}]{row.get('type', 'unknown')}[/{NEON_CYAN}]",
                        f"[{CORAL}]{row.get('count', 0)}[/{CORAL}]",
                    )
                console.print(table)
                console.print()

        except SibylClientError as e:
            if e.status_code == 403:
                error("Access denied - OWNER role required")
                raise typer.Exit(1)
            handle_client_error(e)

    _run()


@app.command("status")
def status(
    json_output: Annotated[
        bool,
        typer.Option("--json", "-j", help="Output as JSON"),
    ] = False,
) -> None:
    """Show comprehensive dev status dashboard.

    Aggregates health from all system components:
    - API server
    - Worker process
    - FalkorDB graph database
    - Job queue
    - Recent errors
    - Active agents

    Examples:
        sibyl debug status
        sibyl debug status --json
    """
    from sibyl_cli.common import ERROR_RED, SUCCESS_GREEN

    @run_async
    async def _run() -> None:
        try:
            async with get_client() as client:
                data = await client.get("/admin/dev-status")

                if json_output:
                    print_json(data)
                    return

                console.print("\n[bold]Sibyl Dev Status[/bold]\n")

                # Component health
                def _health(healthy: bool) -> str:
                    if healthy:
                        return f"[{SUCCESS_GREEN}]healthy[/{SUCCESS_GREEN}]"
                    return f"[{ERROR_RED}]unhealthy[/{ERROR_RED}]"

                console.print(f"  API:      {_health(data.get('api_healthy', False))}")
                console.print(f"  Worker:   {_health(data.get('worker_healthy', False))}")
                console.print(f"  Graph:    {_health(data.get('graph_healthy', False))}")
                console.print(f"  Queue:    {_health(data.get('queue_healthy', False))}")
                console.print()

                # Stats
                uptime = data.get("uptime_seconds", 0)
                hours = int(uptime // 3600)
                mins = int((uptime % 3600) // 60)
                uptime_str = f"{hours}h {mins}m" if hours else f"{mins}m"

                console.print(f"  Uptime:       [{CORAL}]{uptime_str}[/{CORAL}]")
                console.print(f"  Entities:     [{CORAL}]{data.get('entity_count', 0):,}[/{CORAL}]")
                console.print(f"  Queue depth:  [{CORAL}]{data.get('queue_depth', 0)}[/{CORAL}]")
                console.print(f"  Active agents:[{CORAL}]{data.get('active_agents', 0)}[/{CORAL}]")
                console.print()

                # Recent errors
                errors = data.get("recent_errors", [])
                if errors:
                    console.print(f"  [bold]Recent Errors ({len(errors)}):[/bold]")
                    for err in errors[-5:]:  # Show last 5
                        ts = err.get("timestamp", "")[:19]
                        event = err.get("event", "unknown")
                        console.print(f"    [{ERROR_RED}]{ts}[/{ERROR_RED}] {event}")
                    console.print()
                else:
                    console.print(f"  [{SUCCESS_GREEN}]No recent errors[/{SUCCESS_GREEN}]")
                    console.print()

        except SibylClientError as e:
            if e.status_code == 403:
                error("Access denied - OWNER role required")
                raise typer.Exit(1)
            handle_client_error(e)

    _run()
