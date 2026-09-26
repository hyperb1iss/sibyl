"""Which SurrealDB URLs the bundled Python SDK serves, and how.

Every "is this store embedded" decision in Sibyl goes through these
predicates, so a scheme cannot be embedded for one check and remote for the
next. The scheme sets mirror the SDK's own dispatch (`surrealdb.UrlScheme`
and the embedded branch of `surrealdb.AsyncSurreal`); a test fails if they
drift apart.

- In-memory stores (`memory://`, `mem://`) live in the process and vanish
  with it, and every connection gets a fresh, empty store.
- File-backed stores (`surrealkv://`, `surrealkv+versioned://`, `file://`)
  run in the process over files on disk.
- Remote URLs (`ws://`, `wss://`, `http://`, `https://`) reach a server.

Schemes are case-insensitive, as URL schemes are. The SDK lowercases them to
pick a connection class, but its embedded engine then rejects the original
spelling ("Unsupported URL scheme: SURREALKV://..."), so the client
normalizes every URL with `normalize_surreal_url` before the SDK sees it.

`rocksdb://` is not on any list. The SDK rejects it ("'rocksdb' is not a
valid UrlScheme"); it is a storage argument for `surreal start`, not a client
URL, so configuration rejects it up front instead of letting the first
connect fail.
"""

from __future__ import annotations

EMBEDDED_MEMORY_SCHEMES = frozenset({"memory", "mem"})
EMBEDDED_FILE_SCHEMES = frozenset({"surrealkv", "surrealkv+versioned", "file"})
EMBEDDED_SCHEMES = EMBEDDED_MEMORY_SCHEMES | EMBEDDED_FILE_SCHEMES
REMOTE_SCHEMES = frozenset({"ws", "wss", "http", "https"})
SUPPORTED_SCHEMES = EMBEDDED_SCHEMES | REMOTE_SCHEMES


def surreal_url_scheme(url: str) -> str:
    """The URL's scheme, lowercased as the SDK's urlparse sees it; '' if none."""
    scheme, separator, _ = url.strip().partition("://")
    return scheme.lower() if separator else ""


def normalize_surreal_url(url: str) -> str:
    """The URL with its scheme lowercased, which every SDK code path accepts."""
    stripped = url.strip()
    scheme, separator, rest = stripped.partition("://")
    return f"{scheme.lower()}://{rest}" if separator else stripped


def is_websocket_surreal_url(url: str) -> bool:
    """Whether this URL reaches a server over a WebSocket (live queries need one)."""
    return surreal_url_scheme(url) in {"ws", "wss"}


def is_embedded_surreal_url(url: str) -> bool:
    """Whether the SDK serves this URL with an in-process engine."""
    return surreal_url_scheme(url) in EMBEDDED_SCHEMES


def is_memory_surreal_url(url: str) -> bool:
    """Whether this is an in-process store that lives only in memory."""
    return surreal_url_scheme(url) in EMBEDDED_MEMORY_SCHEMES


def is_file_backed_surreal_url(url: str) -> bool:
    """Whether this is an in-process engine over files on disk."""
    return surreal_url_scheme(url) in EMBEDDED_FILE_SCHEMES


def unsupported_surreal_url_reason(url: str) -> str | None:
    """Why the bundled SDK cannot open this URL, or None when it can."""
    scheme = surreal_url_scheme(url)
    if scheme in SUPPORTED_SCHEMES:
        return None
    supported = ", ".join(f"{name}://" for name in sorted(SUPPORTED_SCHEMES))
    if scheme == "rocksdb":
        return (
            "rocksdb:// is a storage argument for `surreal start`, not a client URL. "
            "Run a SurrealDB server on RocksDB and point SIBYL_SURREAL_URL at its "
            f"ws:// endpoint, or use surrealkv:// for an embedded store. Supported: {supported}"
        )
    # Name only the scheme: the rest of a URL can carry credentials.
    if not scheme:
        return f"SurrealDB URL has no scheme. Supported: {supported}"
    return f"SurrealDB URL scheme {scheme}:// is not supported. Supported: {supported}"


def production_surreal_url_problem(url: str, *, allow_embedded_single_writer: bool) -> str | None:
    """Why a production runtime must refuse this URL, or None when it may use it.

    Every in-memory spelling is refused, since the data would not survive a
    restart. Every file-backed spelling needs the explicit single-writer
    opt-in, since the embedded engine is safe only when one daemon owns it.
    """
    if is_memory_surreal_url(url):
        return (
            "In-memory SurrealDB is forbidden in production. "
            "Set SIBYL_SURREAL_URL or SIBYL_SURREAL_DATA_DIR."
        )
    if is_file_backed_surreal_url(url) and not allow_embedded_single_writer:
        return (
            "Embedded SurrealDB requires explicit single-writer opt-in in "
            "production. Set SIBYL_ALLOW_EMBEDDED_SINGLE_WRITER=1 only when one "
            "daemon owns the database."
        )
    return None


__all__ = [
    "EMBEDDED_FILE_SCHEMES",
    "EMBEDDED_MEMORY_SCHEMES",
    "EMBEDDED_SCHEMES",
    "REMOTE_SCHEMES",
    "SUPPORTED_SCHEMES",
    "is_embedded_surreal_url",
    "is_file_backed_surreal_url",
    "is_memory_surreal_url",
    "is_websocket_surreal_url",
    "normalize_surreal_url",
    "production_surreal_url_problem",
    "surreal_url_scheme",
    "unsupported_surreal_url_reason",
]
