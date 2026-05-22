"""Environment diagnostics for the Sibyl CLI."""

from __future__ import annotations

import json as _json
import os
import socket
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated
from urllib.parse import urlparse

import httpx
import typer

from sibyl_cli import config_store
from sibyl_cli.client import SibylClientError, get_client
from sibyl_cli.common import (
    ELECTRIC_YELLOW,
    ERROR_RED,
    NEON_CYAN,
    SUCCESS_GREEN,
    console,
    create_table,
    print_json,
    run_async,
)
from sibyl_cli.skill import canonical_skill_markdown, default_skill_roots
from sibyl_core.integration import AGENT_PROMPT_SNIPPET

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

# Markers delimit a Sibyl-managed block inside a user-owned CLAUDE.md/AGENTS.md
# file so `sibyl doctor --append` can update it idempotently on subsequent runs.
AGENT_BLOCK_BEGIN = "<!-- sibyl:agent-setup -->"
AGENT_BLOCK_END = "<!-- /sibyl:agent-setup -->"

# Heuristic anchors for "this CLAUDE.md teaches the memory loop." Look for the
# bridges header plus the three loop verbs; presence of all four signals the
# recommended snippet (or an equivalent rewrite) is in place.
AGENT_PROMPT_CONTENT_MARKERS = (
    "Intent -> verb",  # may appear verbatim or as "Intent → verb"
    "Intent → verb",
)
AGENT_PROMPT_LOOP_VERBS = ("recall", "remember", "reflect")

CLAUDE_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
LEGACY_USER_PROMPT_HOOK = Path.home() / ".claude" / "hooks" / "sibyl" / "user-prompt-submit.py"
AGENT_PROMPT_CANDIDATES = (
    Path.home() / ".claude" / "CLAUDE.md",
    Path.home() / ".codex" / "AGENTS.md",
    Path.cwd() / "CLAUDE.md",
    Path.cwd() / "AGENTS.md",
)


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    status: str
    message: str
    detail: str | None = None

    @property
    def failed(self) -> bool:
        return self.status == "fail"

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "name": self.name,
            "status": self.status,
            "message": self.message,
        }
        if self.detail:
            payload["detail"] = self.detail
        return payload


@dataclass(frozen=True)
class DoctorContext:
    name: str
    server_url: str
    insecure: bool = False
    source: str = "context"


def _api_health_url(server_url: str) -> str:
    return f"{server_url.rstrip('/')}/api/health"


def _is_local_server(server_url: str) -> bool:
    parsed = urlparse(server_url)
    return (parsed.hostname or "").lower() in LOCAL_HOSTS


def _server_host_port(server_url: str) -> tuple[str, int] | None:
    parsed = urlparse(server_url)
    if not parsed.hostname:
        return None
    if parsed.port:
        return parsed.hostname, parsed.port
    if parsed.scheme == "https":
        return parsed.hostname, 443
    if parsed.scheme == "http":
        return parsed.hostname, 80
    return None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def embedded_lock_path() -> Path:
    return config_store.config_dir() / "run" / "embedded-surreal.lock"


