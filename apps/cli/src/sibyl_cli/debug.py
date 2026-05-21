"""Debug commands for developer introspection.

Provides tools for debugging and troubleshooting the Sibyl system.
Requires organization OWNER role.
"""

from __future__ import annotations

from typing import Annotated, cast

import typer
from rich.table import Table

from sibyl_cli.client import SibylClientError, get_client
from sibyl_cli.common import (
    CORAL,
    ELECTRIC_YELLOW,
    NEON_CYAN,
    console,
    create_table,
    error,
    handle_client_error,
    info,
    print_json,
    run_async,
)
from sibyl_cli.pending_writes import pending_write_status

app = typer.Typer(
    name="debug",
    help="Debug tools for development (requires OWNER role)",
    no_args_is_help=True,
)


@app.command("query")
def query(
    query_text: Annotated[
        str,
        typer.Argument(help="Read-only graph query to execute"),
    ],
    json_output: Annotated[
        bool,
        typer.Option("--json", "-j", help="Output as JSON"),
    ] = False,
    explain: Annotated[
        bool,
        typer.Option("--explain", help="Run EXPLAIN ANALYZE FORMAT JSON"),
    ] = False,
) -> None:
    """Execute a read-only graph query against the active runtime.

    Use SurrealQL when running the Surreal runtime. Only read-only queries are
    permitted.

    Examples:
        sibyl debug query "SELECT name, entity_type FROM entity LIMIT 5;"
        sibyl debug query --explain "SELECT name FROM entity LIMIT 5;"
        sibyl debug query "SELECT entity_type, count() AS count FROM entity GROUP BY entity_type;" -j
        sibyl debug query "SELECT name, metadata.status AS status FROM entity WHERE entity_type = 'task' LIMIT 10;"
    """
    cypher = f"EXPLAIN ANALYZE FORMAT JSON {query_text}" if explain else query_text

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

                if explain:
                    _print_explain_plan(rows, row_count)
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
                raise typer.Exit(1) from None
            if e.status_code == 400:
                error(f"Invalid query: {e.detail}")
                raise typer.Exit(1) from None
            handle_client_error(e)

    _run()


def _print_explain_plan(rows: list[object], row_count: int) -> None:
    nodes = _explain_nodes(rows)
    if not nodes:
        print_json(rows)
        return

    console.print(f"\n[bold]Explain returned {row_count} rows:[/bold]\n")
    table = Table(
        box=None,
        show_header=True,
        header_style=f"bold {NEON_CYAN}",
        pad_edge=False,
    )
    table.add_column("Operator", style=NEON_CYAN)
    table.add_column("Context")
    table.add_column("Rows", justify="right", style=CORAL)
    table.add_column("Batches", justify="right", style=CORAL)
    table.add_column("Elapsed", justify="right", style=ELECTRIC_YELLOW)
    table.add_column("Details")

    for node in nodes:
        raw_metrics = node.get("metrics")
        metrics = cast(dict[str, object], raw_metrics) if isinstance(raw_metrics, dict) else {}
        table.add_row(
            str(node.get("operator") or "unknown"),
            str(node.get("context") or ""),
            _metric_value(metrics.get("output_rows")),
            _metric_value(metrics.get("output_batches")),
            _elapsed_value(metrics.get("elapsed_ns")),
            _explain_details(node),
        )
    console.print(table)
    console.print()


def _explain_nodes(rows: list[object]) -> list[dict[str, object]]:
    nodes: list[dict[str, object]] = []
    for row in rows:
        nodes.extend(_coerce_explain_node(row))
    return nodes


def _coerce_explain_node(value: object) -> list[dict[str, object]]:
    if isinstance(value, dict):
        node = cast(dict[str, object], value)
        if isinstance(node.get("operator"), str):
            return [node]
        raw_value = node.get("value")
        if set(node) == {"value"}:
            return _coerce_explain_node(raw_value)
        if isinstance(raw_value, (dict, list)):
            return _coerce_explain_node(raw_value)
    if isinstance(value, list):
        nodes: list[dict[str, object]] = []
        for item in value:
            nodes.extend(_coerce_explain_node(item))
        return nodes
    return []


def _metric_value(value: object) -> str:
    if value is None:
        return "[dim]-[/dim]"
    return str(value)


