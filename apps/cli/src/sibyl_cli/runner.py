"""Runner management CLI commands.

Commands for managing distributed runners:
- list: Show all runners and their status
- route: Route a task to the optimal runner
- scores: Show routing scores for all runners
"""

import asyncio
from typing import Annotated, Any

import typer

from sibyl_cli.client import SibylClientError, get_client
from sibyl_cli.common import (
    CORAL,
    ELECTRIC_PURPLE,
    NEON_CYAN,
    SUCCESS_GREEN,
    console,
    create_table,
    error,
    handle_client_error,
    info,
    print_json,
    success,
)

app = typer.Typer(
    name="runner",
    help="Manage distributed runners",
    no_args_is_help=True,
)


def _status_color(status: str) -> str:
    """Get color for runner status."""
    colors = {
        "online": SUCCESS_GREEN,
        "offline": "dim",
        "busy": ELECTRIC_PURPLE,
        "draining": "yellow",
    }
    return colors.get(status, "white")


@app.command("list")
def list_runners(
    status: Annotated[str | None, typer.Option("--status", "-s", help="Filter by status")] = None,
    json_out: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
) -> None:
    """List all runners for the organization.

    Example:
        sibyl runner list
        sibyl runner list --status online
    """

    async def _list_runners() -> None:
        async with get_client() as client:
            params: dict[str, Any] = {}
            if status:
                params["status"] = status

            result = await client.get("/runners", params=params)

            if json_out:
                print_json(result)
                return

            runners = result.get("runners", [])
            if not runners:
                info("No runners found")
                return

            table = create_table("Runners", "Name", "Status", "Load", "Capabilities")
            for r in runners:
                name = r.get("name", "")
                runner_status = r.get("status", "unknown")
                current = r.get("current_agent_count", 0)
                max_agents = r.get("max_concurrent_agents", 0)
                load = f"{current}/{max_agents}"
                caps = ", ".join(r.get("capabilities", [])) or "-"

                color = _status_color(runner_status)
                table.add_row(
                    f"[{NEON_CYAN}]{name}[/{NEON_CYAN}]",
                    f"[{color}]{runner_status}[/{color}]",
                    load,
                    caps[:30],
                )

            console.print(table)
            console.print(f"\n[dim]Total: {result.get('total', 0)} runners[/dim]")

    try:
        asyncio.run(_list_runners())
    except SibylClientError as e:
        handle_client_error(e)


@app.command()
def route(
    project_id: Annotated[
        str | None, typer.Option("--project", "-p", help="Project ID for affinity scoring")
    ] = None,
    capabilities: Annotated[
        str | None, typer.Option("--caps", "-c", help="Required capabilities (comma-separated)")
    ] = None,
    prefer: Annotated[str | None, typer.Option("--prefer", help="Preferred runner ID")] = None,
    exclude: Annotated[
        str | None, typer.Option("--exclude", "-x", help="Runner IDs to exclude (comma-separated)")
    ] = None,
    json_out: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
) -> None:
    """Route a task to the optimal runner.

    Scores runners based on:
    - Project affinity (warm worktrees)
    - Capability match
    - Current load
    - Health status

    Example:
        sibyl runner route --project proj_abc123 --caps docker,gpu
    """

    async def _route_task() -> None:
        async with get_client() as client:
            payload: dict[str, Any] = {}
            if project_id:
                payload["project_id"] = project_id
            if capabilities:
                payload["required_capabilities"] = [c.strip() for c in capabilities.split(",")]
            if prefer:
                payload["preferred_runner_id"] = prefer
            if exclude:
                payload["exclude_runners"] = [e.strip() for e in exclude.split(",")]

            result = await client.post("/runners/route", json=payload)

            if json_out:
                print_json(result)
                return

            if result.get("success"):
                runner_name = result.get("runner_name", "Unknown")
                runner_id = result.get("runner_id", "")
                score_data = result.get("score", {})
                total_score = score_data.get("total_score", 0)

                success(f"Routed to [{NEON_CYAN}]{runner_name}[/{NEON_CYAN}]")
                console.print(f"  Runner ID: [{CORAL}]{runner_id}[/{CORAL}]")
                console.print(f"  Score: {total_score:.1f}")

                if score_data.get("has_warm_worktree"):
                    console.print(f"  [{SUCCESS_GREEN}]✓[/{SUCCESS_GREEN}] Warm worktree available")

                console.print(f"  Available slots: {score_data.get('available_slots', 0)}")
            else:
                reason = result.get("reason", "Unknown error")
                error(f"Routing failed: {reason}")

                # Show all scores for debugging
                all_scores = result.get("all_scores", [])
                if all_scores:
                    console.print("\n[dim]Runner scores:[/dim]")
                    for s in all_scores:
                        name = s.get("runner_name", "")
                        total = s.get("total_score", 0)
                        missing = s.get("missing_capabilities", [])
                        console.print(f"  {name}: {total:.1f}", end="")
                        if missing:
                            console.print(f" [red](missing: {', '.join(missing)})[/red]")
                        else:
                            console.print()

    try:
        asyncio.run(_route_task())
    except SibylClientError as e:
        handle_client_error(e)


