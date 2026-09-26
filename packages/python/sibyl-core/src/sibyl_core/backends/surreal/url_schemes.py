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


# Path segments that name an endpoint rather than a deployment detail.
_ENDPOINT_SEGMENTS = frozenset(
    {"rpc", "sql", "health", "metrics", "status", "version", "signin", "signup", "key"}
)
_PIECE_SEPARATORS = re.compile(r"[@:/?#&=;]+")


def _url_components(remainder: str) -> tuple[str, str, str, str, str]:
    """Userinfo, host and port text, path, query, and fragment, without urlsplit."""
    authority_end = len(remainder)
    for marker in "/?#":
        index = remainder.find(marker)
        if index != -1:
            authority_end = min(authority_end, index)
    authority, after = remainder[:authority_end], remainder[authority_end:]
    userinfo, _, hostport = authority.rpartition("@")
    after, _, fragment = after.partition("#")
    path, _, query = after.partition("?")
    return userinfo, hostport, path, query, fragment


def _secret_parts(url: str) -> set[str]:
    """Every raw piece of the URL that must not reach an error message or log.

    Worked out without urlsplit, which itself raises (quoting the netloc) on
    some malformed hosts. The scheme, a clean host, its port, and endpoint path
    segments such as "rpc" are not secret. The userinfo (whole, user, and
    password), every other path segment, the query string and each query
    value, and the fragment are, and so is the URL itself. When the host and
    port do not parse, everything after the scheme is suspect.
    """
    stripped = url.strip()
    parts = split_surreal_url(stripped)
    if parts is None:
        pieces = {stripped, *_PIECE_SEPARATORS.split(stripped)}
        return {piece for piece in pieces if piece}
    scheme, remainder = parts
    secret = {stripped, remainder, f"{scheme}://{remainder}"}
    userinfo, hostport, path, query, fragment = _url_components(remainder)
    if userinfo:
        user, _, password = userinfo.partition(":")
        secret.update({userinfo, user, password})
    embedded = scheme in EMBEDDED_SCHEMES
    for segment in path.split("/"):
        if segment and (embedded or segment.lower() not in _ENDPOINT_SEGMENTS):
            secret.add(segment)
    if path and path.strip("/").lower() not in _ENDPOINT_SEGMENTS:
        secret.add(path)
    if query:
        secret.add(query)
        for pair in query.split("&"):
            key, _, value = pair.partition("=")
            secret.update({pair, value} if value else {key})
    if fragment:
        secret.add(fragment)
    if not embedded and surreal_url_host_port(stripped) is None:
        # No clean host and port: the port text and anything else in the
        # authority may be secret too.
        secret.update({hostport, *_PIECE_SEPARATORS.split(remainder)})
    return {piece for piece in secret if piece}


def canonical_url_text(text: str) -> str:
    """One comparable form of any URL-bearing text.

    Percent-decoded repeatedly until stable, "+" read as a space, and
    casefolded. HTTP stacks normalize the URL they print in their own ways
    (aiohttp decodes %3A but keeps %2F), so the secret pieces and the error
    text are both reduced to this form and compared there, rather than
    enumerating spellings that can never cover every mix.
    """
    current = text
    for _ in range(8):
        decoded = unquote(current)
        if decoded == current:
            break
        current = decoded
    return current.replace("+", " ").casefold()


# A piece shorter than this cannot be told apart from ordinary text ("T" in a
# macOS temp path would match "connect"), so matching it would scrub every
# error without protecting anything.
_MIN_SECRET_LENGTH = 3


def canonical_url_secrets(url: str) -> list[str]:
    """Every secret piece of the URL in canonical form, longest first."""
    pieces = {canonical_url_text(piece) for piece in _secret_parts(url)}
    return sorted(
        (piece for piece in pieces if len(piece) >= _MIN_SECRET_LENGTH),
        key=len,
        reverse=True,
    )


def text_mentions_url_secret(text: str, url: str) -> bool:
    """Whether the text quotes a secret piece of the URL, in any normalization."""
    canonical = canonical_url_text(text)
    return any(piece in canonical for piece in canonical_url_secrets(url))


def _chain(error: BaseException) -> list[BaseException]:
    """The error, then its causes and contexts, each once."""
    seen: set[int] = set()
    chain: list[BaseException] = []
    pending: list[BaseException] = [error]
    while pending and len(chain) < 32:
        current = pending.pop(0)
        if id(current) in seen:
            continue
        seen.add(id(current))
        chain.append(current)
        pending.extend(link for link in (current.__cause__, current.__context__) if link)
    return chain


def _error_texts(error: BaseException) -> list[str]:
    texts: list[str] = []
    for link in _chain(error):
        texts.append(str(link))
        texts.append(repr(link))
        texts.extend(str(arg) for arg in getattr(link, "args", ()))
    return texts


def error_mentions_url_secret(error: BaseException, url: str) -> bool:
    """Whether the error or anything chained to it quotes a secret piece of the URL.

    Both sides are compared in canonical form (canonical_url_text), since an
    HTTP stack may echo the URL it requested in its own normalization.
    """
    secrets = canonical_url_secrets(url)
    for text in _error_texts(error):
        canonical = canonical_url_text(text)
        if any(piece in canonical for piece in secrets):
            return True
    return False


def safe_error_detail(error: BaseException, url: str) -> str:
    """The error's message when neither it nor its chain quotes the URL, else ''."""
    if error_mentions_url_secret(error, url):
        return ""
    return str(error).strip()


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
    "canonical_url_secrets",
    "canonical_url_text",
    "error_mentions_url_secret",
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
    "text_mentions_url_secret",
    "unsupported_surreal_url_reason",
]
