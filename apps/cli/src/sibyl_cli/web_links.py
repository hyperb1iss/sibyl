"""Links from the CLI into the web app.

The web app is served from the same origin as the API in every deployment
behind one ingress, so its base is the context's server URL with any ``/api``
path dropped. The local development stack is the exception: its API listens
on ``localhost:3334`` and the web app on ``localhost:3337``.
"""

from __future__ import annotations

from urllib.parse import urlencode, urlsplit, urlunsplit

LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
LOCAL_API_PORT = 3334
LOCAL_WEB_PORT = 3337


def web_base(server_url: str) -> str:
    """The web app's base URL for a server or API URL."""
    parts = urlsplit(server_url.strip().rstrip("/"))
    path = parts.path.rstrip("/")
    if path.endswith("/api"):
        path = path[: -len("/api")]
    netloc = parts.netloc
    if parts.hostname in LOCAL_HOSTS and parts.port == LOCAL_API_PORT:
        host = f"[{parts.hostname}]" if ":" in (parts.hostname or "") else parts.hostname
        netloc = f"{host}:{LOCAL_WEB_PORT}"
    return urlunsplit((parts.scheme, netloc, path, "", ""))


def net_url(server_url: str, channel: str | None = None, thread: str | None = None) -> str:
    """The web Net page, opened on a channel and thread when given."""
    query = {key: value for key, value in (("channel", channel), ("thread", thread)) if value}
    page = f"{web_base(server_url)}/net"
    return f"{page}?{urlencode(query)}" if query else page


__all__ = ["net_url", "web_base"]
