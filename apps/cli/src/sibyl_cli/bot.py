"""Bot account CLI commands.

A bot is an agent's own identity: a member of your organization that you
sponsor, which signs in only with API keys and never holds more on a project
than you do.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any
from uuid import UUID

import typer
from rich.markup import escape

from sibyl_cli.client import SibylClientError, get_client
from sibyl_cli.common import (
    console,
    create_table,
    handle_client_error,
    info,
    print_json,
    run_async,
    success,
)

app = typer.Typer(help="Bot accounts: agent identities you sponsor")
key_app = typer.Typer(help="Issue and revoke a bot's API keys")
app.add_typer(key_app, name="key")

JsonOption = Annotated[bool, typer.Option("--json", "-j", help="Output as JSON")]
BotArgument = Annotated[str, typer.Argument(help="Bot ID or name")]
_ROLE_ALIASES = {"viewer": "project_viewer", "contributor": "project_contributor"}


def _project_role(role: str) -> str:
    normalized = role.strip().lower()
    return _ROLE_ALIASES.get(normalized, normalized)


async def _resolve_bot(client: Any, ref: str) -> str:
    """A bot ID as given, or the one bot you can see with this exact name."""
    try:
        return str(UUID(ref))
    except ValueError:
        pass
    listing = await client.list_bots()
    bots = listing.get("bots", []) if isinstance(listing, Mapping) else []
    matches = [bot for bot in bots if str(bot.get("name", "")).casefold() == ref.casefold()]
    if len(matches) == 1:
        return str(matches[0]["id"])
    if not matches:
        raise SibylClientError(f"No bot named {ref!r}", status_code=404, detail="Bot not found")
    ids = ", ".join(str(bot["id"]) for bot in matches)
    raise SibylClientError(
        f"More than one bot is named {ref!r}; use an ID ({ids})",
        status_code=400,
        detail=f"Several bots are named {ref!r}; use one of: {ids}",
    )


def _sponsor_label(bot: Mapping[str, Any]) -> str:
    sponsor = bot.get("sponsor")
    if not isinstance(sponsor, Mapping):
        return "-"
    return str(sponsor.get("name") or sponsor.get("email") or sponsor.get("id") or "-")


def _print_bot(bot: Mapping[str, Any]) -> None:
    status = str(bot.get("status") or "")
    console.print(f"[bold]{escape(str(bot.get('name') or ''))}[/bold]  {escape(str(bot['id']))}")
    console.print(f"  sponsor: {escape(_sponsor_label(bot))}")
    console.print(f"  status:  {escape(status)}")
    if status == "suspended":
        info(
            "Its sponsor is no longer a member, so its keys are refused until an admin transfers it."
        )
    projects = bot.get("projects")
    if isinstance(projects, list):
        if not projects:
            info("No project grants yet. Grant one with `sibyl bot grant`.")
            return
        table = create_table("Projects", "Project", "Project ID", "Role")
        for grant in projects:
            table.add_row(
                escape(str(grant.get("project_name") or "")),
                escape(str(grant.get("project_id") or "")),
                escape(str(grant.get("role") or "")),
            )
        console.print(table)


@app.command("create")
def create_cmd(
    name: Annotated[str, typer.Argument(help="Bot name, e.g. oncall")],
    json_output: JsonOption = False,
) -> None:
    """Create a bot you sponsor."""

    @run_async
    async def run() -> dict[str, Any]:
        async with get_client() as client:
            return await client.create_bot(name)

    try:
        bot = run()
    except SibylClientError as e:
        handle_client_error(e)
        return
    if json_output:
        print_json(bot)
        return
    success(f"Created bot {escape(str(bot.get('name')))} ({escape(str(bot.get('id')))})")
    info("Next: `sibyl bot grant <bot> <project>`, then `sibyl bot key create <bot> --name ...`.")


@app.command("list")
def list_cmd(json_output: JsonOption = False) -> None:
    """List bots: every bot for an org admin, your own otherwise."""

    @run_async
    async def run() -> dict[str, Any]:
        async with get_client() as client:
            return await client.list_bots()

    try:
        listing = run()
    except SibylClientError as e:
        handle_client_error(e)
        return
    if json_output:
        print_json(listing)
        return
    bots = listing.get("bots", []) if isinstance(listing, Mapping) else []
    if not bots:
        info("No bots yet. Create one with `sibyl bot create <name>`.")
        return
    table = create_table("Bots", "Name", "ID", "Sponsor", "Status", "Created")
    for bot in bots:
        table.add_row(
            escape(str(bot.get("name") or "")),
            escape(str(bot.get("id") or "")),
            escape(_sponsor_label(bot)),
            escape(str(bot.get("status") or "")),
            escape(str(bot.get("created_at") or "")[:10]),
        )
    console.print(table)


@app.command("show")
def show_cmd(bot: BotArgument, json_output: JsonOption = False) -> None:
    """Show a bot, its sponsor and its project grants."""

    @run_async
    async def run() -> dict[str, Any]:
        async with get_client() as client:
            return await client.get_bot(await _resolve_bot(client, bot))

    try:
        data = run()
    except SibylClientError as e:
        handle_client_error(e, not_found_label="Bot not found")
        return
    if json_output:
        print_json(data)
        return
    _print_bot(data)


@app.command("grant")
def grant_cmd(
    bot: BotArgument,
    project: Annotated[str, typer.Argument(help="Project ID")],
    role: Annotated[
        str, typer.Option("--role", "-r", help="contributor or viewer")
    ] = "contributor",
    json_output: JsonOption = False,
) -> None:
    """Grant your bot a project role, up to min(your role there, contributor)."""

    @run_async
    async def run() -> dict[str, Any]:
        async with get_client() as client:
            bot_id = await _resolve_bot(client, bot)
            return await client.grant_bot_project(bot_id, project, _project_role(role))

    try:
        data = run()
    except SibylClientError as e:
        handle_client_error(e, not_found_label="Bot or project not found")
        return
    if json_output:
        print_json(data)
        return
    success(f"Granted {escape(str(data.get('role')))} on {escape(str(data.get('project_id')))}")


@app.command("revoke")
def revoke_cmd(
    bot: BotArgument,
    project: Annotated[str, typer.Argument(help="Project ID")],
    json_output: JsonOption = False,
) -> None:
    """Remove a bot's grant on a project."""

    @run_async
    async def run() -> dict[str, Any]:
        async with get_client() as client:
            return await client.revoke_bot_project(await _resolve_bot(client, bot), project)

    try:
        data = run()
    except SibylClientError as e:
        handle_client_error(e, not_found_label="Grant not found")
        return
    if json_output:
        print_json(data)
        return
    success(f"Revoked the bot's access to {escape(str(data.get('project_id')))}")


