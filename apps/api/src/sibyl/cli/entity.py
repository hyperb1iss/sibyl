"""Entity CRUD CLI commands.

Generic commands for all entity types: list, show, create, update, delete, related.
All commands communicate with the REST API to ensure proper event broadcasting.
"""

from typing import Annotated

import typer

from sibyl.cli.client import SibylClientError, get_client
from sibyl.cli.common import (
    CORAL,
    ELECTRIC_PURPLE,
    NEON_CYAN,
    console,
    create_panel,
    create_table,
    error,
    info,
    print_json,
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
    "convention",
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


def _handle_client_error(e: SibylClientError) -> None:
    """Handle client errors with helpful messages and exit with code 1."""
    if "Cannot connect" in str(e):
        error(str(e))
        info("Start the server with: sibyl serve")
    elif e.status_code == 404:
        error(f"Not found: {e.detail}")
    elif e.status_code == 400:
        error(f"Invalid request: {e.detail}")
    else:
        error(str(e))
    raise typer.Exit(1)


@app.command("list")
def list_entities(
    entity_type: Annotated[
        str, typer.Option("--type", "-T", help="Entity type to list")
    ] = "pattern",
    language: Annotated[
        str | None, typer.Option("--language", "-l", help="Filter by language")
    ] = None,
    category: Annotated[
        str | None, typer.Option("--category", "-c", help="Filter by category")
    ] = None,
    limit: Annotated[int, typer.Option("--limit", "-n", help="Max results")] = 50,
    table_out: Annotated[
        bool, typer.Option("--table", "-t", help="Table output (human-readable)")
    ] = False,
    csv_out: Annotated[bool, typer.Option("--csv", help="CSV output")] = False,
) -> None:
    """List entities by type with optional filters. Default: JSON output."""
    format_ = "table" if table_out else ("csv" if csv_out else "json")
    if entity_type not in ENTITY_TYPES:
        error(f"Invalid entity type: {entity_type}")
        info(f"Valid types: {', '.join(ENTITY_TYPES)}")
        return

    @run_async
    async def _list() -> None:
        client = get_client()

        try:
            if format_ in ("json", "csv"):
                response = await client.explore(
                    mode="list",
                    types=[entity_type],
                    language=language,
                    category=category,
                    limit=limit,
                )
            else:
                with spinner(f"Loading {entity_type}s...") as progress:
                    progress.add_task(f"Loading {entity_type}s...", total=None)
                    response = await client.explore(
                        mode="list",
                        types=[entity_type],
                        language=language,
                        category=category,
                        limit=limit,
                    )

            entities = response.get("entities", [])

            if format_ == "json":
                print_json(entities)
                return

            if format_ == "csv":
                import csv
                import sys

                writer = csv.writer(sys.stdout)
                writer.writerow(["id", "name", "type", "description"])
                for e in entities:
                    writer.writerow(
                        [
                            e.get("id", ""),
                            e.get("name", ""),
                            e.get("type", ""),
                            truncate(e.get("description") or "", 100),
                        ]
                    )
                return

            if not entities:
                info(f"No {entity_type}s found")
                return

            table = create_table(f"{entity_type.title()}s", "ID", "Name", "Description")
            for e in entities:
                table.add_row(
                    e.get("id", "")[:8] + "...",
                    truncate(e.get("name", ""), 35),
                    truncate(e.get("description") or "", 50),
                )

            console.print(table)
            console.print(f"\n[dim]Showing {len(entities)} {entity_type}(s)[/dim]")

        except SibylClientError as e:
            _handle_client_error(e)

    _list()


@app.command("show")
def show_entity(
    entity_id: Annotated[str, typer.Argument(help="Entity ID")],
    table_out: Annotated[
        bool, typer.Option("--table", "-t", help="Table output (human-readable)")
    ] = False,
) -> None:
    """Show detailed entity information. Default: JSON output."""

    @run_async
    async def _show() -> None:
        client = get_client()

        try:
            if table_out:
                with spinner("Loading entity...") as progress:
                    progress.add_task("Loading entity...", total=None)
                    entity = await client.get_entity(entity_id)
            else:
                entity = await client.get_entity(entity_id)

            # JSON output (default)
            if not table_out:
                print_json(entity)
                return

            # Table output
            lines = [
                f"[{ELECTRIC_PURPLE}]Name:[/{ELECTRIC_PURPLE}] {entity.get('name', '')}",
                f"[{ELECTRIC_PURPLE}]Type:[/{ELECTRIC_PURPLE}] {entity.get('entity_type', '')}",
                f"[{ELECTRIC_PURPLE}]ID:[/{ELECTRIC_PURPLE}] {entity.get('id', '')}",
                "",
                f"[{NEON_CYAN}]Description:[/{NEON_CYAN}]",
                entity.get("description") or "[dim]No description[/dim]",
            ]

            content = entity.get("content", "")
            if content and content != entity.get("description"):
                lines.extend(
                    [
                        "",
                        f"[{NEON_CYAN}]Content:[/{NEON_CYAN}]",
                        content[:500] + "..." if len(content) > 500 else content,
                    ]
                )

            meta = entity.get("metadata", {})
            if meta:
                lines.extend(["", f"[{CORAL}]Metadata:[/{CORAL}]"])
                for k, v in list(meta.items())[:10]:
                    lines.append(f"  {k}: {truncate(str(v), 60)}")

            entity_type = entity.get("entity_type", "entity")
            panel = create_panel("\n".join(lines), title=f"{entity_type.title()} Details")
            console.print(panel)

        except SibylClientError as e:
            _handle_client_error(e)

    _show()


@app.command("create")
def create_entity(
    entity_type: Annotated[str, typer.Option("--type", "-T", help="Entity type", prompt=True)],
    name: Annotated[str, typer.Option("--name", "-n", help="Entity name", prompt=True)],
    content: Annotated[str | None, typer.Option("--content", "-c", help="Entity content")] = None,
    category: Annotated[str | None, typer.Option("--category", help="Category")] = None,
    languages: Annotated[
        str | None, typer.Option("--languages", "-l", help="Comma-separated languages")
    ] = None,
    tags: Annotated[str | None, typer.Option("--tags", help="Comma-separated tags")] = None,
    table_out: Annotated[
        bool, typer.Option("--table", "-t", help="Table output (human-readable)")
    ] = False,
) -> None:
    """Create a new entity. Default: JSON output."""
    if entity_type not in ENTITY_TYPES:
        error(f"Invalid entity type: {entity_type}")
        info(f"Valid types: {', '.join(ENTITY_TYPES)}")
        return

    @run_async
    async def _create() -> None:
        client = get_client()

        try:
            lang_list = [lang.strip() for lang in languages.split(",")] if languages else None
            tag_list = [tag.strip() for tag in tags.split(",")] if tags else None

            if table_out:
                with spinner("Creating entity...") as progress:
                    progress.add_task("Creating entity...", total=None)
                    response = await client.create_entity(
                        name=name,
                        content=content or f"{entity_type}: {name}",
                        entity_type=entity_type
                        if entity_type in ["episode", "pattern", "task", "project"]
                        else "episode",
                        category=category,
                        languages=lang_list,
                        tags=tag_list,
                    )
            else:
                response = await client.create_entity(
                    name=name,
                    content=content or f"{entity_type}: {name}",
                    entity_type=entity_type
                    if entity_type in ["episode", "pattern", "task", "project"]
                    else "episode",
                    category=category,
                    languages=lang_list,
                    tags=tag_list,
                )

            # JSON output (default)
            if not table_out:
                print_json(response)
                return

            # Table output
            if response.get("id"):
                success(f"Entity created: {response['id']}")
            else:
                error("Failed to create entity")

        except SibylClientError as e:
            _handle_client_error(e)

    _create()


@app.command("delete")
def delete_entity(
    entity_id: Annotated[str, typer.Argument(help="Entity ID to delete")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation")] = False,
    table_out: Annotated[
        bool, typer.Option("--table", "-t", help="Table output (human-readable)")
    ] = False,
) -> None:
    """Delete an entity (with confirmation). Default: JSON output."""
    if not yes:
        confirm = typer.confirm(f"Delete entity {entity_id[:8]}...? This cannot be undone.")
        if not confirm:
            info("Cancelled")
            return

    @run_async
    async def _delete() -> None:
        client = get_client()

        try:
            if table_out:
                with spinner("Deleting entity...") as progress:
                    progress.add_task("Deleting entity...", total=None)
                    await client.delete_entity(entity_id)
            else:
                await client.delete_entity(entity_id)

            # JSON output (default)
            if not table_out:
                response = {"deleted": True, "id": entity_id}
                print_json(response)
                return

            # Table output
            success(f"Entity deleted: {entity_id[:8]}...")

        except SibylClientError as e:
            _handle_client_error(e)

    _delete()


@app.command("related")
def related_entities(
    entity_id: Annotated[str, typer.Argument(help="Entity ID")],
    limit: Annotated[int, typer.Option("--limit", "-n", help="Max results")] = 20,
    table_out: Annotated[
        bool, typer.Option("--table", "-t", help="Table output (human-readable)")
    ] = False,
) -> None:
    """Show entities related to the given entity (1-hop). Default: JSON output."""

    @run_async
    async def _related() -> None:
        client = get_client()

        try:
            if table_out:
                with spinner("Finding related entities...") as progress:
                    progress.add_task("Finding related entities...", total=None)
                    response = await client.explore(
                        mode="related",
                        entity_id=entity_id,
                        limit=limit,
                    )
            else:
                response = await client.explore(
                    mode="related",
                    entity_id=entity_id,
                    limit=limit,
                )

            entities = response.get("entities", [])

            # JSON output (default)
            if not table_out:
                print_json(entities)
                return

            # Table output
            if not entities:
                info("No related entities found")
                return

            table = create_table("Related Entities", "ID", "Name", "Type", "Relationship")
            for e in entities:
                meta = e.get("metadata", {})
                rel_type = meta.get("relationship_type", "-") if meta else "-"
                table.add_row(
                    e.get("id", "")[:8] + "...",
                    truncate(e.get("name", ""), 30),
                    e.get("type", ""),
                    rel_type,
                )

            console.print(table)
            console.print(f"\n[dim]Found {len(entities)} related entity(ies)[/dim]")

        except SibylClientError as e:
            _handle_client_error(e)

    _related()
