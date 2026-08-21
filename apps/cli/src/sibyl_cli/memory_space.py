"""Memory-space access preview CLI commands."""

from __future__ import annotations

from typing import cast

import typer

from sibyl_cli import command_support
from sibyl_cli.client import SibylClientError, get_client
from sibyl_cli.common import print_json, run_async
from sibyl_cli.memory_views import print_access_preview

app = typer.Typer(help="Memory-space inspection and preview commands")


@app.command("preview-agent")
def memory_space_preview_agent(
    agent_id: str = typer.Argument(..., help="Agent principal ID"),
    space_id: str = typer.Option(..., "--space", help="Primary memory space ID"),
    additional_spaces: str | None = typer.Option(
        None,
        "--also-space",
        help="Comma-separated additional memory space IDs",
    ),
    limit: int = typer.Option(50, "--limit", "-l", min=1, max=200, help="Maximum sources"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
) -> None:
    """Preview what an agent could recall from selected memory spaces."""
    extra_space_ids = command_support.parse_csv_ids(additional_spaces)

    @run_async
    async def run_memory_space_preview_agent() -> None:
        try:
            async with get_client() as client:
                data = await client.preview_memory_space_access(
                    space_id=space_id,
                    target_principal_type="agent",
                    target_principal_id=agent_id,
                    additional_space_ids=extra_space_ids,
                    limit=limit,
                )
            if json_output:
                print_json(data)
                return
            print_access_preview(cast("dict[str, object]", data))
        except SibylClientError as e:
            command_support.handle_client_error(e)

    run_memory_space_preview_agent()
