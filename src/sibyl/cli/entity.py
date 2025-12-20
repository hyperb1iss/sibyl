"""Entity CRUD CLI commands.

Generic commands for all entity types: list, show, create, update, delete, related.
"""

from typing import Annotated

import typer

from sibyl.cli.common import (
    CORAL,
    ELECTRIC_PURPLE,
    NEON_CYAN,
    console,
    create_panel,
    create_table,
    error,
    info,
    print_db_hint,
    run_async,
    spinner,
    success,
    truncate,
)

app = typer.Typer(
    name="entity",
    help="Generic entity CRUD operations",
    no_args_is_help=True,
)

# Valid entity types
ENTITY_TYPES = [
    "pattern",
    "rule",
    "template",
    "tool",
    "language",
    "topic",
    "episode",
    "knowledge_source",
    "config_file",
    "slash_command",
    "task",
    "project",
    "team",
    "error_pattern",
    "milestone",
    "source",
    "document",
    "community",
]


@app.command("list")
def list_entities(
    entity_type: Annotated[str, typer.Option("--type", "-t", help="Entity type to list")] = "pattern",
    language: Annotated[str | None, typer.Option("--language", "-l", help="Filter by language")] = None,
    category: Annotated[str | None, typer.Option("--category", "-c", help="Filter by category")] = None,
    limit: Annotated[int, typer.Option("--limit", "-n", help="Max results")] = 50,
    format_: Annotated[str, typer.Option("--format", "-f", help="Output format: table, json, csv")] = "table",
) -> None:
    """List entities by type with optional filters."""
    if entity_type not in ENTITY_TYPES:
        error(f"Invalid entity type: {entity_type}")
        info(f"Valid types: {', '.join(ENTITY_TYPES)}")
        return

    @run_async
    async def _list() -> None:
        from sibyl.tools.core import explore

        try:
            with spinner(f"Loading {entity_type}s...") as progress:
                progress.add_task(f"Loading {entity_type}s...", total=None)
                response = await explore(
                    mode="list",
                    types=[entity_type],
                    language=language,
                    category=category,
                    limit=limit,
                )

            entities = response.entities or []

            if format_ == "json":
                import json

                console.print(json.dumps([e.model_dump() for e in entities], indent=2, default=str))
                return

            if format_ == "csv":
                import csv
                import sys

                writer = csv.writer(sys.stdout)
                writer.writerow(["id", "name", "type", "description"])
                for e in entities:
                    writer.writerow([e.id, e.name, e.type, truncate(e.description or "", 100)])
                return

            if not entities:
                info(f"No {entity_type}s found")
                return

            table = create_table(f"{entity_type.title()}s", "ID", "Name", "Description")
            for e in entities:
                table.add_row(
                    e.id[:8] + "...",
                    truncate(e.name, 35),
                    truncate(e.description or "", 50),
                )

            console.print(table)
            console.print(f"\n[dim]Showing {len(entities)} {entity_type}(s)[/dim]")

        except Exception as e:
            error(f"Failed to list entities: {e}")
            print_db_hint()

    _list()


@app.command("show")
def show_entity(
    entity_id: Annotated[str, typer.Argument(help="Entity ID")],
) -> None:
    """Show detailed entity information."""

    @run_async
    async def _show() -> None:
        from sibyl.graph.entities import EntityManager

        try:
            with spinner("Loading entity...") as progress:
                progress.add_task("Loading entity...", total=None)
                manager = EntityManager()
                entity = await manager.get(entity_id)

            if not entity:
                error(f"Entity not found: {entity_id}")
                return

            lines = [
                f"[{ELECTRIC_PURPLE}]Name:[/{ELECTRIC_PURPLE}] {entity.name}",
                f"[{ELECTRIC_PURPLE}]Type:[/{ELECTRIC_PURPLE}] {entity.entity_type}",
                f"[{ELECTRIC_PURPLE}]ID:[/{ELECTRIC_PURPLE}] {entity.id}",
                "",
                f"[{NEON_CYAN}]Description:[/{NEON_CYAN}]",
                entity.description or "[dim]No description[/dim]",
            ]

            if entity.content and entity.content != entity.description:
                lines.extend([
                    "",
                    f"[{NEON_CYAN}]Content:[/{NEON_CYAN}]",
                    entity.content[:500] + "..." if len(entity.content) > 500 else entity.content,
                ])

            meta = entity.metadata or {}
            if meta:
                lines.extend(["", f"[{CORAL}]Metadata:[/{CORAL}]"])
                for k, v in list(meta.items())[:10]:
                    lines.append(f"  {k}: {truncate(str(v), 60)}")

            panel = create_panel("\n".join(lines), title=f"{entity.entity_type.title()} Details")
            console.print(panel)

        except Exception as e:
            error(f"Failed to show entity: {e}")
            print_db_hint()

    _show()


