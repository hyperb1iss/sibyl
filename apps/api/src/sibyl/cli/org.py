"""Organization CLI commands."""

from __future__ import annotations

import typer

from sibyl.cli.auth_store import set_tokens
from sibyl.cli.client import SibylClientError, get_client
from sibyl.cli.common import error, print_json, run_async, success

app = typer.Typer(help="Organizations")


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