@app.command()
def scores(
    project_id: Annotated[
        str | None, typer.Option("--project", "-p", help="Project ID for affinity scoring")
    ] = None,
    capabilities: Annotated[
        str | None, typer.Option("--caps", "-c", help="Required capabilities (comma-separated)")
    ] = None,
    json_out: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
) -> None:
    """Show routing scores for all runners.

    Useful for debugging and understanding runner selection.

    Example:
        sibyl runner scores
        sibyl runner scores --project proj_abc123 --caps docker
    """

    async def _get_scores() -> None:
        async with get_client() as client:
            params: dict[str, Any] = {}
            if project_id:
                params["project_id"] = project_id
            if capabilities:
                params["capabilities"] = capabilities

            result = await client.get("/runners/scores", params=params)

            if json_out:
                print_json(result)
                return

            scores_list = result.get("scores", [])
            if not scores_list:
                info("No runners available")
                return

            table = create_table(
                "Runner Scores",
                "Runner",
                "Total",
                "Affinity",
                "Caps",
                "Load",
                "Health",
                "Slots",
            )

            for s in scores_list:
                name = s.get("runner_name", "")
                total = s.get("total_score", 0)
                affinity = s.get("affinity_score", 0)
                caps = s.get("capability_score", 0)
                load = s.get("load_score", 0)
                health = s.get("health_penalty", 0)
                slots = s.get("available_slots", 0)

                # Color total based on viability
                total_str = (
                    f"[{SUCCESS_GREEN}]{total:.0f}[/{SUCCESS_GREEN}]"
                    if total >= 0
                    else f"[red]{total:.0f}[/red]"
                )

                # Color affinity if has warm worktree
                affinity_str = (
                    f"[{SUCCESS_GREEN}]{affinity:.0f}[/{SUCCESS_GREEN}]"
                    if s.get("has_warm_worktree")
                    else f"{affinity:.0f}"
                )

                # Color caps if missing
                caps_str = (
                    f"[red]{caps:.0f}[/red]" if s.get("missing_capabilities") else f"{caps:.0f}"
                )

                # Color health penalty
                health_str = f"[red]{health:.0f}[/red]" if health < 0 else f"{health:.0f}"

                table.add_row(
                    f"[{NEON_CYAN}]{name}[/{NEON_CYAN}]",
                    total_str,
                    affinity_str,
                    caps_str,
                    f"{load:.0f}",
                    health_str,
                    str(slots),
                )

            console.print(table)
            console.print(f"\n[dim]Total: {result.get('total', 0)} runners[/dim]")

            # Legend
            console.print(
                "[dim]Scoring: affinity(50) + caps(30) + load(0-20) + health(-100 if stale)[/dim]"
            )

    try:
        asyncio.run(_get_scores())
    except SibylClientError as e:
        handle_client_error(e)
