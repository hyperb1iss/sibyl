"""`sibyl setup`: connect this machine to a Sibyl server in one command.

Every install path ends here (Homebrew, uv, the curl installer, and the agent
setup document), so this is the one implementation of the steps: pick a
context for the server, sign in, install the skill, add the Claude Code hook,
and verify. Each step checks first and skips work that is already done.
"""

from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit, urlunsplit

import httpx
import typer

from sibyl_cli import config_store, doctor, setup
from sibyl_cli.auth import _login_auto
from sibyl_cli.auth_store import credential_scope, normalize_api_url
from sibyl_cli.client import SibylClient, SibylClientError, clear_client_cache
from sibyl_cli.common import ELECTRIC_PURPLE, NEON_CYAN, console, error, run_async
from sibyl_cli.skill import install_canonical_skill
from sibyl_cli.version_drift import client_version
from sibyl_core.integration import (
    BREW_FORMULA,
    LOGIN_TIMEOUT_MINUTES,
    UV_INSTALL_COMMAND,
)
from sibyl_core.version_contract import (
    MIN_CLIENT_HEADER,
    SERVER_VERSION_HEADER,
    client_is_below_floor,
)

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
FIRST_TRY = 'sibyl context "what\'s in flight"'
HOOK_PURPOSE = "a Claude Code SessionStart hook that loads your active tasks and recent memory"


@dataclass(frozen=True)
class Step:
    label: str
    ok: bool
    detail: str


def _print_step(step: Step) -> None:
    mark = "[green]✓[/green]" if step.ok else "[red]✗[/red]"
    console.print(f"  {mark} {step.label:<10} [dim]{step.detail}[/dim]")


def normalize_server_url(raw: str) -> str:
    """Accept what people paste: a web URL, an API URL, or a trailing slash."""
    text = raw.strip()
    if "://" not in text:
        text = f"https://{text}"
    parts = urlsplit(text)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise typer.BadParameter(f"Not a server URL: {raw}")
    path = parts.path.rstrip("/").removesuffix("/api")
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def upgrade_command() -> str:
    """How to upgrade this CLI, matched to how it was installed."""
    prefix = Path(sys.prefix).as_posix().lower()
    if "/cellar/" in prefix or "homebrew" in prefix or "linuxbrew" in prefix:
        return f"brew upgrade {BREW_FORMULA}"
    return UV_INSTALL_COMMAND


def interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def probe_server(server_url: str, *, insecure: bool) -> tuple[str | None, str | None]:
    """Return the server's version and version floor from its public health route."""
    response = httpx.get(
        f"{server_url}/api/health", timeout=10, verify=not insecure, follow_redirects=True
    )
    response.raise_for_status()
    body = response.json() if response.content else {}
    version = response.headers.get(SERVER_VERSION_HEADER) or body.get("version")
    return version, response.headers.get(MIN_CLIENT_HEADER)


def _matching_contexts(server_url: str, name: str | None) -> list[config_store.Context]:
    target = normalize_api_url(f"{server_url}/api")
    return [
        ctx
        for ctx in config_store.list_contexts()
        if normalize_api_url(f"{ctx.server_url.rstrip('/')}/api") == target
        and (name is None or ctx.name == name)
    ]


def _context_for(server_url: str, name: str | None) -> tuple[config_store.Context, str]:
    """Select the context for `server_url`, creating one when none points there."""
    matches = _matching_contexts(server_url, name)
    current = config_store.resolve_context_name()
    if matches:
        chosen = next((ctx for ctx in matches if ctx.name == current), matches[0])
        if chosen.name == current:
            return chosen, f"{chosen.name} (active)"
        config_store.set_active_context(chosen.name)
        clear_client_cache()
        return chosen, f"{chosen.name} (now active)"

    host = urlsplit(server_url).hostname or "remote"
    context_name = name or ("local" if host in LOCAL_HOSTS else host)
    if config_store.get_context(context_name) is not None:
        error(f"Context '{context_name}' already points at another server.")
        console.print(f"  Name a new one: sibyl setup {server_url} --context <name>")
        raise typer.Exit(1)
    ctx = config_store.create_context(context_name, server_url=server_url, set_active=True)
    clear_client_cache()
    return ctx, f"{context_name} (created, active)"


def whoami(ctx: config_store.Context) -> str | None:
    """Who the saved credentials sign in as, or None when there is no valid login."""
    client = SibylClient(context_name=ctx.name)

    @run_async
    async def _me() -> dict:
        try:
            return await client.get("/auth/me")
        finally:
            await client.close()

    try:
        me = _me()
    except SibylClientError:
        return None
    user = me.get("user") or {}
    return str(user.get("email") or user.get("name") or "signed in")


