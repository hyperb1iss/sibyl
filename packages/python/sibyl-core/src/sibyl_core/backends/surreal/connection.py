"""Shared SurrealDB connection retry helpers."""

from __future__ import annotations

import re

from sibyl_core.backends.surreal.url_schemes import (
    error_mentions_url_secret,
    redact_surreal_url,
    safe_error_detail,
    surreal_url_scheme,
    text_mentions_url_secret,
)

_READ_ONLY_QUERY_TOKENS = {"SELECT", "RETURN", "INFO", "SHOW"}
_RAW_READ_ONLY_QUERY_TOKENS = {*_READ_ONLY_QUERY_TOKENS, "LET"}
_WRITE_QUERY_TOKENS = {
    "ALTER",
    "BEGIN",
    "CANCEL",
    "COMMIT",
    "CREATE",
    "DEFINE",
    "DELETE",
    # Stored functions can write even when invoked through SELECT or RETURN.
    "FN",
    "IMPORT",
    "INSERT",
    "REBUILD",
    "RELATE",
    "REMOVE",
    "UPDATE",
    "UPSERT",
}

_TOKEN_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SURREAL_QUERY_ID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _query_tokens(query: str) -> list[str]:
    return [match.group(0).upper() for match in _TOKEN_PATTERN.finditer(query)]


def _first_token(query: str) -> str:
    tokens = _query_tokens(query)
    return tokens[0] if tokens else ""


def _is_connection_closed_error(exc: BaseException) -> bool:
    class_names = {type(exc).__name__, type(exc).__qualname__}
    module = type(exc).__module__
    message = str(exc).lower()
    return "ConnectionClosed" in "".join(class_names) or (
        "websockets" in module and ("closed" in message or "keepalive ping timeout" in message)
    )


def _is_transient_connection_error(exc: BaseException) -> bool:
    if isinstance(exc, SurrealConnectTimeout):
        return True
    if isinstance(exc, SurrealConnectError | SurrealTransportError):
        return exc.transient
    if _is_connection_closed_error(exc):
        return True
    if isinstance(exc, KeyError) and exc.args:
        missing_key = str(exc.args[0])
        if _SURREAL_QUERY_ID_PATTERN.fullmatch(missing_key):
            return True
    return isinstance(exc, TimeoutError) and "opening handshake" in str(exc).lower()


def _can_retry_query(query: str) -> bool:
    statements = [statement.strip() for statement in query.split(";") if statement.strip()]
    if not statements:
        return False
    tokens = _query_tokens(query)
    return all(
        _first_token(statement) in _READ_ONLY_QUERY_TOKENS for statement in statements
    ) and not (set(tokens) & _WRITE_QUERY_TOKENS)


def _can_retry_raw_query(query: str) -> bool:
    statements = [statement.strip() for statement in query.split(";") if statement.strip()]
    if not statements:
        return False
    tokens = _query_tokens(query)
    return all(
        _first_token(statement) in _RAW_READ_ONLY_QUERY_TOKENS for statement in statements
    ) and not (set(tokens) & _WRITE_QUERY_TOKENS)


class SurrealConnectTimeout(TimeoutError):
    """Raised when opening a SurrealDB socket exceeds the connect budget.

    Distinct from a slow query: the statement never reached the server. It
    subclasses ``TimeoutError`` so existing transient-error handling keeps
    working, while callers that care about the difference can catch this.
    """

    def __init__(self, *, url: str, attempt: int, timeout_seconds: float) -> None:
        super().__init__(
            f"SurrealDB connect timed out after {timeout_seconds:.3f}s "
            f"(attempt {attempt}, scheme {_log_scheme(url)})"
        )
        self.attempt = attempt
        self.timeout_seconds = timeout_seconds
        self.url_scheme = _log_scheme(url)


def _log_scheme(url: str) -> str:
    """The URL's scheme for an error or log field; never any other part of it."""
    return surreal_url_scheme(url) or "unknown"


class SurrealConnectError(ConnectionError):
    """Opening a SurrealDB connection failed, described without the URL.

    The SDK's own connect errors can quote the whole URL, credentials
    included ("ws:///user:pass@host/rpc isn't a valid URI"). This carries the
    redacted endpoint, the SDK error's class, and its message only when that
    message quotes no secret part of the URL. Raise it outside the handler
    that caught the SDK error, so the original is neither the cause nor the
    context and no traceback can print it.
    """

    def __init__(self, *, url: str, cause: BaseException) -> None:
        detail = safe_error_detail(cause, url)
        suffix = f": {detail}" if detail else ""
        super().__init__(
            f"SurrealDB connect to {redact_surreal_url(url)} failed "
            f"({type(cause).__name__}){suffix}"
        )
        self.url_scheme = _log_scheme(url)
        self.cause_type = type(cause).__name__
        # Retry decisions keep following the original error.
        self.transient = _is_transient_connection_error(cause)


