"""Team memory management CLI commands."""

from __future__ import annotations

from typing import cast

import typer

from sibyl_cli import command_support
from sibyl_cli.client import SibylClientError, get_client
from sibyl_cli.common import print_json, run_async, success
from sibyl_cli.memory_views import print_team, print_team_list

app = typer.Typer(help="Team memory management commands")


@app.command("list")
def list_teams_cmd(
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
) -> None:
    """List teams in the active organization."""

    @run_async
    async def run_list_teams() -> None:
        try:
            async with get_client() as client:
                data = await client.list_teams()
            if json_output:
                print_json(data)
                return
            print_team_list(cast("dict[str, object]", data))
        except SibylClientError as e:
            command_support.handle_client_error(e)

    run_list_teams()


@app.command("create")
def create_team_cmd(
    name: str = typer.Argument(..., help="Team name"),
    slug: str | None = typer.Option(None, "--slug", help="Stable team slug"),
    description: str | None = typer.Option(None, "--description", "-d", help="Team description"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
) -> None:
    """Create a team and its team memory space."""

    @run_async
    async def run_create_team() -> None:
        try:
            async with get_client() as client:
                data = await client.create_team(
                    name=name,
                    slug=slug,
                    description=description,
                )
            if json_output:
                print_json(data)
                return
            print_team(cast("dict[str, object]", data))
            success(f"Created team: {data.get('slug') or data.get('id')}")
        except SibylClientError as e:
            command_support.handle_client_error(e)

    run_create_team()


@app.command("add-member")
def add_team_member_cmd(
    team_id: str = typer.Argument(..., help="Team ID or slug"),
    user_id: str = typer.Argument(..., help="User UUID"),
    role: str = typer.Option("member", "--role", "-r", help="Team role"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
) -> None:
    """Add or update a team member."""

    @run_async
    async def run_add_team_member() -> None:
        try:
            async with get_client() as client:
                data = await client.add_team_member(
                    team_id=team_id,
                    user_id=user_id,
                    role=role,
                )
            if json_output:
                print_json(data)
                return
            success(f"Added {data.get('user_id')} to team {team_id} as {data.get('role')}")
        except SibylClientError as e:
            command_support.handle_client_error(e)

    run_add_team_member()


@app.command("remove-member")
def remove_team_member_cmd(
    team_id: str = typer.Argument(..., help="Team ID or slug"),
    user_id: str = typer.Argument(..., help="User UUID"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
) -> None:
    """Remove a team member."""

    @run_async
    async def run_remove_team_member() -> None:
        try:
            async with get_client() as client:
                data = await client.remove_team_member(team_id=team_id, user_id=user_id)
            if json_output:
                print_json(data)
                return
            success(f"Removed {user_id} from team {team_id}")
        except SibylClientError as e:
            command_support.handle_client_error(e)

    run_remove_team_member()


@app.command("link-project")
def link_team_project_cmd(
    team_id: str = typer.Argument(..., help="Team ID or slug"),
    project_id: str = typer.Argument(..., help="Project UUID or graph project ID"),
    role: str = typer.Option(
        "project_contributor",
        "--role",
        "-r",
        help="Project role granted to the team",
    ),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
) -> None:
    """Grant a team access to a project."""

    @run_async
    async def run_link_team_project() -> None:
        try:
            async with get_client() as client:
                data = await client.link_team_project(
                    team_id=team_id,
                    project_id=project_id,
                    role=role,
                )
            if json_output:
                print_json(data)
                return
            success(
                f"Linked team {team_id} to project "
                f"{data.get('graph_project_id') or data.get('project_id')}"
            )
        except SibylClientError as e:
            command_support.handle_client_error(e)

    run_link_team_project()


@app.command("unlink-project")
def unlink_team_project_cmd(
    team_id: str = typer.Argument(..., help="Team ID or slug"),
    project_id: str = typer.Argument(..., help="Project UUID or graph project ID"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
) -> None:
    """Remove a team's project access."""

    @run_async
    async def run_unlink_team_project() -> None:
        try:
            async with get_client() as client:
                data = await client.unlink_team_project(team_id=team_id, project_id=project_id)
            if json_output:
                print_json(data)
                return
            success(f"Unlinked team {team_id} from project {project_id}")
        except SibylClientError as e:
            command_support.handle_client_error(e)

    run_unlink_team_project()
