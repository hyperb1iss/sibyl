"""Synthetic data generation CLI commands.

Commands for generating test data to stress-test the system.
Supports both template-based (fast) and LLM-enhanced (Claude) generation.
"""

import os

# Disable graphiti telemetry before any imports
os.environ["GRAPHITI_TELEMETRY_ENABLED"] = "false"

from typing import Annotated

import typer

from sibyl.cli.common import (
    CORAL,
    ELECTRIC_PURPLE,
    ELECTRIC_YELLOW,
    NEON_CYAN,
    SUCCESS_GREEN,
    console,
    create_panel,
    create_table,
    error,
    info,
    run_async,
    spinner,
    success,
)

app = typer.Typer(
    name="generate",
    help="Generate synthetic test data",
    no_args_is_help=True,
)


@app.command("realistic")
def generate_realistic(  # noqa: PLR0915 - complex CLI command
    projects: Annotated[int, typer.Option("--projects", "-p", help="Number of projects")] = 5,
    tasks_per_project: Annotated[int, typer.Option("--tasks", "-t", help="Tasks per project")] = 20,
    patterns: Annotated[int, typer.Option("--patterns", help="Number of patterns")] = 50,
    episodes: Annotated[int, typer.Option("--episodes", "-e", help="Number of episodes")] = 100,
    seed: Annotated[int | None, typer.Option("--seed", "-s", help="Random seed")] = None,
    model: Annotated[str, typer.Option("--model", "-m", help="LLM model: sonnet, opus")] = "sonnet",
    no_llm: Annotated[
        bool, typer.Option("--no-llm", help="Use templates only (no API calls)")
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show what would be generated")
    ] = False,
) -> None:
    """Generate a realistic development scenario with interconnected data.

    Uses Claude Sonnet 4.5 by default for balanced quality/cost.
    Use --model opus for highest quality content.
    Use --no-llm for fast, template-only generation without API calls.

    Examples:
        sibyl generate realistic                    # Default: 5 projects, Sonnet
        sibyl generate realistic --model opus       # Use Opus for rich content
        sibyl generate realistic --no-llm           # Fast, no API calls
        sibyl generate realistic -p 10 -t 30        # 10 projects, 30 tasks each
    """
    if dry_run:
        console.print(
            create_panel(f"[{ELECTRIC_PURPLE}]Dry Run - Generation Plan[/{ELECTRIC_PURPLE}]")
        )
        console.print(f"\n  Projects: [{NEON_CYAN}]{projects}[/{NEON_CYAN}]")
        console.print(
            f"  Tasks: [{NEON_CYAN}]{projects * tasks_per_project}[/{NEON_CYAN}] ({tasks_per_project}/project)"
        )
        console.print(f"  Patterns: [{NEON_CYAN}]{patterns}[/{NEON_CYAN}]")
        console.print(f"  Episodes: [{NEON_CYAN}]{episodes}[/{NEON_CYAN}]")
        console.print(
            f"  Model: [{NEON_CYAN}]{model if not no_llm else 'none (template only)'}[/{NEON_CYAN}]"
        )
        console.print(f"  Seed: [{NEON_CYAN}]{seed or 'random'}[/{NEON_CYAN}]")
        console.print("\n[dim]Run without --dry-run to generate data[/dim]")
        return

    @run_async
    async def _generate() -> None:  # noqa: PLR0915 - complex CLI logic
        from sibyl.generator.config import GeneratorConfig, ModelType
        from sibyl.generator.llm import LLMContentGenerator
        from sibyl.generator.relationships import RelationshipWeaver
        from sibyl.generator.templates import TemplateGenerator
        from sibyl.graph.entities import EntityManager
        from sibyl.graph.relationships import RelationshipManager

        # Build config
        model_type = ModelType.OPUS if model.lower() == "opus" else ModelType.SONNET
        config = GeneratorConfig(
            projects=projects,
            tasks_per_project=tasks_per_project,
            patterns=patterns,
            episodes=episodes,
            seed=seed,
            model=model_type,
            use_llm=not no_llm,
        )

        # Select generator
        if no_llm:
            generator = TemplateGenerator(config)
            gen_name = "Template Generator"
        else:
            generator = LLMContentGenerator(config)
            gen_name = f"LLM Generator ({model_type.value})"

        console.print(
            create_panel(f"[{ELECTRIC_PURPLE}]Generating Realistic Data[/{ELECTRIC_PURPLE}]")
        )
        console.print(f"\n  Using: [{NEON_CYAN}]{gen_name}[/{NEON_CYAN}]")

        try:
            with spinner("Generating entities...") as progress:
                task = progress.add_task("Generating entities...", total=None)
                result = await generator.generate()

                # Weave relationships
                progress.update(task, description="Weaving relationships...")
                weaver = RelationshipWeaver(config)
                result.relationships = weaver.weave(result.entities)

            console.print(
                f"\n[{SUCCESS_GREEN}]Generated {result.entity_count} entities, {result.relationship_count} relationships[/{SUCCESS_GREEN}]"
            )

            # Show summary
            table = create_table("Generation Results", "Metric", "Value")
            table.add_row("Entities", str(result.entity_count))
            table.add_row("Relationships", str(result.relationship_count))
            table.add_row("Duration", f"{result.duration_seconds:.2f}s")
            console.print(table)

            # Store in graph
            confirm = typer.confirm("\nStore generated data in the graph?")
            if confirm:
                with spinner("Storing in graph...") as progress:
                    progress.add_task("Storing in graph...", total=None)

                    from sibyl.graph.client import get_graph_client

                    client = await get_graph_client()
                    entity_mgr = EntityManager(client)
                    rel_mgr = RelationshipManager(client)

                    # Use bulk_create_direct for speed (bypasses Graphiti LLM)
                    stored_entities, _ = await entity_mgr.bulk_create_direct(
                        result.entities, batch_size=100
                    )
                    stored_rels, _ = await rel_mgr.bulk_create_direct(
                        result.relationships, batch_size=100
                    )

                success(f"Stored {stored_entities} entities, {stored_rels} relationships")
            else:
                info("Data not stored")

        except ImportError as e:
            if "anthropic" in str(e):
                error("Anthropic SDK not installed")
                console.print(f"\n[{ELECTRIC_YELLOW}]Install with:[/{ELECTRIC_YELLOW}]")
                console.print(f"  [{NEON_CYAN}]uv add anthropic[/{NEON_CYAN}]")
                console.print("\n[dim]Or use --no-llm for template-only generation[/dim]")
            else:
                error(f"Import error: {e}")
        except Exception as e:
            error(f"Generation failed: {e}")

    _generate()