class SurrealTransportError(RuntimeError):
    """A SurrealDB request failed with an error that quoted the configured URL.

    Raised by the DedicatedSurrealClient boundary in place of such an error:
    the SDK's HTTP and WebSocket stacks can echo the URL they requested, path
    and query included, often normalized. Only the redacted endpoint and the
    original class survive; the original is not chained.
    """

    def __init__(self, *, url: str, cause: BaseException) -> None:
        super().__init__(
            f"SurrealDB request to {redact_surreal_url(url)} failed ({type(cause).__name__})"
        )
        self.url_scheme = _log_scheme(url)
        self.cause_type = type(cause).__name__
        # Retry decisions keep following the original error.
        self.transient = _is_transient_connection_error(cause)


def detach_url_secrets(error: Exception, url: str) -> Exception | None:
    """The exception to raise in place of ``error``, or None when it is clean.

    The one boundary check for everything leaving a DedicatedSurrealClient. An
    error is clean when neither it nor anything chained to it quotes a secret
    piece of the URL, in any spelling; it then keeps its type, message, and
    chain. Otherwise the replacement names only the redacted endpoint and the
    original class, keeps the retry classification, and must be raised outside
    the handler so the original is neither its cause nor its context.
    """
    unchained = error.__cause__ is None and error.__context__ is None
    if unchained and isinstance(
        error, SurrealConnectError | SurrealConnectTimeout | SurrealTransportError
    ):
        # Built from the redacted endpoint and raised without a chain.
        return None
    if not error_mentions_url_secret(error, url):
        return None
    if isinstance(error, SurrealConnectTimeout):
        return SurrealConnectTimeout(
            url=url, attempt=error.attempt, timeout_seconds=error.timeout_seconds
        )
    return SurrealTransportError(url=url, cause=error)


def _withhold_in_statement(statement: object, url: str, withheld: str) -> object:
    """An ERR statement with every URL-quoting text field withheld, else itself."""
    if not isinstance(statement, dict) or statement.get("status") != "ERR":
        return statement
    leaky = {
        key
        for key, value in statement.items()
        if key not in {"status", "time"}
        and isinstance(value, str)
        and text_mentions_url_secret(value, url)
    }
    if not leaky:
        return statement
    return {key: withheld if key in leaky else value for key, value in statement.items()}


def withhold_url_secrets_in_envelope(response: object, url: str) -> object:
    """A raw query response with URL-quoting error text withheld.

    Raw callers receive statement envelopes, and several raise an ERR
    statement's text themselves, outside the client. A statement or RPC error
    whose text quotes a secret piece of the URL has that text replaced; its
    status, details, and position stay, so callers still branch the same way.
    A response with nothing to withhold is returned as the same object.
    """
    if not isinstance(response, dict):
        return response
    withheld = f"SurrealDB error withheld: it quoted the configured URL ({redact_surreal_url(url)})"
    replaced: dict[str, object] = {}
    error = response.get("error")
    if (
        isinstance(error, dict)
        and isinstance(error.get("message"), str)
        and text_mentions_url_secret(error["message"], url)
    ):
        replaced["error"] = {**error, "message": withheld}
    statements = response.get("result")
    if isinstance(statements, list):
        scrubbed = [_withhold_in_statement(statement, url, withheld) for statement in statements]
        if any(new is not old for new, old in zip(scrubbed, statements, strict=True)):
            replaced["result"] = scrubbed
    return {**response, **replaced} if replaced else response


class SurrealQueryError(RuntimeError):
    """Raised when SurrealDB returns an error envelope instead of result rows."""

    def __init__(self, query: str, message: str) -> None:
        snippet = (query[:120] + "…") if len(query) > 120 else query
        super().__init__(f"SurrealDB query failed: {message} (query: {snippet!r})")
        self.query = query
        self.surreal_message = message


__all__ = [
    "SurrealConnectTimeout",
    "SurrealQueryError",
    "_can_retry_query",
    "_can_retry_raw_query",
    "_is_connection_closed_error",
    "_is_transient_connection_error",
]