def _probe_port(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _load_config_context() -> tuple[list[DoctorCheck], DoctorContext | None]:
    path = config_store.config_path()
    if not path.exists():
        return [
            DoctorCheck(
                "config",
                "fail",
                "No Sibyl config exists.",
                f"Run 'sibyl init' to create {path}.",
            )
        ], None

    try:
        with open(path, "rb") as stream:
            raw_config = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return [
            DoctorCheck(
                "config",
                "fail",
                "Sibyl config is unreadable.",
                str(exc),
            )
        ], None

    config = config_store.load_config()
    active_name = str(config.get("active_context") or "").strip()
    contexts = raw_config.get("contexts", {})
    checks: list[DoctorCheck] = [DoctorCheck("config", "pass", f"Config file is readable: {path}")]

    if active_name:
        ctx = config_store.get_context(active_name)
        if ctx is None:
            checks.append(
                DoctorCheck(
                    "context",
                    "fail",
                    f"Active context '{active_name}' is missing.",
                    "Run 'sibyl context list' and 'sibyl context use <name>'.",
                )
            )
            return checks, None
        checks.append(DoctorCheck("context", "pass", f"Active context: {active_name}"))
        return checks, DoctorContext(
            name=ctx.name,
            server_url=ctx.server_url,
            insecure=ctx.insecure,
        )

    if contexts:
        checks.append(
            DoctorCheck(
                "context",
                "warn",
                "Contexts exist but none is active.",
                "Run 'sibyl context use <name>' to make writes explicit.",
            )
        )
        return checks, None

    server_url = str(config.get("server", {}).get("url") or "").strip()
    if not server_url:
        checks.append(
            DoctorCheck(
                "context",
                "fail",
                "No server URL is configured.",
                "Run 'sibyl init' or 'sibyl context create local --use'.",
            )
        )
        return checks, None

    checks.append(
        DoctorCheck(
            "context",
            "warn",
            "Using legacy server.url because no named context is active.",
            "Run 'sibyl init --force' to create an explicit local or remote context.",
        )
    )
    return checks, DoctorContext(name="legacy", server_url=server_url, source="legacy")


async def _check_public_health(context: DoctorContext, timeout: float) -> DoctorCheck:
    url = _api_health_url(context.server_url)
    try:
        async with httpx.AsyncClient(timeout=timeout, verify=not context.insecure) as client:
            response = await client.get(url)
    except httpx.ConnectError as exc:
        return DoctorCheck("daemon", "fail", "Sibyl API is not reachable.", str(exc))
    except httpx.TimeoutException as exc:
        return DoctorCheck("daemon", "fail", "Sibyl API health check timed out.", str(exc))
    except httpx.HTTPError as exc:
        return DoctorCheck("daemon", "fail", "Sibyl API health check failed.", str(exc))

    if response.status_code != 200:
        return DoctorCheck(
            "daemon",
            "fail",
            f"Sibyl API returned HTTP {response.status_code}.",
            url,
        )

    try:
        data = response.json()
    except ValueError:
        return DoctorCheck("daemon", "fail", "Sibyl API returned non-JSON health data.", url)

    status = str(data.get("status") or "unknown")
    if status != "healthy":
        return DoctorCheck("daemon", "fail", f"Sibyl API is {status}.", url)
    version = str(data.get("version") or "").strip()
    suffix = f" ({version})" if version else ""
    return DoctorCheck("daemon", "pass", f"Sibyl API is healthy{suffix}.", url)


def _check_local_port(context: DoctorContext, timeout: float, health: DoctorCheck) -> DoctorCheck:
    host_port = _server_host_port(context.server_url)
    if host_port is None:
        return DoctorCheck("port", "warn", "Could not parse local server port.")
    host, port = host_port
    if _probe_port(host, port, timeout):
        status = "pass" if health.status == "pass" else "fail"
        message = (
            f"Port {host}:{port} is serving Sibyl."
            if health.status == "pass"
            else f"Port {host}:{port} is open but Sibyl health failed."
        )
        return DoctorCheck("port", status, message)
    return DoctorCheck(
        "port",
        "fail",
        f"Port {host}:{port} is closed.",
        "Run 'sibyl serve' for local mode or switch contexts.",
    )


def _check_embedded_lock(
    *,
    lock_path: os.PathLike[str] | str | None = None,
    pid_alive: Callable[[int], bool] = _pid_alive,
) -> DoctorCheck:
    path = Path(lock_path) if lock_path is not None else embedded_lock_path()
    if not path.exists():
        return DoctorCheck(
            "embedded-lock",
            "warn",
            "No embedded SurrealDB lockfile was found.",
            f"Expected at {path}; this is fine for remote or Docker contexts.",
        )

    try:
        with open(path, "rb") as stream:
            data = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return DoctorCheck("embedded-lock", "fail", "Embedded lockfile is unreadable.", str(exc))

    pid_value = data.get("pid")
    if not isinstance(pid_value, int | str):
        return DoctorCheck("embedded-lock", "fail", "Embedded lockfile does not contain a PID.")
    try:
        pid = int(pid_value)
    except (TypeError, ValueError):
        return DoctorCheck("embedded-lock", "fail", "Embedded lockfile does not contain a PID.")

    if pid_alive(pid):
        return DoctorCheck("embedded-lock", "pass", f"Embedded lock is held by PID {pid}.")
    return DoctorCheck(
        "embedded-lock",
        "fail",
        f"Embedded lock is stale; PID {pid} is not running.",
        f"Remove {path} only after confirming no sibyld process is active.",
    )


async def _check_write_probe(enabled: bool) -> DoctorCheck:
    if not enabled:
        return DoctorCheck("write-test", "warn", "Write probe skipped by --no-write-test.")
    try:
        async with get_client() as client:
            data = await client._request("POST", "/admin/write-test", _buffer_pending=False)
    except SibylClientError as exc:
        return DoctorCheck(
            "write-test",
            "fail",
            "Authenticated write probe failed.",
            exc.remediation or exc.detail or str(exc),
        )

    status = str(data.get("status") or "unknown")
    if status != "ok":
        return DoctorCheck("write-test", "fail", f"Write probe returned {status}.")
    return DoctorCheck("write-test", "pass", "Authenticated write probe succeeded.")


def _check_skill_stub() -> DoctorCheck:
    """Verify the canonical skill stub is installed at expected roots."""
    try:
        expected = canonical_skill_markdown()
    except (OSError, FileNotFoundError) as exc:
        return DoctorCheck(
            "skill-stub",
            "fail",
            "Canonical skill markdown is unreadable.",
            str(exc),
        )

    roots = default_skill_roots()
    present: list[Path] = []
    stale: list[Path] = []
    missing: list[Path] = []
    for root in roots:
        target = root / "sibyl" / "SKILL.md"
        if not target.exists():
            missing.append(target)
            continue
        try:
            installed = target.read_text(encoding="utf-8")
        except OSError:
            stale.append(target)
            continue
        if installed.strip() == expected.strip():
            present.append(target)
        else:
            stale.append(target)

    if stale:
        return DoctorCheck(
            "skill-stub",
            "fail",
            "Installed skill stub is out of date.",
            "Run 'sibyl skill install --force' to refresh: " + ", ".join(str(p) for p in stale),
        )
    if not present:
        return DoctorCheck(
            "skill-stub",
            "fail",
            "Sibyl skill stub is not installed in any assistant root.",
            "Run 'sibyl skill install' or 'sibyl local setup'.",
        )
    if missing:
        return DoctorCheck(
            "skill-stub",
            "warn",
            f"Skill stub installed at {len(present)} of {len(roots)} roots.",
            "Missing: " + ", ".join(str(p) for p in missing),
        )
    return DoctorCheck(
        "skill-stub",
        "pass",
        f"Canonical skill stub installed at all {len(roots)} assistant roots.",
    )


def _load_claude_settings() -> dict | None:
    if not CLAUDE_SETTINGS_PATH.exists():
        return None
    try:
        return _json.loads(CLAUDE_SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, _json.JSONDecodeError):
        return None


def _is_sibyl_hook_entry(entry: dict) -> bool:
    for hook in entry.get("hooks") or []:
        cmd = str(hook.get("command", ""))
        if "sibyl" in cmd or "hooks/sibyl" in cmd:
            return True
    return False


def _check_session_hook() -> DoctorCheck:
    settings = _load_claude_settings()
    if settings is None:
        return DoctorCheck(
            "session-hook",
            "warn",
            "Claude settings.json not present; skipping SessionStart hook check.",
            f"Expected at {CLAUDE_SETTINGS_PATH}.",
        )
    hooks = settings.get("hooks") or {}
    session_entries = hooks.get("SessionStart") or []
    if any(_is_sibyl_hook_entry(e) for e in session_entries):
        return DoctorCheck(
            "session-hook",
            "pass",
            "SessionStart hook is registered.",
        )
    return DoctorCheck(
        "session-hook",
        "warn",
        "SessionStart hook is not registered in Claude settings.",
        "Run 'sibyl local setup' to install the wake-up bundle hook.",
    )


def _check_no_legacy_hook() -> DoctorCheck:
    settings = _load_claude_settings() or {}
    hooks = settings.get("hooks") or {}
    user_prompt_entries = hooks.get("UserPromptSubmit") or []
    settings_has_legacy = any(_is_sibyl_hook_entry(e) for e in user_prompt_entries)
    file_present = LEGACY_USER_PROMPT_HOOK.exists()
    if not settings_has_legacy and not file_present:
        return DoctorCheck("legacy-hook", "pass", "No legacy UserPromptSubmit hook is installed.")
    detail_parts: list[str] = []
    if settings_has_legacy:
        detail_parts.append(
            f"settings.json still registers UserPromptSubmit at {CLAUDE_SETTINGS_PATH}"
        )
    if file_present:
        detail_parts.append(f"orphan script at {LEGACY_USER_PROMPT_HOOK}")
    return DoctorCheck(
        "legacy-hook",
        "fail",
        "Legacy UserPromptSubmit hook still present.",
        "Run 'sibyl local setup' to prune. " + " · ".join(detail_parts),
    )


def _agent_prompt_has_bridges(text: str) -> bool:
    lowered = text.lower()
    has_bridges_header = any(marker.lower() in lowered for marker in AGENT_PROMPT_CONTENT_MARKERS)
    has_loop_verbs = all(verb in lowered for verb in AGENT_PROMPT_LOOP_VERBS)
    return has_bridges_header and has_loop_verbs


def _check_agent_prompt_content() -> DoctorCheck:
    found_path: Path | None = None
    found_with_bridges: Path | None = None
    seen: list[Path] = []
    for candidate in AGENT_PROMPT_CANDIDATES:
        if not candidate.exists():
            continue
        seen.append(candidate)
        try:
            text = candidate.read_text(encoding="utf-8")
        except OSError:
            continue
        if found_path is None:
            found_path = candidate
        if _agent_prompt_has_bridges(text):
            found_with_bridges = candidate
            break

    if found_with_bridges is not None:
        return DoctorCheck(
            "agent-prompt",
            "pass",
            f"Recommended memory-loop content found in {found_with_bridges}.",
        )
    if found_path is not None:
        return DoctorCheck(
            "agent-prompt",
            "warn",
            f"Found {found_path} but it does not include the recommended bridges.",
            "Run 'sibyl doctor --append <path>' to add a managed block, or copy "
            "the block printed by this command into the file by hand.",
        )
    return DoctorCheck(
        "agent-prompt",
        "warn",
        "No CLAUDE.md or AGENTS.md found in the standard locations.",
        "Looked at: " + ", ".join(str(p) for p in AGENT_PROMPT_CANDIDATES),
    )


def collect_agent_checks() -> list[DoctorCheck]:
    """Filesystem-only checks: skill stub, hooks, and CLAUDE.md content."""
    return [
        _check_skill_stub(),
        _check_session_hook(),
        _check_no_legacy_hook(),
        _check_agent_prompt_content(),
    ]


async def collect_doctor_checks(
    *, timeout: float, write_test: bool, skip_agent: bool = False
) -> list[DoctorCheck]:
    checks, context = _load_config_context()
    if context is not None:
        health = await _check_public_health(context, timeout)
        checks.append(health)
        if _is_local_server(context.server_url):
            checks.append(_check_local_port(context, timeout, health))
            checks.append(_check_embedded_lock())
        else:
            checks.append(DoctorCheck("port", "warn", "Port probe skipped for remote context."))
            checks.append(
                DoctorCheck(
                    "embedded-lock", "warn", "Embedded lock probe skipped for remote context."
                )
            )
        checks.append(await _check_write_probe(write_test))
    if not skip_agent:
        checks.extend(collect_agent_checks())
    return checks


def append_managed_block(target: Path, snippet: str = AGENT_PROMPT_SNIPPET) -> str:
    """Append or replace the Sibyl-managed agent-setup block in target.

    Returns "appended" when no managed block existed before, "updated" when an
    existing block was rewritten in place. Raises OSError on filesystem failure.
    """
    existing = target.read_text(encoding="utf-8") if target.exists() else ""
    block = f"{AGENT_BLOCK_BEGIN}\n{snippet.rstrip()}\n{AGENT_BLOCK_END}"

    if AGENT_BLOCK_BEGIN in existing and AGENT_BLOCK_END in existing:
        before, _, rest = existing.partition(AGENT_BLOCK_BEGIN)
        _, _, after = rest.partition(AGENT_BLOCK_END)
        new = before.rstrip() + "\n\n" + block + "\n" + after.lstrip()
        target.write_text(new, encoding="utf-8")
        return "updated"

    separator = "\n\n" if existing and not existing.endswith("\n\n") else ""
    target.write_text(existing + separator + block + "\n", encoding="utf-8")
    return "appended"


def _render_checks(checks: list[DoctorCheck]) -> None:
    table = create_table("Sibyl Doctor", "Check", "Status", "Message", "Detail")
    colors = {"pass": SUCCESS_GREEN, "warn": ELECTRIC_YELLOW, "fail": ERROR_RED}
    labels = {"pass": "PASS", "warn": "WARN", "fail": "FAIL"}
    for check in checks:
        color = colors.get(check.status, NEON_CYAN)
        table.add_row(
            check.name,
            f"[{color}]{labels.get(check.status, check.status.upper())}[/{color}]",
            check.message,
            check.detail or "",
        )
    console.print(table)


def doctor(
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Output as JSON")] = False,
    timeout: Annotated[float, typer.Option("--timeout", help="Network timeout in seconds")] = 2.0,
    write_test: Annotated[
        bool,
        typer.Option("--write-test/--no-write-test", help="Run the authenticated write probe"),
    ] = True,
    skip_agent: Annotated[
        bool,
        typer.Option("--skip-agent", help="Skip agent-setup checks (skill stub, hooks, CLAUDE.md)"),
    ] = False,
    append: Annotated[
        Path | None,
        typer.Option(
            "--append",
            help="Append the recommended agent-setup block to the given CLAUDE.md/AGENTS.md",
        ),
    ] = None,
) -> None:
    """Diagnose Sibyl config, daemon health, locks, write readiness, and agent setup."""

    @run_async
    async def _run() -> None:
        if append is not None:
            action = append_managed_block(append)
            if not json_output:
                color = SUCCESS_GREEN
                verb = (
                    "Updated managed block in"
                    if action == "updated"
                    else "Appended managed block to"
                )
                console.print(f"[{color}]✓[/{color}] {verb} {append}")
                console.print(
                    f"  Markers: {AGENT_BLOCK_BEGIN} / {AGENT_BLOCK_END}; re-run to update."
                )

        checks = await collect_doctor_checks(
            timeout=timeout, write_test=write_test, skip_agent=skip_agent
        )
        ok = not any(check.failed for check in checks)

        agent_prompt_failed = any(c.name == "agent-prompt" and c.status != "pass" for c in checks)

        if json_output:
            payload: dict[str, object] = {
                "ok": ok,
                "checks": [check.to_dict() for check in checks],
            }
            if append is not None:
                payload["append"] = {"target": str(append), "action": action}
            print_json(payload)
        else:
            _render_checks(checks)
            if agent_prompt_failed and append is None:
                console.print()
                console.print(
                    f"[{NEON_CYAN}]Recommended agent-setup block "
                    f"(paste into your CLAUDE.md / AGENTS.md, or use --append):[/{NEON_CYAN}]"
                )
                console.print()
                console.print(AGENT_PROMPT_SNIPPET)

        if not ok:
            raise typer.Exit(1)

    _run()
