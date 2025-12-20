"""Synthetic data generation CLI commands.

Commands for generating test data to stress-test the system.
Stub implementation - full generator to be built in separate tasks.
"""

from typing import Annotated

import typer

from sibyl.cli.common import (
    ELECTRIC_PURPLE,
    NEON_CYAN,
    console,
    error,
    info,
)

app = typer.Typer(
    name="generate",
    help="Generate synthetic test data",
    no_args_is_help=True,
)


@app.command("realistic")
def generate_realistic(
    projects: Annotated[int, typer.Option("--projects", "-p", help="Number of projects")] = 5,
    tasks_per_project: Annotated[int, typer.Option("--tasks", "-t", help="Tasks per project")] = 20,
    patterns: Annotated[int, typer.Option("--patterns", help="Number of patterns")] = 50,
    episodes: Annotated[int, typer.Option("--episodes", "-e", help="Number of episodes")] = 100,
    seed: Annotated[int | None, typer.Option("--seed", "-s", help="Random seed")] = None,
    model: Annotated[str, typer.Option("--model", "-m", help="LLM model: sonnet, opus")] = "sonnet",
) -> None:
    """Generate a realistic development scenario with interconnected data."""
    info("Generator not yet implemented")
    console.print(f"\n[{ELECTRIC_PURPLE}]Planned generation:[/{ELECTRIC_PURPLE}]")
    console.print(f"  Projects: [{NEON_CYAN}]{projects}[/{NEON_CYAN}]")
    console.print(f"  Tasks: [{NEON_CYAN}]{projects * tasks_per_project}[/{NEON_CYAN}] ({tasks_per_project}/project)")
    console.print(f"  Patterns: [{NEON_CYAN}]{patterns}[/{NEON_CYAN}]")
    console.print(f"  Episodes: [{NEON_CYAN}]{episodes}[/{NEON_CYAN}]")
    console.print(f"  Model: [{NEON_CYAN}]{model}[/{NEON_CYAN}]")
    console.print(f"  Seed: [{NEON_CYAN}]{seed or 'random'}[/{NEON_CYAN}]")
    console.print("\n[dim]Run 'sibyl generate scenario --list' to see predefined scenarios[/dim]")


@app.command("stress")
def generate_stress(
    entities: Annotated[int, typer.Option("--entities", "-e", help="Total entities")] = 5000,
    relationships: Annotated[int, typer.Option("--relationships", "-r", help="Total relationships")] = 10000,
    depth: Annotated[int, typer.Option("--depth", "-d", help="Max graph depth")] = 5,
) -> None:
    """Generate maximum-scale data for stress testing."""
    info("Stress test generator not yet implemented")
    console.print(f"\n[{ELECTRIC_PURPLE}]Planned stress test:[/{ELECTRIC_PURPLE}]")
    console.print(f"  Entities: [{NEON_CYAN}]{entities}[/{NEON_CYAN}]")
    console.print(f"  Relationships: [{NEON_CYAN}]{relationships}[/{NEON_CYAN}]")
    console.print(f"  Max Depth: [{NEON_CYAN}]{depth}[/{NEON_CYAN}]")


@app.command("scenario")
def generate_scenario(
    name: Annotated[str | None, typer.Argument(help="Scenario name")] = None,
    list_scenarios: Annotated[bool, typer.Option("--list", "-l", help="List available scenarios")] = False,
) -> None:
    """Generate data from a predefined scenario."""
    scenarios = {
        "startup-mvp": "5 projects, 100 tasks, fast iteration startup",
        "enterprise-migration": "3 projects, 200 tasks, complex dependencies",
        "open-source-library": "1 project, 50 tasks, many patterns",
        "data-pipeline": "2 projects, 75 tasks, heavy episodes",
    }

    if list_scenarios or not name:
        console.print(f"\n[{ELECTRIC_PURPLE}]Available Scenarios:[/{ELECTRIC_PURPLE}]\n")
        for scenario, desc in scenarios.items():
            console.print(f"  [{NEON_CYAN}]{scenario}[/{NEON_CYAN}]")
            console.print(f"    {desc}")
        return

    if name not in scenarios:
        error(f"Unknown scenario: {name}")
        info(f"Valid scenarios: {', '.join(scenarios.keys())}")
        return

    info(f"Scenario generator not yet implemented: {name}")
    console.print(f"\n[dim]{scenarios[name]}[/dim]")


@app.command("clean")
def clean_generated(
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation")] = False,
    preserve_real: Annotated[bool, typer.Option("--preserve-real", help="Keep non-generated data")] = True,
) -> None:
    """Clean up generated test data."""
    if not yes:
        confirm = typer.confirm("Remove all generated test data?")
        if not confirm:
            info("Cancelled")
            return

    info("Clean not yet implemented")
    if preserve_real:
        console.print("[dim]Would preserve non-generated data[/dim]")
    else:
        console.print("[dim]Would remove ALL data[/dim]")