@app.command("create")
def create_entity(
    entity_type: Annotated[str, typer.Option("--type", "-t", help="Entity type", prompt=True)],
    name: Annotated[str, typer.Option("--name", "-n", help="Entity name", prompt=True)],
    content: Annotated[str | None, typer.Option("--content", "-c", help="Entity content")] = None,
    category: Annotated[str | None, typer.Option("--category", help="Category")] = None,
    languages: Annotated[str | None, typer.Option("--languages", "-l", help="Comma-separated languages")] = None,
    tags: Annotated[str | None, typer.Option("--tags", help="Comma-separated tags")] = None,
) -> None:
    """Create a new entity."""
    if entity_type not in ENTITY_TYPES:
        error(f"Invalid entity type: {entity_type}")
        info(f"Valid types: {', '.join(ENTITY_TYPES)}")
        return

    @run_async
    async def _create() -> None:
        from sibyl.tools.core import add

        try:
            lang_list = [l.strip() for l in languages.split(",")] if languages else None
            tag_list = [t.strip() for t in tags.split(",")] if tags else None

            with spinner("Creating entity...") as progress:
                progress.add_task("Creating entity...", total=None)
                response = await add(
                    title=name,
                    content=content or f"{entity_type}: {name}",
                    entity_type=entity_type if entity_type in ["episode", "pattern", "task", "project"] else "episode",
                    category=category,
                    languages=lang_list,
                    tags=tag_list,
                )

            if response.success:
                success(f"Entity created: {response.id}")
            else:
                error(f"Failed to create entity: {response.message}")

        except Exception as e:
            error(f"Failed to create entity: {e}")
            print_db_hint()

    _create()


@app.command("delete")
def delete_entity(
    entity_id: Annotated[str, typer.Argument(help="Entity ID to delete")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation")] = False,
) -> None:
    """Delete an entity (with confirmation)."""
    if not yes:
        confirm = typer.confirm(f"Delete entity {entity_id[:8]}...? This cannot be undone.")
        if not confirm:
            info("Cancelled")
            return

    @run_async
    async def _delete() -> None:
        from sibyl.graph.entities import EntityManager

        try:
            with spinner("Deleting entity...") as progress:
                progress.add_task("Deleting entity...", total=None)
                manager = EntityManager()
                deleted = await manager.delete(entity_id)

            if deleted:
                success(f"Entity deleted: {entity_id[:8]}...")
            else:
                error(f"Entity not found: {entity_id}")

        except Exception as e:
            error(f"Failed to delete entity: {e}")
            print_db_hint()

    _delete()


@app.command("related")
def related_entities(
    entity_id: Annotated[str, typer.Argument(help="Entity ID")],
    limit: Annotated[int, typer.Option("--limit", "-n", help="Max results")] = 20,
) -> None:
    """Show entities related to the given entity (1-hop)."""

    @run_async
    async def _related() -> None:
        from sibyl.tools.core import explore

        try:
            with spinner("Finding related entities...") as progress:
                progress.add_task("Finding related entities...", total=None)
                response = await explore(
                    mode="related",
                    entity_id=entity_id,
                    limit=limit,
                )

            entities = response.entities or []

            if not entities:
                info("No related entities found")
                return

            table = create_table("Related Entities", "ID", "Name", "Type", "Relationship")
            for e in entities:
                rel_type = e.metadata.get("relationship_type", "-") if e.metadata else "-"
                table.add_row(
                    e.id[:8] + "...",
                    truncate(e.name, 30),
                    e.type,
                    rel_type,
                )

            console.print(table)
            console.print(f"\n[dim]Found {len(entities)} related entity(ies)[/dim]")

        except Exception as e:
            error(f"Failed to find related entities: {e}")
            print_db_hint()

    _related()
