"""Shared SurrealDB connection retry helpers."""

from __future__ import annotations

import re

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
    return (
        "ConnectionClosed" in "".join(class_names)
        or (
            "websockets" in module
            and ("closed" in message or "keepalive ping timeout" in message)
        )
    )


def _is_transient_connection_error(exc: BaseException) -> bool:
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
    return all(_first_token(statement) in _READ_ONLY_QUERY_TOKENS for statement in statements) and not (
        set(tokens) & _WRITE_QUERY_TOKENS
    )


def _can_retry_raw_query(query: str) -> bool:
    statements = [statement.strip() for statement in query.split(";") if statement.strip()]
    if not statements:
        return False
    tokens = _query_tokens(query)
    return all(_first_token(statement) in _RAW_READ_ONLY_QUERY_TOKENS for statement in statements) and not (
        set(tokens) & _WRITE_QUERY_TOKENS
    )


__all__ = [
    "_can_retry_query",
    "_can_retry_raw_query",
    "_is_connection_closed_error",
    "_is_transient_connection_error",
]