def login(ctx: config_store.Context) -> None:
    api_url = normalize_api_url(f"{ctx.server_url}/api")
    _login_auto(
        api_url=api_url,
        no_browser=False,
        timeout_seconds=LOGIN_TIMEOUT_MINUTES * 60,
        email=None,
        password=None,
        insecure=ctx.insecure,
        credential_scope_name=credential_scope(ctx.name, ctx.org_slug),
    )
    clear_client_cache()


def ensure_skill() -> Step:
    if doctor._check_skill_stub().status == "pass":
        return Step("Skill", True, "already installed")
    install_canonical_skill()
    check = doctor._check_skill_stub()
    if check.status == "fail":
        return Step("Skill", False, check.message)
    return Step("Skill", True, "installed for Claude Code, Codex, and other agents")


def claude_code_present() -> bool:
    # Not ~/.claude alone: installing the skill creates that directory itself.
    home = Path.home()
    return (
        shutil.which("claude") is not None
        or (home / ".claude.json").exists()
        or (home / ".claude" / "settings.json").exists()
    )


def ensure_hook() -> Step:
    """Register the Claude Code SessionStart hook; other agents have no hook."""
    if not claude_code_present():
        return Step("Hook", True, "skipped: Claude Code not found, other agents need none")
    script = setup.CLAUDE_HOOKS_DIR / "session-start.py"
    if doctor._check_session_hook().status == "pass" and script.exists():
        return Step("Hook", True, "already registered")
    data_dir = setup.get_package_data_dir()
    if data_dir is None or not setup.install_hooks_copy(data_dir):
        return Step("Hook", False, "hook script missing from this CLI package")
    if not setup.configure_claude_hooks():
        return Step("Hook", False, f"could not update {setup.CLAUDE_SETTINGS_FILE}")
    return Step("Hook", True, "Claude Code SessionStart")


def setup_cmd(
    url: Annotated[
        str | None,
        typer.Argument(help="Server URL, e.g. https://sibyl.example.com (default: active context)"),
    ] = None,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Don't ask; for agents and scripts")
    ] = False,
    context: Annotated[
        str | None,
        typer.Option("--context", "-c", help="Context name to use or create for this server"),
    ] = None,
    no_hooks: Annotated[
        bool, typer.Option("--no-hooks", help="Skip the Claude Code SessionStart hook")
    ] = False,
) -> None:
    """Connect this machine to a Sibyl server: sign in, install the skill and hooks."""
    if url:
        server_url = normalize_server_url(url)
    else:
        current = config_store.resolve_effective_context()
        if current is None:
            error("Which server? Run: sibyl setup https://your-sibyl-server")
            raise typer.Exit(1)
        server_url = normalize_server_url(current.server_url)

    console.print()
    console.print(f"[{ELECTRIC_PURPLE}]◈[/{ELECTRIC_PURPLE}] [bold]Sibyl setup[/bold] {server_url}")
    console.print()

    if not yes and interactive():
        hook = "" if no_hooks else f", and add {HOOK_PURPOSE}"
        console.print(
            f"  Connects this CLI, signs you in, installs the Sibyl skill for your agents{hook}."
        )
        if not typer.confirm("  Continue?", default=True):
            raise typer.Exit(1)
        console.print()

    insecure = any(ctx.insecure for ctx in _matching_contexts(server_url, context))
    try:
        version, minimum = probe_server(server_url, insecure=insecure)
    except (httpx.HTTPError, ValueError) as exc:
        _print_step(Step("Server", False, f"unreachable: {exc}"))
        raise typer.Exit(1) from exc
    current = client_version()
    if client_is_below_floor(client=current, minimum=minimum):
        _print_step(Step("Server", False, f"needs sibyl {minimum}+, this CLI is {current}"))
        console.print(f"\n  Upgrade, then re-run: [{NEON_CYAN}]{upgrade_command()}[/{NEON_CYAN}]")
        raise typer.Exit(1)
    _print_step(Step("Server", True, f"sibyl {version or 'unknown version'}"))

    ctx, context_detail = _context_for(server_url, context)
    _print_step(Step("Context", True, context_detail))

    identity = whoami(ctx)
    if identity is None:
        console.print("\n  Opening your browser to sign in. Approve the code there.")
        login(ctx)
        console.print()
        identity = whoami(ctx)
    _print_step(Step("Signed in", identity is not None, identity or "sign-in did not complete"))

    steps = [ensure_skill()]
    if not no_hooks:
        steps.append(ensure_hook())
    for step in steps:
        _print_step(step)

    console.print()
    if identity is None or not all(step.ok for step in steps):
        error("Setup is incomplete. Fix the step above, then re-run this command.")
        raise typer.Exit(1)
    console.print(f"  Try it: [{NEON_CYAN}]{FIRST_TRY}[/{NEON_CYAN}]")
