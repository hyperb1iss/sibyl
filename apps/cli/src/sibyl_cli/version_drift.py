"""Notice when this CLI and the server it is talking to have drifted apart.

`sibyl update` compares the installed CLI against PyPI, which is the wrong
axis for a remote server: the server may be pinned behind or ahead of the
newest release, so matching PyPI proves nothing. The authority is the
server actually being talked to, and it stamps its version on every
response.

Warnings go to stderr and are rate-limited to once per server version per
day, so `--json` output stays parseable and a drifted setup does not nag
on every command.
"""

from __future__ import annotations

import time
from contextlib import suppress

from rich.console import Console

from sibyl_cli import config_store
from sibyl_core.version_contract import (
    MIN_CLIENT_HEADER,
    SERVER_VERSION_HEADER,
    client_is_below_floor,
    server_is_ahead,
)

# stderr, so a drift notice never lands in `--json` output that a script
# or an agent is parsing.
_stderr = Console(stderr=True)

_WARN_INTERVAL_SECONDS = 24 * 60 * 60
_CONFIG_KEY = "version_drift"

# One notice per process regardless of how many requests it makes.
_notified: set[str] = set()


class ClientTooOldError(RuntimeError):
    """The server declared a minimum client version this CLI does not meet."""

    def __init__(self, *, client: str, minimum: str) -> None:
        self.client = client
        self.minimum = minimum
        super().__init__(
            f"This server requires sibyl {minimum} or newer; you are running {client}."
        )


def client_version() -> str:
    from sibyl_cli import __version__

    return str(__version__)


def _warn_state() -> dict[str, dict[str, object]]:
    state = config_store.get(_CONFIG_KEY, {})
    return state if isinstance(state, dict) else {}


def _should_warn(*, base_url: str, server: str, now: float) -> bool:
    entry = _warn_state().get(base_url)
    if not isinstance(entry, dict):
        return True
    # A newly-changed server version is always worth one notice, even if we
    # warned about the previous one an hour ago.
    if entry.get("version") != server:
        return True
    last = entry.get("at")
    if not isinstance(last, int | float):
        return True
    return (now - float(last)) >= _WARN_INTERVAL_SECONDS


def _record_warning(*, base_url: str, server: str, now: float) -> None:
    state = _warn_state()
    state[base_url] = {"version": server, "at": int(now)}
    # Never let bookkeeping break the command the user actually ran; the
    # cost of a failed write is warning again next time.
    with suppress(OSError):
        config_store.set_value(_CONFIG_KEY, state)


def check_response_headers(
    headers: object,
    *,
    base_url: str,
    now: float | None = None,
) -> None:
    """Compare this CLI against the server that produced `headers`.

    Raises ClientTooOldError when the server declares a floor this client
    does not meet. Otherwise warns at most once per server version per day.
    """
    get = getattr(headers, "get", None)
    if not callable(get):
        return

    server = get(SERVER_VERSION_HEADER)
    minimum = get(MIN_CLIENT_HEADER)
    current = client_version()

    if client_is_below_floor(client=current, minimum=minimum):
        raise ClientTooOldError(client=current, minimum=str(minimum))

    if base_url in _notified:
        return
    if not server_is_ahead(client=current, server=server):
        return

    _notified.add(base_url)
    now = time.time() if now is None else now
    if not _should_warn(base_url=base_url, server=str(server), now=now):
        return

    _stderr.print(
        f"[yellow]![/yellow] Server is [bold]{server}[/bold], "
        f"this CLI is [bold]{current}[/bold] — run [cyan]sibyl update[/cyan]"
    )
    _record_warning(base_url=base_url, server=str(server), now=now)


def reset_process_state() -> None:
    """Clear the once-per-process guard. For tests."""
    _notified.clear()
