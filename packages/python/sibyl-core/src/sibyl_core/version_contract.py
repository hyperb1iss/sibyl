"""Header names and comparison rules for client/server version drift.

A client talking to a remote Sibyl cannot learn the server's version from
PyPI: the server may be pinned behind or ahead of the newest release. The
only authority is the server it is actually talking to, so the server
stamps its identity on every response and the client compares.

Both sides import these names from here so the strings cannot drift apart.
"""

from __future__ import annotations

from packaging.version import InvalidVersion, Version

# Stamped by the server on every response.
SERVER_VERSION_HEADER = "X-Sibyl-Version"
# Stamped only when the operator configures a floor. Absent means "no floor".
MIN_CLIENT_HEADER = "X-Sibyl-Min-Client"
# Sent by the client so the server can log or refuse stale callers.
CLIENT_VERSION_HEADER = "X-Sibyl-Client-Version"

__all__ = [
    "CLIENT_VERSION_HEADER",
    "MIN_CLIENT_HEADER",
    "SERVER_VERSION_HEADER",
    "client_is_below_floor",
    "parse_version",
    "server_is_ahead",
]


def parse_version(value: str | None) -> Version | None:
    """Parse a version string, returning None for anything unusable.

    Unparseable input is not an error: a client may be talking to an older
    server that sends nothing, or to a build stamped with a placeholder.
    Drift reporting degrades to silence rather than to a crash.
    """
    text = (value or "").strip()
    if not text:
        return None
    try:
        return Version(text)
    except InvalidVersion:
        return None


def server_is_ahead(*, client: str | None, server: str | None) -> bool:
    """True when the server is running a strictly newer version than the client."""
    client_version = parse_version(client)
    server_version = parse_version(server)
    if client_version is None or server_version is None:
        return False
    return server_version > client_version


def client_is_below_floor(*, client: str | None, minimum: str | None) -> bool:
    """True when the server declared a floor the client does not meet.

    An unparseable or absent floor is not enforced. Refusing to run because
    a header could not be read would turn a diagnostic into an outage.
    """
    minimum_version = parse_version(minimum)
    if minimum_version is None:
        return False
    client_version = parse_version(client)
    if client_version is None:
        return False
    return client_version < minimum_version