def _elapsed_value(value: object) -> str:
    if not isinstance(value, int | float):
        return "[dim]-[/dim]"
    if value < 1_000:
        return f"{value:g}ns"
    if value < 1_000_000:
        return f"{value / 1_000:g}us"
    if value < 1_000_000_000:
        return f"{value / 1_000_000:g}ms"
    return f"{value / 1_000_000_000:g}s"


def _explain_details(node: dict[str, object]) -> str:
    parts: list[str] = []
    attributes = node.get("attributes")
    if isinstance(attributes, dict):
        for key, value in sorted(attributes.items()):
            parts.append(f"{key}={_short_detail(value)}")
    expressions = node.get("expressions")
    if isinstance(expressions, list):
        for expression in expressions[:2]:
            if isinstance(expression, dict):
                role = expression.get("role") or "expr"
                sql = expression.get("sql")
                if sql:
                    parts.append(f"{role}={_short_detail(sql)}")
    total_rows = node.get("total_rows")
    if total_rows is not None:
        parts.append(f"total_rows={total_rows}")
    return ", ".join(parts) if parts else "[dim]-[/dim]"


def _short_detail(value: object) -> str:
    text = str(value)
    if len(text) > 60:
        return text[:57] + "..."
    return text


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
                        SELECT entity_type AS type, count() AS count
                        FROM entity
                        GROUP BY entity_type
                        ORDER BY count DESC;
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
                raise typer.Exit(1) from None
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
    - Active graph database runtime
    - Job queue
    - Recent errors

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
                data["pending_writes"] = pending_write_status()

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
                console.print(
                    f"  Coord:    [{NEON_CYAN}]{data.get('coordination_backend', 'unknown')}[/{NEON_CYAN}]"
                    f" ({data.get('coordination_status', 'unknown')})"
                )
                console.print()

                # Stats
                uptime = data.get("uptime_seconds", 0)
                hours = int(uptime // 3600)
                mins = int((uptime % 3600) // 60)
                uptime_str = f"{hours}h {mins}m" if hours else f"{mins}m"

                console.print(f"  Uptime:       [{CORAL}]{uptime_str}[/{CORAL}]")
                console.print(f"  Entities:     [{CORAL}]{data.get('entity_count', 0):,}[/{CORAL}]")
                console.print(f"  Queue depth:  [{CORAL}]{data.get('queue_depth', 0)}[/{CORAL}]")
                console.print(
                    f"  Durable:      [{CORAL}]{str(data.get('coordination_durable', False)).lower()}[/{CORAL}]"
                )
                pending = data.get("pending_writes", {})
                if isinstance(pending, dict):
                    metrics = pending.get("metrics", {})
                    if not isinstance(metrics, dict):
                        metrics = {}
                    console.print(
                        f"  Pending:      [{CORAL}]{pending.get('count', 0)}[/{CORAL}]"
                        f" buffered, [{CORAL}]{metrics.get('attempted', 0)}[/{CORAL}] attempted,"
                        f" [{CORAL}]{metrics.get('replayed', 0)}[/{CORAL}] replayed,"
                        f" [{CORAL}]{metrics.get('discarded', 0)}[/{CORAL}] discarded"
                    )
                surreal_obs = data.get("surreal_observability", {})
                if isinstance(surreal_obs, dict) and surreal_obs.get("configured"):
                    metrics_status = surreal_obs.get("metrics_http_status")
                    metrics_available = bool(surreal_obs.get("metrics_available"))
                    metrics_display = (
                        f"metrics {metrics_status}"
                        if metrics_status is not None
                        else "metrics unknown"
                    )
                    if metrics_available:
                        metrics_display = (
                            f"metrics ok ({surreal_obs.get('metric_count', 0)} series)"
                        )
                    console.print(
                        f"  Surreal:      health {surreal_obs.get('health_http_status', 'unknown')}, "
                        f"{metrics_display}"
                    )
                coordination_error = data.get("coordination_error")
                if coordination_error:
                    console.print(
                        f"  Detail:       [{ERROR_RED}]{coordination_error}[/{ERROR_RED}]"
                    )
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
                raise typer.Exit(1) from None
            handle_client_error(e)

    _run()