@app.command("transfer")
def transfer_cmd(
    bot: BotArgument,
    to: Annotated[str, typer.Option("--to", help="User ID of the new sponsor")],
    json_output: JsonOption = False,
) -> None:
    """Give a bot a new sponsor. Org admins only."""

    @run_async
    async def run() -> dict[str, Any]:
        async with get_client() as client:
            return await client.transfer_bot(await _resolve_bot(client, bot), to)

    try:
        data = run()
    except SibylClientError as e:
        handle_client_error(e, not_found_label="Bot not found")
        return
    if json_output:
        print_json(data)
        return
    success(f"{escape(str(data.get('name')))} is now sponsored by {escape(_sponsor_label(data))}")


@app.command("delete")
def delete_cmd(
    bot: BotArgument,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Don't ask first")] = False,
    json_output: JsonOption = False,
) -> None:
    """Delete a bot. Its keys stop working at once."""
    if not yes and not typer.confirm(f"Delete bot {bot} and revoke every key it holds?"):
        raise typer.Exit(1)

    @run_async
    async def run() -> dict[str, Any]:
        async with get_client() as client:
            return await client.delete_bot(await _resolve_bot(client, bot))

    try:
        data = run()
    except SibylClientError as e:
        handle_client_error(e, not_found_label="Bot not found")
        return
    if json_output:
        print_json(data)
        return
    success(
        f"Deleted bot {escape(str(data.get('id')))}; revoked {data.get('api_keys_revoked')} keys"
    )


def _split(value: str | None) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


@key_app.command("create")
def key_create_cmd(
    bot: BotArgument,
    name: Annotated[str, typer.Option("--name", "-n", help="Display name for the key")],
    scopes: Annotated[
        str, typer.Option("--scopes", help="Comma-separated scopes (mcp, api:read, api:write)")
    ] = "mcp,api:read,api:write",
    projects: Annotated[
        str | None, typer.Option("--projects", help="Comma-separated project IDs to narrow to")
    ] = None,
    memory_spaces: Annotated[
        str | None,
        typer.Option("--memory-spaces", help="Comma-separated memory-space IDs to narrow to"),
    ] = None,
    expires_days: Annotated[
        int | None, typer.Option("--expires-days", min=1, max=365, help="Expire after N days")
    ] = None,
    live: Annotated[bool, typer.Option("--live/--test", help="sk_live_ or sk_test_")] = True,
) -> None:
    """Issue a key that authenticates as the bot. The secret prints once."""

    @run_async
    async def run() -> dict[str, Any]:
        async with get_client() as client:
            return await client.create_bot_api_key(
                await _resolve_bot(client, bot),
                name=name,
                live=live,
                scopes=_split(scopes),
                project_ids=_split(projects),
                memory_space_ids=_split(memory_spaces),
                expires_days=expires_days,
            )

    try:
        print_json(run())
    except SibylClientError as e:
        handle_client_error(e, not_found_label="Bot not found")


@key_app.command("list")
def key_list_cmd(bot: BotArgument, json_output: JsonOption = False) -> None:
    """List a bot's API keys."""

    @run_async
    async def run() -> dict[str, Any]:
        async with get_client() as client:
            return await client.list_bot_api_keys(await _resolve_bot(client, bot))

    try:
        listing = run()
    except SibylClientError as e:
        handle_client_error(e, not_found_label="Bot not found")
        return
    if json_output:
        print_json(listing)
        return
    keys = listing.get("keys", []) if isinstance(listing, Mapping) else []
    if not keys:
        info("No keys. Issue one with `sibyl bot key create <bot> --name ...`.")
        return
    table = create_table("Bot API Keys", "Name", "ID", "Prefix", "Scopes", "Created", "Status")
    for key in keys:
        table.add_row(
            escape(str(key.get("name") or "")),
            escape(str(key.get("id") or "")),
            escape(str(key.get("prefix") or "")),
            escape(", ".join(str(scope) for scope in key.get("scopes") or [])),
            escape(str(key.get("created_at") or "")[:10]),
            "revoked" if key.get("revoked_at") else "active",
        )
    console.print(table)


@key_app.command("revoke")
def key_revoke_cmd(
    bot: BotArgument,
    key_id: Annotated[str, typer.Argument(help="API key ID")],
) -> None:
    """Revoke one of a bot's API keys."""

    @run_async
    async def run() -> dict[str, Any]:
        async with get_client() as client:
            return await client.revoke_bot_api_key(await _resolve_bot(client, bot), key_id)

    try:
        print_json(run())
    except SibylClientError as e:
        handle_client_error(e, not_found_label="API key not found")