@app.command("stress")
def generate_stress(
    entities: Annotated[int, typer.Option("--entities", "-e", help="Total entities")] = 5000,
    relationships: Annotated[
        int, typer.Option("--relationships", "-r", help="Total relationships")
    ] = 10000,
    depth: Annotated[int, typer.Option("--depth", "-d", help="Max graph depth")] = 5,
    seed: Annotated[int | None, typer.Option("--seed", "-s", help="Random seed")] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show what would be generated")
    ] = False,
) -> None:
    """Generate maximum-scale data for stress testing.

    Creates large volumes of entities and relationships quickly
    using templates only (no LLM calls).

    Examples:
        sibyl generate stress                           # Default: 5000 entities
        sibyl generate stress -e 10000 -r 20000         # 10K entities, 20K relationships
        sibyl generate stress --dry-run                 # Preview without generating
    """
    if dry_run:
        console.print(create_panel(f"[{ELECTRIC_PURPLE}]Stress Test Plan[/{ELECTRIC_PURPLE}]"))
        console.print(f"\n  Entities: [{NEON_CYAN}]{entities:,}[/{NEON_CYAN}]")
        console.print(f"  Relationships: [{NEON_CYAN}]{relationships:,}[/{NEON_CYAN}]")
        console.print(f"  Max Depth: [{NEON_CYAN}]{depth}[/{NEON_CYAN}]")
        console.print(f"  Seed: [{NEON_CYAN}]{seed or 'random'}[/{NEON_CYAN}]")
        console.print("\n[dim]Run without --dry-run to generate data[/dim]")
        return

    @run_async
    async def _stress() -> None:
        from sibyl.generator.config import StressConfig
        from sibyl.generator.stress import StressTestGenerator
        from sibyl.graph.entities import EntityManager
        from sibyl.graph.relationships import RelationshipManager

        stress_config = StressConfig(
            entities=entities,
            relationships=relationships,
            max_depth=depth,
        )

        generator = StressTestGenerator(stress_config, seed=seed)

        console.print(
            create_panel(f"[{ELECTRIC_PURPLE}]Stress Test Generation[/{ELECTRIC_PURPLE}]")
        )
        console.print(
            f"\n  Target: [{CORAL}]{entities:,}[/{CORAL}] entities, [{CORAL}]{relationships:,}[/{CORAL}] relationships"
        )

        try:
            with spinner("Generating stress test data...") as progress:
                task = progress.add_task("Generating...", total=None)
                result = await generator.generate()
                progress.update(task, description="Complete!")

            # Show results
            table = create_table("Stress Test Results", "Metric", "Value")
            table.add_row("Entities Generated", f"{result.entity_count:,}")
            table.add_row("Relationships Generated", f"{result.relationship_count:,}")
            table.add_row("Duration", f"{result.duration_seconds:.2f}s")
            rate = (
                result.entity_count / result.duration_seconds if result.duration_seconds > 0 else 0
            )
            table.add_row("Rate", f"{rate:,.0f} entities/sec")
            console.print(table)

            # Store in graph
            confirm = typer.confirm("\nStore stress test data in the graph?")
            if confirm:
                with spinner("Storing in graph (this may take a while)...") as progress:
                    task = progress.add_task("Storing...", total=None)

                    from sibyl.graph.client import get_graph_client

                    client = await get_graph_client()
                    entity_mgr = EntityManager(client)
                    rel_mgr = RelationshipManager(client)

                    # Use bulk_create_direct for speed (bypasses Graphiti LLM)
                    progress.update(task, description="Storing entities...")
                    stored, _failed_ents = await entity_mgr.bulk_create_direct(
                        result.entities, batch_size=500
                    )

                    progress.update(task, description="Storing relationships...")
                    stored_rels, _failed_rels = await rel_mgr.bulk_create_direct(
                        result.relationships, batch_size=500
                    )

                success(f"Stored {stored:,} entities, {stored_rels:,} relationships")
            else:
                info("Data not stored")

        except Exception as e:
            error(f"Stress test failed: {e}")

    _stress()


