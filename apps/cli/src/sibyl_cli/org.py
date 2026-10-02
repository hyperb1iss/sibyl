"""Organization CLI commands."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any

import typer
from rich.markup import escape
from rich.table import Table

from sibyl_cli import config_store
from sibyl_cli.auth_store import credential_scope, normalize_api_url, set_tokens
from sibyl_cli.client import SibylClientError, get_client
from sibyl_cli.client_transport import _environment_auth_token
from sibyl_cli.common import (
    console,
    create_table,
    error,
    info,
    print_json,
    run_async,
    success,
    warn,
)

app = typer.Typer(help="Organizations")
members_app = typer.Typer(help="Manage organization members")
app.add_typer(members_app, name="members")


def _switch_destination(client: Any, slug: str) -> tuple[str | None, str | None]:
    """Where a switched org's tokens belong: (credential scope, context to pin).

    Both follow the server the client actually talked to. A paired automation
    token (SIBYL_API_URL + SIBYL_AUTH_TOKEN) can aim the client away from the
    effective context, and filing its switched token under that context, or
    repointing it, would break a context the switch never touched.
    """
    if _environment_auth_token(client.base_url):
        warn(
            "SIBYL_AUTH_TOKEN authenticates this server, so it keeps deciding which org "
            "commands use; the switched credential is saved without a context."
        )
        return None, None
    ctx = config_store.resolve_effective_context()
    if ctx is None or normalize_api_url(f"{ctx.server_url}/api") != normalize_api_url(
        client.base_url
    ):
        return None, None
    return credential_scope(ctx.name, slug), ctx.name


def store_org_tokens(
    api_url: str,
    access_token: str,
    *,
    refresh_token: str | None,
    expires_in: int | None,
    scope_name: str | None,
) -> None:
    """Save a switched org's tokens and record who now owns buffered writes.

    An org switch starts a new credential lineage, which clears the previous
    queue owner, so the new one is recorded while the connection that produced
    the switch is still up.
    """
    from sibyl_cli.pending_identity import warm_pending_replay_identity

    set_tokens(
        api_url,
        access_token,
        refresh_token=refresh_token,
        expires_in=expires_in,
        credential_scope=scope_name,
    )
    warm_pending_replay_identity(api_url, access_token, credential_scope=scope_name)


def pin_context_org(context_name: str | None, slug: str) -> None:
    """Point the context at the org whose tokens were just stored.

    Credentials are keyed by context and org, so switched tokens saved under
    the new org's scope stay unread while the context still names the old
    org (or none): every later command would keep acting in the old org.
    """
    if not context_name or not slug:
        return
    ctx = config_store.get_context(context_name)
    if ctx is None or ctx.org_slug == slug:
        return
    config_store.update_context(context_name, org_slug=slug)
    info(f"Context '{context_name}' now uses org '{slug}'")


class OrgRole(StrEnum):
    """Organization member roles."""

    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"
    VIEWER = "viewer"


@app.command("list")
def list_cmd(
    json_output: Annotated[
        bool, typer.Option("--json", "-j", help="JSON output (for scripting)")
    ] = False,
) -> None:
    """List the organizations you belong to. Default: table output."""
    client = get_client()

    @run_async
    async def _run() -> dict:
        return await client.list_orgs()

    try:
        result = _run()
    except SibylClientError as e:
        error(str(e))
        raise typer.Exit(1) from e

    if json_output:
        print_json(result)
        return

    listing = result.get("orgs") if isinstance(result, dict) else None
    orgs = [org for org in listing or [] if isinstance(org, dict)]
    if not orgs:
        info("No organizations found")
        return

    table = create_table("Organizations", "Slug", "Name", "Role", "Type", "ID")
    for org in orgs:
        cells = (
            str(org.get("slug") or ""),
            str(org.get("name") or ""),
            str(org.get("role") or "-"),
            "personal" if org.get("is_personal") else "team",
            str(org.get("id") or ""),
        )
        # Org names are free text; unescaped brackets would parse as Rich markup.
        table.add_row(*(escape(cell) for cell in cells))
    console.print(table)


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
            organization = result.get("organization") or {}
            created_slug = str(organization.get("slug") or slug or "")
            if token and not created_slug:
                warn(
                    "The server did not report the new org's slug, so its credential "
                    "was not saved; switch into it with: sibyl org switch <slug>"
                )
            elif token:
                scope_name, pin_to = _switch_destination(client, created_slug)
                store_org_tokens(
                    client.base_url,
                    token,
                    refresh_token=refresh,
                    expires_in=expires_in,
                    scope_name=scope_name,
                )
                pin_context_org(pin_to, created_slug)
                success("Switched org (tokens saved to ~/.sibyl/auth.json)")
        print_json(result)
    except SibylClientError as e:
        error(str(e))
        raise typer.Exit(1) from e


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
            scope_name, pin_to = _switch_destination(client, slug)
            store_org_tokens(
                client.base_url,
                token,
                refresh_token=refresh,
                expires_in=expires_in,
                scope_name=scope_name,
            )
            pin_context_org(pin_to, slug)
            success("Org switched (tokens saved to ~/.sibyl/auth.json)")
        print_json(result)
    except SibylClientError as e:
        error(str(e))
        raise typer.Exit(1) from e


# =============================================================================
# Member Commands
# =============================================================================


@members_app.command("list")
def list_members_cmd(
    slug: Annotated[str, typer.Argument(help="Organization slug")],
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Output as JSON")] = False,
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
        raise typer.Exit(1) from e


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
        raise typer.Exit(1) from e


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
        raise typer.Exit(1) from e


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
        raise typer.Exit(1) from e
