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

import re
from urllib.parse import unquote, urlsplit

EMBEDDED_MEMORY_SCHEMES = frozenset({"memory", "mem"})
EMBEDDED_FILE_SCHEMES = frozenset({"surrealkv", "surrealkv+versioned", "file"})
EMBEDDED_SCHEMES = EMBEDDED_MEMORY_SCHEMES | EMBEDDED_FILE_SCHEMES
REMOTE_SCHEMES = frozenset({"ws", "wss", "http", "https"})
SUPPORTED_SCHEMES = EMBEDDED_SCHEMES | REMOTE_SCHEMES


# An RFC 3986 scheme token at the very start. Splitting on the first "://"
# instead would read "admin:secret@host/rpc?next=http://x" as having the
# scheme "admin:secret@host/rpc?next=http", and echo the password back in the
# unsupported-scheme error.
_SCHEME_PREFIX = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*)://")


def split_surreal_url(url: str) -> tuple[str, str] | None:
    """The lowercased scheme and everything after its "://", or None without one.

    The only place a URL is split into scheme and remainder. The input is
    trimmed first, deliberately, so a trailing newline from a secret file
    still parses; the scheme must then open the trimmed string.
    """
    stripped = url.strip()  # Trimmed first, deliberately: secret files end in a newline.
    match = _SCHEME_PREFIX.match(stripped)
    if match is None:
        return None
    return match.group(1).lower(), stripped[match.end() :]


def surreal_url_scheme(url: str) -> str:
    """The URL's scheme, lowercased as the SDK's urlparse sees it; '' if none."""
    parts = split_surreal_url(url)
    return parts[0] if parts else ""


def normalize_surreal_url(url: str) -> str:
    """The URL with its scheme lowercased, which every SDK code path accepts.

    Only a valid scheme token is touched; anything else is returned stripped
    but otherwise verbatim.
    """
    parts = split_surreal_url(url)
    return f"{parts[0]}://{parts[1]}" if parts else url.strip()


def surreal_url_host_port(url: str) -> tuple[str, int | None] | None:
    """The URL's host and port, or None when they do not parse.

    urlsplit raises, quoting the netloc, on some malformed hosts and on port
    text that is not a number; both come back as None here instead.
    """
    try:
        parsed = urlsplit(normalize_surreal_url(url))
        host, port = parsed.hostname, parsed.port
    except ValueError:
        return None
    if not host:
        return None
    return host, port


def _host_and_port(url: str) -> tuple[str, int | None] | None:
    """The URL's host (bracketed when IPv6) and port, or None if they do not parse."""
    location = surreal_url_host_port(url)
    if location is None:
        return None
    host, port = location
    return (f"[{host}]" if ":" in host else host), port


def redact_surreal_url(url: str) -> str:
    """A form of the URL that is safe to print or log.

    Only the scheme and, for a server, its host and port survive. Userinfo,
    path, query, and fragment are never shown, since any of them can carry a
    credential; an embedded store's path is dropped for the same reason.
    """
    scheme = surreal_url_scheme(url)
    if not scheme:
        return "(no URL scheme)"
    if scheme in EMBEDDED_SCHEMES:
        return f"{scheme}:// (embedded)"
    location = _host_and_port(url)
    if location is None:
        return f"{scheme}:// (host not parsed)"
    host, port = location
    return f"{scheme}://{host}" if port is None else f"{scheme}://{host}:{port}"


def surreal_http_base_url(url: str) -> str | None:
    """The server's plain-HTTP base URL for requests, without userinfo or query.

    A transport value, not a display one: it keeps the path prefix a proxy may
    need, and a path can itself carry a secret, so never show or return it.
    Show redact_surreal_url() instead. None for anything but a ws, wss, http,
    or https URL with a parseable host. Pass surreal_url_credentials() to the
    HTTP client for authentication.
    """
    scheme = surreal_url_scheme(url)
    if scheme not in REMOTE_SCHEMES:
        return None
    location = _host_and_port(url)
    if location is None:
        return None
    host, port = location
    http_scheme = "https" if scheme in {"wss", "https"} else "http"
    netloc = host if port is None else f"{host}:{port}"
    path = urlsplit(normalize_surreal_url(url)).path.rstrip("/").removesuffix("/rpc")
    return f"{http_scheme}://{netloc}{path}".rstrip("/")


_FRAGMENT_SEPARATORS = re.compile(r"[@:/?#&=;]+")
_HARMLESS_FRAGMENTS = frozenset({"rpc", "/rpc", "/rpc/", "sql", "http", "https", "ws", "wss"})


def _secret_fragments(url: str) -> list[str]:
    """Every piece of the URL that must not reach an error message or log.

    Worked out without urlsplit, which itself raises (echoing the netloc) on
    some malformed hosts. The scheme and a bare host are not secret; the
    userinfo, path, query, and fragment are, along with anything left over
    when the authority cannot be told apart from the path.
    """
    parts = split_surreal_url(url)
    if parts is None:
        secret = [url.strip()]
    elif parts[0] not in EMBEDDED_SCHEMES and surreal_url_host_port(url) is None:
        # No clean host and port: everything after the scheme is suspect,
        # including port text that is not a number.
        secret = [parts[1]]
    else:
        remainder = parts[1]
        authority_end = len(remainder)
        for marker in "/?#":
            index = remainder.find(marker)
            if index != -1:
                authority_end = min(authority_end, index)
        authority, after = remainder[:authority_end], remainder[authority_end:]
        userinfo = authority.rpartition("@")[0]
        secret = [userinfo, after] if authority else [remainder]
    fragments = set(secret)
    for piece in secret:
        fragments.update(_FRAGMENT_SEPARATORS.split(piece))
    return sorted(
        (
            fragment
            for fragment in fragments
            if len(fragment) >= 3 and fragment.lower() not in _HARMLESS_FRAGMENTS
        ),
        key=len,
        reverse=True,
    )


def safe_error_detail(error: BaseException, url: str) -> str:
    """The error's message when it quotes no secret part of the URL, else ''."""
    message = str(error).strip()
    lowered = message.lower()
    if any(fragment.lower() in lowered for fragment in _secret_fragments(url)):
        return ""
    return message


def surreal_url_credentials(url: str) -> tuple[str, str] | None:
    """The username and password embedded in the URL's userinfo, if any."""
    try:
        parsed = urlsplit(normalize_surreal_url(url))
    except ValueError:
        return None
    if parsed.username is None:
        return None
    return unquote(parsed.username), unquote(parsed.password or "")


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
    "redact_surreal_url",
    "safe_error_detail",
    "split_surreal_url",
    "surreal_http_base_url",
    "surreal_url_credentials",
    "surreal_url_host_port",
    "surreal_url_scheme",
    "unsupported_surreal_url_reason",
]