@app.command("scenario")
def generate_scenario(  # noqa: PLR0915 - complex CLI command
    name: Annotated[str | None, typer.Argument(help="Scenario name")] = None,
    list_scenarios: Annotated[
        bool, typer.Option("--list", "-l", help="List available scenarios")
    ] = False,
    model: Annotated[str, typer.Option("--model", "-m", help="LLM model: sonnet, opus")] = "sonnet",
    no_llm: Annotated[bool, typer.Option("--no-llm", help="Use templates only")] = False,
    seed: Annotated[int | None, typer.Option("--seed", "-s", help="Random seed")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show scenario details only")] = False,
) -> None:
    """Generate data from a predefined scenario.

    Scenarios are curated configurations for different development contexts:
    startup, enterprise, open-source, data pipelines, etc.

    Examples:
        sibyl generate scenario --list              # See all scenarios
        sibyl generate scenario startup-mvp         # Generate startup data
        sibyl generate scenario enterprise-migration --model opus
    """
    from sibyl.generator.scenarios import SCENARIOS, list_scenarios as get_scenarios

    if list_scenarios or not name:
        console.print(create_panel(f"[{ELECTRIC_PURPLE}]Available Scenarios[/{ELECTRIC_PURPLE}]"))
        console.print()

        for scenario_name, description in get_scenarios().items():
            scenario = SCENARIOS[scenario_name]
            console.print(f"  [{NEON_CYAN}]{scenario_name}[/{NEON_CYAN}]")
            console.print(f"    {description}")
            console.print(
                f"    [dim]{scenario.projects} projects, {scenario.projects * scenario.tasks_per_project} tasks, {scenario.patterns} patterns[/dim]"
            )
            console.print()
        return

    if name not in SCENARIOS:
        error(f"Unknown scenario: {name}")
        info(f"Valid scenarios: {', '.join(SCENARIOS.keys())}")
        return

    scenario = SCENARIOS[name]

    if dry_run:
        console.print(create_panel(f"[{ELECTRIC_PURPLE}]Scenario: {name}[/{ELECTRIC_PURPLE}]"))
        console.print(f"\n  {scenario.description}\n")

        table = create_table("Configuration", "Setting", "Value")
        table.add_row("Projects", str(scenario.projects))
        table.add_row("Tasks", str(scenario.projects * scenario.tasks_per_project))
        table.add_row("Patterns", str(scenario.patterns))
        table.add_row("Episodes", str(scenario.episodes))
        table.add_row("Rules", str(scenario.rules))
        table.add_row("Templates", str(scenario.templates))
        table.add_row(
            "Languages", ", ".join(scenario.languages) if scenario.languages else "default"
        )
        table.add_row(
            "Frameworks", ", ".join(scenario.frameworks) if scenario.frameworks else "default"
        )
        console.print(table)

        console.print("\n[dim]Run without --dry-run to generate data[/dim]")
        return

    @run_async
    async def _scenario() -> None:
        from sibyl.generator.config import ModelType
        from sibyl.generator.scenarios import ScenarioRunner
        from sibyl.graph.entities import EntityManager
        from sibyl.graph.relationships import RelationshipManager

        model_type = ModelType.OPUS if model.lower() == "opus" else ModelType.SONNET

        runner = ScenarioRunner(
            scenario=scenario,
            model=model_type,
            use_llm=not no_llm,
            seed=seed,
        )

        console.print(
            create_panel(f"[{ELECTRIC_PURPLE}]Running Scenario: {name}[/{ELECTRIC_PURPLE}]")
        )
        console.print(f"\n  {scenario.description}\n")

        try:
            with spinner(f"Generating {name} scenario...") as progress:
                task = progress.add_task("Generating...", total=None)

                def progress_cb(step: str, _current: int, _total: int) -> None:
                    progress.update(task, description=f"{step}...")

                result = await runner.run(progress_callback=progress_cb)

            # Show results
            table = create_table("Scenario Results", "Metric", "Value")
            table.add_row("Entities Generated", f"{result.entity_count:,}")
            table.add_row("Relationships Generated", f"{result.relationship_count:,}")
            table.add_row("Duration", f"{result.duration_seconds:.2f}s")
            console.print(table)

            # Store in graph
            confirm = typer.confirm("\nStore scenario data in the graph?")
            if confirm:
                with spinner("Storing in graph...") as progress:
                    progress.add_task("Storing...", total=None)

                    from sibyl.graph.client import get_graph_client

                    client = await get_graph_client()
                    entity_mgr = EntityManager(client)
                    rel_mgr = RelationshipManager(client)

                    # Use bulk_create_direct for speed (bypasses Graphiti LLM)
                    stored_entities, _ = await entity_mgr.bulk_create_direct(
                        result.entities, batch_size=100
                    )
                    stored_rels, _ = await rel_mgr.bulk_create_direct(
                        result.relationships, batch_size=100
                    )

                success(f"Stored {stored_entities:,} entities, {stored_rels:,} relationships")
            else:
                info("Data not stored")

        except ImportError as e:
            if "anthropic" in str(e):
                error("Anthropic SDK not installed")
                console.print(f"\n[{ELECTRIC_YELLOW}]Install with:[/{ELECTRIC_YELLOW}]")
                console.print(f"  [{NEON_CYAN}]uv add anthropic[/{NEON_CYAN}]")
                console.print("\n[dim]Or use --no-llm for template-only generation[/dim]")
            else:
                error(f"Import error: {e}")
        except Exception as e:
            error(f"Scenario failed: {e}")

    _scenario()


@app.command("clean")
def clean_generated(
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation")] = False,
    preserve_real: Annotated[
        bool, typer.Option("--preserve-real", help="Keep non-generated data")
    ] = True,
) -> None:
    """Clean up generated test data.

    Removes entities that have the _generated marker in their metadata.
    By default, preserves any real (non-generated) data.

    Examples:
        sibyl generate clean                    # Interactive cleanup
        sibyl generate clean --yes              # Skip confirmation
        sibyl generate clean --no-preserve-real # Remove ALL data (dangerous!)
    """
    if not yes:
        if preserve_real:
            console.print(
                f"[{ELECTRIC_YELLOW}]This will remove all generated test data.[/{ELECTRIC_YELLOW}]"
            )
        else:
            console.print(f"[{CORAL}]WARNING: This will remove ALL data from the graph![/{CORAL}]")

        confirm = typer.confirm("Continue?")
        if not confirm:
            info("Cancelled")
            return

    @run_async
    async def _clean() -> None:
        from sibyl.graph.client import get_graph_client

        try:
            with spinner("Cleaning generated data...") as progress:
                progress.add_task("Cleaning...", total=None)

                client = await get_graph_client()

                if preserve_real:
                    # Delete only generated entities
                    result = await client.driver.execute_query(
                        "MATCH (n) WHERE n._generated = true DETACH DELETE n RETURN count(n) as deleted"
                    )
                    deleted = result.result_set[0][0] if result.result_set else 0
                else:
                    # Delete everything
                    result = await client.driver.execute_query(
                        "MATCH (n) DETACH DELETE n RETURN count(n) as deleted"
                    )
                    deleted = result.result_set[0][0] if result.result_set else 0

            success(f"Removed {deleted:,} entities")

        except Exception as e:
            error(f"Cleanup failed: {e}")

    _clean()
