"""Raw capture archive CLI commands."""

from typing import Annotated

import typer
from rich.markup import escape

from sibyl_cli.client import SibylClientError, get_client
from sibyl_cli.common import (
    CORAL,
    ELECTRIC_PURPLE,
    NEON_CYAN,
    handle_client_error,
    run_async,
    success,
    truncate,
)
from sibyl_cli.view_shared import maybe_print_json, render_detail_panel, render_table_or_empty

app = typer.Typer(
    name="archive",
    help="Browse archived raw quick captures",
    no_args_is_help=True,
)


@app.command("list")
def list_archive(
    entity_type: Annotated[
        str | None, typer.Option("--type", "-t", help="Filter by entity type")
    ] = None,
    capture_surface: Annotated[
        str | None, typer.Option("--surface", help="Filter by capture surface")
    ] = None,
    limit: Annotated[int, typer.Option("--limit", "-n", help="Max results")] = 20,
    offset: Annotated[int, typer.Option("--offset", help="Skip first N results")] = 0,
    json_out: Annotated[
        bool, typer.Option("--json", "-j", help="JSON output (for scripting)")
    ] = False,
) -> None:
    """List archived raw quick captures."""

    @run_async
    async def _list() -> None:
        try:
            async with get_client() as client:
                response = await client.list_raw_captures(
                    entity_type=entity_type,
                    capture_surface=capture_surface,
                    limit=limit,
                    offset=offset,
                )

                if maybe_print_json(response, json_out=json_out):
                    return

                captures = response.get("captures", [])
                rows = [
                    (
                        capture.get("id", ""),
                        escape(truncate(capture.get("title", ""), 46)),
                        capture.get("entity_type", ""),
                        capture.get("capture_surface") or "unknown",
                    )
                    for capture in captures
                ]
                render_table_or_empty(
                    title="Raw Capture Archive",
                    columns=("ID", "Title", "Type", "Surface"),
                    rows=rows,
                    empty_message="No archived raw captures found",
                    empty_printer=success,
                    footer=(
                        f"\n[dim]More captures available. Try --offset {offset + limit}.[/dim]"
                        if response.get("has_more")
                        else None
                    ),
                )
        except SibylClientError as e:
            handle_client_error(e)

    _list()


@app.command("show")
def show_archive_capture(
    capture_id: Annotated[str, typer.Argument(help="Raw capture ID")],
    json_out: Annotated[
        bool, typer.Option("--json", "-j", help="JSON output (for scripting)")
    ] = False,
) -> None:
    """Show a single archived raw quick capture."""

    @run_async
    async def _show() -> None:
        try:
            async with get_client() as client:
                capture = await client.get_raw_capture(capture_id)

                if maybe_print_json(capture, json_out=json_out):
                    return

                lines = [
                    f"[{ELECTRIC_PURPLE}]Title:[/{ELECTRIC_PURPLE}] {escape(str(capture.get('title', '')))}",
                    f"[{ELECTRIC_PURPLE}]Type:[/{ELECTRIC_PURPLE}] {capture.get('entity_type', '')}",
                    f"[{ELECTRIC_PURPLE}]Archive ID:[/{ELECTRIC_PURPLE}] {capture.get('id', '')}",
                ]

                if capture.get("entity_id"):
                    lines.append(
                        f"[{ELECTRIC_PURPLE}]Entity ID:[/{ELECTRIC_PURPLE}] {capture.get('entity_id', '')}"
                    )
                if capture.get("capture_surface"):
                    lines.append(
                        f"[{ELECTRIC_PURPLE}]Surface:[/{ELECTRIC_PURPLE}] {capture.get('capture_surface', '')}"
                    )
                if capture.get("created_at"):
                    lines.append(
                        f"[{ELECTRIC_PURPLE}]Captured:[/{ELECTRIC_PURPLE}] {capture.get('created_at', '')}"
                    )
                if capture.get("tags"):
                    lines.append(
                        f"[{ELECTRIC_PURPLE}]Tags:[/{ELECTRIC_PURPLE}] {escape(', '.join(capture.get('tags', [])))}"
                    )

                metadata = capture.get("metadata", {})
                if metadata:
                    lines.extend(["", f"[{CORAL}]Metadata:[/{CORAL}]"])
                    for key, value in metadata.items():
                        lines.append(f"  {escape(str(key))}: {escape(truncate(str(value), 80))}")

                lines.extend(
                    [
                        "",
                        f"[{NEON_CYAN}]Raw Content:[/{NEON_CYAN}]",
                        escape(capture.get("raw_content") or ""),
                    ]
                )

                render_detail_panel(title="Raw Capture", lines=lines)
        except SibylClientError as e:
            handle_client_error(e)

    _show()
