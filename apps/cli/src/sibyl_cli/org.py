"""Organization CLI commands."""

from __future__ import annotations

from enum import Enum
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from sibyl_cli.auth_store import set_tokens
from sibyl_cli.client import SibylClientError, get_client
from sibyl_cli.common import error, print_json, run_async, success

app = typer.Typer(help="Organizations")
members_app = typer.Typer(help="Manage organization members")
app.add_typer(members_app, name="members")

console = Console()


class OrgRole(str, Enum):
    """Organization member roles."""

    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"
    VIEWER = "viewer"


@app.command("list")
def list_cmd() -> None:
    client = get_client()

    @run_async
    async def _run() -> dict:
        return await client.list_orgs()

    try:
        result = _run()
        print_json(result)
    except SibylClientError as e:
        error(str(e))


@app.command("create")
def create_cmd(
    name: str = typer.Option(..., "--name", "-n", help="Organization name"),
    slug: str | None = typer.Option(None, "--slug", help="Optional URL slug"),
    switch: bool = typer.Option(True, "--switch/--no-switch", help="Switch into it after create"),
) -> None:
    client = get_client()

    @run_async
    async def _run() -> dict:
        return await client.create_org(name=name, slug=slug)

    try:
        result = _run()
        if switch and "access_token" in result:
            token = str(result.get("access_token", "")).strip()
            refresh = str(result.get("refresh_token", "")).strip() or None
            expires_raw = result.get("expires_in")
            expires_in = int(expires_raw) if expires_raw is not None else None
            if token:
                set_tokens(client.base_url, token, refresh_token=refresh, expires_in=expires_in)
                success("Switched org (tokens saved to ~/.sibyl/auth.json)")
        print_json(result)
    except SibylClientError as e:
        error(str(e))


@app.command("switch")
def switch_cmd(slug: str) -> None:
    client = get_client()

    @run_async
    async def _run() -> dict:
        return await client.switch_org(slug)

    try:
        result = _run()
        token = str(result.get("access_token", "")).strip()
        refresh = str(result.get("refresh_token", "")).strip() or None
        expires_raw = result.get("expires_in")
        expires_in = int(expires_raw) if expires_raw is not None else None
        if token:
            set_tokens(client.base_url, token, refresh_token=refresh, expires_in=expires_in)
            success("Org switched (tokens saved to ~/.sibyl/auth.json)")
        print_json(result)
    except SibylClientError as e:
        error(str(e))


# =============================================================================
# Member Commands
# =============================================================================


@members_app.command("list")
def list_members_cmd(
    slug: Annotated[str, typer.Argument(help="Organization slug")],
    json_output: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
) -> None:
    """List all members of an organization."""
    client = get_client()

    @run_async
    async def _run() -> dict:
        return await client.list_org_members(slug)

    try:
        result = _run()
        members = result.get("members", [])

        if json_output:
            print_json(result)
            return

        if not members:
            console.print("[dim]No members found[/dim]")
            return

        table = Table(title=f"Members of {slug}")
        table.add_column("User", style="cyan")
        table.add_column("Email", style="dim")
        table.add_column("Role", style="magenta")
        table.add_column("Joined", style="dim")

        for member in members:
            user = member.get("user", {})
            table.add_row(
                user.get("name") or user.get("id", "Unknown"),
                user.get("email") or "-",
                member.get("role", "-"),
                member.get("created_at", "-")[:10] if member.get("created_at") else "-",
            )

        console.print(table)
    except SibylClientError as e:
        error(str(e))


@members_app.command("add")
def add_member_cmd(
    slug: Annotated[str, typer.Argument(help="Organization slug")],
    user_id: Annotated[str, typer.Argument(help="User ID to add")],
    role: Annotated[OrgRole, typer.Option("--role", "-r", help="Role to assign")] = OrgRole.MEMBER,
) -> None:
    """Add a member to an organization."""
    client = get_client()

    @run_async
    async def _run() -> dict:
        return await client.add_org_member(slug, user_id, role.value)

    try:
        result = _run()
        success(f"Added user {user_id} as {role.value}")
        print_json(result)
    except SibylClientError as e:
        error(str(e))


@members_app.command("remove")
def remove_member_cmd(
    slug: Annotated[str, typer.Argument(help="Organization slug")],
    user_id: Annotated[str, typer.Argument(help="User ID to remove")],
    force: Annotated[bool, typer.Option("--force", "-f", help="Skip confirmation")] = False,
) -> None:
    """Remove a member from an organization."""
    if not force:
        confirm = typer.confirm(f"Remove user {user_id} from {slug}?")
        if not confirm:
            raise typer.Abort()

    client = get_client()

    @run_async
    async def _run() -> dict:
        return await client.remove_org_member(slug, user_id)

    try:
        _run()
        success(f"Removed user {user_id} from {slug}")
    except SibylClientError as e:
        error(str(e))


@members_app.command("role")
def update_role_cmd(
    slug: Annotated[str, typer.Argument(help="Organization slug")],
    user_id: Annotated[str, typer.Argument(help="User ID")],
    role: Annotated[OrgRole, typer.Argument(help="New role")],
) -> None:
    """Update a member's role."""
    client = get_client()

    @run_async
    async def _run() -> dict:
        return await client.update_org_member_role(slug, user_id, role.value)

    try:
        result = _run()
        success(f"Updated {user_id} to {role.value}")
        print_json(result)
    except SibylClientError as e:
        error(str(e))
