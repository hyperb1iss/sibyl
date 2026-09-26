"""`sibyl setup`: connect this machine to a Sibyl server in one command.

Every install path ends here (Homebrew, uv, the curl installer, and the agent
setup document), so this is the one implementation of the steps: pick a
context for the server, sign in, install the skill, add the Claude Code hook,
and verify. Each step checks first and skips work that is already done.
"""

from __future__ import annotations

import ipaddress
import os
import shlex
import shutil
import sys
from collections.abc import Iterator
from contextlib import contextmanager
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
from sibyl_cli.client_transport import _paired_automation_api_url
from sibyl_cli.common import ELECTRIC_PURPLE, NEON_CYAN, console, error, run_async, warn
from sibyl_cli.skill import install_canonical_skill
from sibyl_cli.version_drift import client_version
from sibyl_core.integration import (
    BREW_FORMULA,
    LOGIN_TIMEOUT_MINUTES,
    UV_INSTALL_COMMAND,
    is_clean_server_url,
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
    url = urlunsplit((parts.scheme, parts.netloc, path, "", ""))
    if not is_clean_server_url(url):
        raise typer.BadParameter(f"Not a plain http(s) server URL: {raw}")
    return url


def is_loopback(server_url: str) -> bool:
    """True for localhost, 127.0.0.0/8 and ::1, the only hosts plain http may reach."""
    host = (urlsplit(server_url).hostname or "").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


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


def _existing_context(server_url: str, name: str | None) -> config_store.Context | None:
    """The context already pointing at `server_url`, preferring the selected one."""
    matches = _matching_contexts(server_url, name)
    if not matches:
        return None
    current = config_store.resolve_context_name()
    return next((ctx for ctx in matches if ctx.name == current), matches[0])


def _new_context_name(server_url: str, name: str | None) -> str:
    """Name a new context after the server, adding the port when the plain name is taken."""
    if name:
        candidates = [name]
    else:
        parts = urlsplit(server_url)
        host = parts.hostname or "remote"
        base = "local" if host in LOCAL_HOSTS else host
        candidates = [base, f"{base}-{parts.port}"] if parts.port else [base]
    for candidate in candidates:
        if config_store.get_context(candidate) is None:
            return candidate
    error(f"Context '{candidates[-1]}' already points at another server.")
    console.print(f"  Name a new one: sibyl setup {shlex.quote(server_url)} --context <name>")
    raise typer.Exit(1)


def _activate(ctx: config_store.Context, *, created: bool) -> str:
    """Make `ctx` the active context once sign-in worked; returns the checklist detail."""
    if created:
        config_store.set_active_context(ctx.name)
        clear_client_cache()
        return f"{ctx.name} (created, active)"
    if ctx.name == config_store.resolve_context_name():
        return f"{ctx.name} (active)"
    config_store.set_active_context(ctx.name)
    clear_client_cache()
    return f"{ctx.name} (now active)"


def _warn_if_pinned_elsewhere(ctx: config_store.Context) -> None:
    """A -C flag, SIBYL_CONTEXT, or directory pin outranks the active context."""
    selected = config_store.resolve_context_name()
    if selected and selected != ctx.name:
        warn(
            f"This shell selects context '{selected}' (via -C, SIBYL_CONTEXT, or a directory "
            f"pin), so other commands here still use it. Switch with: sibyl -C {ctx.name} ..."
        )


@contextmanager
def only_target_credentials(server_url: str) -> Iterator[None]:
    """Keep an automation token away from a server it was not issued for.

    SIBYL_AUTH_TOKEN wins over stored logins, and without a paired
    SIBYL_API_URL the client sends it to whatever server a command targets.
    For the length of setup it stays in play only when SIBYL_API_URL names
    this same server, so every request setup makes authenticates with the
    target's own stored login or with nothing.
    """
    token = os.environ.get("SIBYL_AUTH_TOKEN", "").strip()
    paired = _paired_automation_api_url()
    target = normalize_api_url(f"{server_url}/api")
    if not token or (paired and normalize_api_url(paired) == target):
        yield
        return
    warn(
        "SIBYL_AUTH_TOKEN is set for another server and was not sent here. Other "
        "commands in this shell still send it; unset it to use this login."
    )
    saved = os.environ.pop("SIBYL_AUTH_TOKEN")
    clear_client_cache()
    try:
        yield
    finally:
        os.environ["SIBYL_AUTH_TOKEN"] = saved
        clear_client_cache()


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


def sign_in(ctx: config_store.Context) -> str | None:
    """The signed-in identity, running the browser login when there is none yet."""
    identity = whoami(ctx)
    if identity is not None:
        return identity
    console.print("\n  Opening your browser to sign in. Approve the code there.")
    try:
        login(ctx)
    except typer.Exit:
        # The login already printed why; setup reports the failed step.
        return None
    console.print()
    return whoami(ctx)


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
    check = doctor._check_session_hook()
    script = setup.CLAUDE_HOOKS_DIR / "session-start.py"
    if check.status == "pass" and script.exists():
        return Step("Hook", True, "already registered")
    unreadable = f"{setup.CLAUDE_SETTINGS_FILE} is not valid Claude settings; left unchanged"
    if check.status == "fail":
        return Step("Hook", False, unreadable)
    data_dir = setup.get_package_data_dir()
    if data_dir is None or not setup.install_hooks_copy(data_dir):
        return Step("Hook", False, "hook script missing from this CLI package")
    if not setup.configure_claude_hooks():
        return Step("Hook", False, unreadable)
    return Step("Hook", True, "Claude Code SessionStart")


def setup_cmd(
    url: Annotated[
        str | None,
        typer.Argument(help="Server URL, e.g. https://sibyl.example.com (default: current server)"),
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
    insecure: Annotated[
        bool,
        typer.Option(
            "--insecure",
            "-k",
            help="Skip TLS verification, and allow plain http to a host that is not this machine",
        ),
    ] = False,
) -> None:
    """Connect this machine to a Sibyl server: sign in, install the skill and hooks."""
    # Without a URL, set up whatever server the CLI already talks to: the
    # selected context, or the local default.
    server_url = normalize_server_url(url or config_store.get_effective_server_url())
    with only_target_credentials(server_url):
        _run_setup(
            server_url,
            from_url=bool(url),
            yes=yes,
            context=context,
            no_hooks=no_hooks,
            insecure=insecure,
        )


def _run_setup(
    server_url: str,
    *,
    from_url: bool,
    yes: bool,
    context: str | None,
    no_hooks: bool,
    insecure: bool,
) -> None:

    console.print()
    console.print(f"[{ELECTRIC_PURPLE}]◈[/{ELECTRIC_PURPLE}] [bold]Sibyl setup[/bold] {server_url}")
    console.print()

    # Sign-in sends a password or a token over this connection.
    if server_url.startswith("http://") and not insecure and not is_loopback(server_url):
        _print_step(Step("Server", False, "plain http to another machine would expose sign-in"))
        console.print(
            "\n  Use the https URL, or pass --insecure to allow http on a network you trust."
        )
        raise typer.Exit(1)

    if not yes and interactive():
        hook = "" if no_hooks else f", and add {HOOK_PURPOSE}"
        console.print(
            f"  Connects this CLI, signs you in, installs the Sibyl skill for your agents{hook}."
        )
        if not typer.confirm("  Continue?", default=True):
            raise typer.Exit(1)
        console.print()

    # TLS trust comes from the flag or the chosen context's own setting, never
    # from a sibling context that happens to point at the same server.
    existing = _existing_context(server_url, context)
    skip_verify = insecure or bool(existing and existing.insecure)
    try:
        version, minimum = probe_server(server_url, insecure=skip_verify)
    except (httpx.HTTPError, ValueError) as exc:
        _print_step(Step("Server", False, f"unreachable: {exc}"))
        if not from_url:
            console.print("\n  Pass the server to connect to: sibyl setup https://your-sibyl-host")
        raise typer.Exit(1) from exc
    current = client_version()
    if client_is_below_floor(client=current, minimum=minimum):
        _print_step(Step("Server", False, f"needs sibyl {minimum}+, this CLI is {current}"))
        console.print(f"\n  Upgrade, then re-run: [{NEON_CYAN}]{upgrade_command()}[/{NEON_CYAN}]")
        raise typer.Exit(1)
    _print_step(Step("Server", True, f"sibyl {version or 'unknown version'}"))

    # A new context stays inactive until sign-in works, and is removed if it
    # does not, so a failed setup never leaves the CLI pointed at a server it
    # cannot talk to.
    created = existing is None
    if existing is None:
        ctx = config_store.create_context(
            _new_context_name(server_url, context), server_url=server_url, insecure=insecure
        )
    elif insecure and not existing.insecure:
        ctx = config_store.update_context(existing.name, insecure=True)
    else:
        ctx = existing
    clear_client_cache()

    identity = sign_in(ctx)
    _print_step(Step("Signed in", identity is not None, identity or "sign-in did not complete"))
    if identity is None:
        if created:
            config_store.delete_context(ctx.name)
            clear_client_cache()
        console.print()
        error("Setup is incomplete. Sign in, then re-run this command.")
        raise typer.Exit(1)

    context_detail = _activate(ctx, created=created)
    if insecure and not (existing and existing.insecure):
        context_detail += ", skips TLS verification"
    _print_step(Step("Context", True, context_detail))
    _warn_if_pinned_elsewhere(ctx)

    steps = [ensure_skill()]
    if not no_hooks:
        steps.append(ensure_hook())
    for step in steps:
        _print_step(step)

    console.print()
    if not all(step.ok for step in steps):
        error("Setup is incomplete. Fix the step above, then re-run this command.")
        raise typer.Exit(1)
    console.print(f"  Try it: [{NEON_CYAN}]{FIRST_TRY}[/{NEON_CYAN}]")
