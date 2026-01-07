"""Entity CRUD CLI commands.

Generic commands for all entity types: list, show, create, update, delete, related.
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
    create_panel,
    create_table,
    error,
    info,
    print_json,
    run_async,
    success,
    truncate,
)
from sibyl_core.models.entities import EntityType

app = typer.Typer(
    name="entity",
    help="Generic entity CRUD operations",
    no_args_is_help=True,
)

# Valid entity types - derived from canonical EntityType enum
ENTITY_TYPES = [e.value for e in EntityType]


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
    json_out: Annotated[
        bool, typer.Option("--json", "-j", help="JSON output (for scripting)")
    ] = False,
    csv_out: Annotated[bool, typer.Option("--csv", help="CSV output")] = False,
) -> None:
    """List entities by type with optional filters. Default: table output."""
    format_ = "json" if json_out else ("csv" if csv_out else "table")
    if entity_type not in ENTITY_TYPES:
        error(f"Invalid entity type: {entity_type}")
        info(f"Valid types: {', '.join(ENTITY_TYPES)}")
        return

    @run_async
    async def _list() -> None:
        client = get_client()

        try:
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
                    e.get("id", ""),
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
    json_out: Annotated[
        bool, typer.Option("--json", "-j", help="JSON output (for scripting)")
    ] = False,
) -> None:
    """Show detailed entity information with related context. Default: table output."""

    @run_async
    async def _show() -> None:
        client = get_client()

        try:
            entity = await client.get_entity(entity_id)

            # JSON output (default)
            if json_out:
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

            # Show related entities
            related_entities = entity.get("related", [])
            if related_entities:
                lines.extend(["", f"[{NEON_CYAN}]Related:[/{NEON_CYAN}]"])
                for rel in related_entities:
                    direction = "→" if rel.get("direction") == "outgoing" else "←"
                    lines.append(
                        f"  [{CORAL}]{rel.get('relationship', '')}[/{CORAL}] {direction} "
                        f"[{ELECTRIC_PURPLE}]{rel.get('entity_type', '')}[/{ELECTRIC_PURPLE}]: "
                        f"{rel.get('name', '')} [{CORAL}]{rel.get('id', '')}[/{CORAL}]"
                    )

            entity_type = entity.get("entity_type", "entity")
            panel = create_panel("\n".join(lines), title=f"{entity_type.title()} Details")
            console.print(panel)

        except SibylClientError as e:
            _handle_client_error(e)

    _show()


@app.command("create")
def create_entity(
    entity_type: Annotated[str, typer.Option("--type", "-T", help="Entity type (required)")],
    name: Annotated[str, typer.Option("--name", "-n", help="Entity name (required)")],
    content: Annotated[str | None, typer.Option("--content", "-c", help="Entity content")] = None,
    category: Annotated[str | None, typer.Option("--category", help="Category")] = None,
    languages: Annotated[
        str | None, typer.Option("--languages", "-l", help="Comma-separated languages")
    ] = None,
    tags: Annotated[str | None, typer.Option("--tags", help="Comma-separated tags")] = None,
    json_out: Annotated[
        bool, typer.Option("--json", "-j", help="JSON output (for scripting)")
    ] = False,
) -> None:
    """Create a new entity. Default: table output."""
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
            if json_out:
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
    json_out: Annotated[
        bool, typer.Option("--json", "-j", help="JSON output (for scripting)")
    ] = False,
) -> None:
    """Delete an entity. Default: table output."""

    @run_async
    async def _delete() -> None:
        client = get_client()

        try:
            await client.delete_entity(entity_id)

            # JSON output (default)
            if json_out:
                response = {"deleted": True, "id": entity_id}
                print_json(response)
                return

            # Table output
            success(f"Entity deleted: {entity_id}")

        except SibylClientError as e:
            _handle_client_error(e)

    _delete()


@app.command("related")
def related_entities(
    entity_id: Annotated[str, typer.Argument(help="Entity ID")],
    limit: Annotated[int, typer.Option("--limit", "-n", help="Max results")] = 20,
    json_out: Annotated[
        bool, typer.Option("--json", "-j", help="JSON output (for scripting)")
    ] = False,
) -> None:
    """Show entities related to the given entity (1-hop). Default: table output."""

    @run_async
    async def _related() -> None:
        client = get_client()

        try:
            response = await client.explore(
                mode="related",
                entity_id=entity_id,
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
                rel_type = meta.get("relationship_type", "-") if meta else "-"
                table.add_row(
                    e.get("id", ""),
                    truncate(e.get("name", ""), 30),
                    e.get("type", ""),
                    rel_type,
                )

            console.print(table)
            console.print(f"\n[dim]Found {len(entities)} related entity(ies)[/dim]")

        except SibylClientError as e:
            _handle_client_error(e)

    _related()


@app.command("history")
def entity_history(
    entity_id: Annotated[str, typer.Argument(help="Entity ID")],
    as_of: Annotated[
        str | None,
        typer.Option("--as-of", "-d", help="Point-in-time (ISO date, e.g. 2025-03-15)"),
    ] = None,
    mode: Annotated[
        str,
        typer.Option("--mode", "-m", help="history|timeline|conflicts"),
    ] = "history",
    include_expired: Annotated[
        bool, typer.Option("--include-expired", "-e", help="Include expired edges")
    ] = False,
    limit: Annotated[int, typer.Option("--limit", "-n", help="Max results")] = 50,
    json_out: Annotated[
        bool, typer.Option("--json", "-j", help="JSON output (for scripting)")
    ] = False,
) -> None:
    """Query bi-temporal history for an entity.

    Modes:
      history  - Edges as they existed at a point in time (use --as-of)
      timeline - All versions of edges over time (shows evolution)
      conflicts - Find invalidated/superseded facts

    Examples:
      sibyl entity history <id>                          # Current edges
      sibyl entity history <id> --as-of 2025-03-15      # As of March 15
      sibyl entity history <id> --mode timeline         # Full history
      sibyl entity history <id> --mode conflicts        # Superseded facts
    """

    @run_async
    async def _history() -> None:
        client = get_client()

        try:
            response = await client.temporal_query(
                mode=mode,
                entity_id=entity_id,
                as_of=as_of,
                include_expired=include_expired,
                limit=limit,
            )

            edges = response.get("edges", [])
            message = response.get("message")

            if json_out:
                print_json(response)
                return

            if not edges:
                if message:
                    info(message)
                else:
                    info(f"No edges found for {entity_id}")
                return

            # Build table based on mode
            if mode == "conflicts":
                table = create_table(
                    "Invalidated Facts",
                    "Source",
                    "Relationship",
                    "Target",
                    "Expired At",
                )
                for e in edges:
                    expired = e.get("expired_at") or e.get("invalid_at") or "-"
                    if isinstance(expired, str) and len(expired) > 10:
                        expired = expired[:10]  # Just date part
                    table.add_row(
                        truncate(e.get("source_name", ""), 20),
                        e.get("name", ""),
                        truncate(e.get("target_name", ""), 20),
                        str(expired),
                    )
            else:
                table = create_table(
                    f"Temporal History ({mode})",
                    "Source",
                    "Relationship",
                    "Target",
                    "Created",
                    "Status",
                )
                for e in edges:
                    created = e.get("created_at") or "-"
                    if isinstance(created, str) and len(created) > 10:
                        created = created[:10]
                    status = (
                        "[green]current[/green]" if e.get("is_current") else "[dim]expired[/dim]"
                    )
                    table.add_row(
                        truncate(e.get("source_name", ""), 18),
                        e.get("name", ""),
                        truncate(e.get("target_name", ""), 18),
                        str(created),
                        status,
                    )

            console.print(table)

            if message:
                console.print(f"\n[dim]{message}[/dim]")
            else:
                console.print(f"\n[dim]Found {len(edges)} edge(s)[/dim]")

            if as_of:
                console.print(f"[dim]Point-in-time: {as_of}[/dim]")

        except SibylClientError as e:
            _handle_client_error(e)

    _history()
