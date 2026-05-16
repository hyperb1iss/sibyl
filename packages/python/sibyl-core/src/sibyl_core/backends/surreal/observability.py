"""Query telemetry for SurrealDB backends."""

from __future__ import annotations

import hashlib
import re
import time

import structlog

from sibyl_core.observability import telemetry_registry
from sibyl_core.utils.log_safety import fingerprint_text

log = structlog.get_logger()

_TOKEN_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_QUERY_STATEMENT_TOKENS = {
    "CREATE",
    "DELETE",
    "INFO",
    "INSERT",
    "RELATE",
    "REMOVE",
    "RETURN",
    "SELECT",
    "UPDATE",
    "UPSERT",
}
_TABLE_CONTEXT_TOKENS = {"CREATE", "DELETE", "FROM", "INTO", "RELATE", "UPDATE"}
_NON_TABLE_TOKENS = {"CONTENT", "MERGE", "ONLY", "OVERWRITE", "SELECT", "SET", "VALUE"}
_QUIET_SUCCESS_STATEMENTS = {"alter", "define"}


def query_start() -> float:
    return time.perf_counter()


def elapsed_ms(started_at: float) -> float:
    return round((time.perf_counter() - started_at) * 1000, 2)


def _slow_query_threshold_ms() -> float:
    from sibyl_core.config import core_config

    return core_config.surreal_slow_query_ms


def _query_tokens(query: str) -> list[str]:
    return [match.group(0) for match in _TOKEN_PATTERN.finditer(query)]


def _statement_count(query: str) -> int:
    return len([statement for statement in query.split(";") if statement.strip()])


def _primary_statement(query: str) -> str:
    tokens = _query_tokens(query)
    if not tokens:
        return "unknown"
    if tokens[0].upper() == "LET":
        for token in tokens[1:]:
            upper = token.upper()
            if upper in _QUERY_STATEMENT_TOKENS:
                return f"let_{upper.lower()}"
        return "let"
    return tokens[0].lower()


def _query_tables(query: str) -> list[str]:
    tokens = _query_tokens(query)
    tables: set[str] = set()
    for index, token in enumerate(tokens[:-1]):
        if token.upper() not in _TABLE_CONTEXT_TOKENS:
            continue
        table = tokens[index + 1]
        if table.upper() in _NON_TABLE_TOKENS:
            continue
        tables.add(table)
    return sorted(tables)[:8]


def _query_hash(query: str) -> str:
    normalized = " ".join(query.split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]


def _error_message(error: BaseException) -> str:
    surreal_message = getattr(error, "surreal_message", None)
    if isinstance(surreal_message, str):
        return surreal_message
    return str(error)


def _error_category(message: str) -> str:
    lowered = message.lower()
    if "parse error" in lowered:
        return "parse_error"
    if "no suitable index" in lowered:
        return "missing_index"
    if "already contains" in lowered or "unique" in lowered:
        return "constraint"
    if "timed out" in lowered or "timeout" in lowered:
        return "timeout"
    if "connection" in lowered or "websocket" in lowered:
        return "connection"
    return "query_error"


def _error_log_fields(error: BaseException) -> dict[str, int | str]:
    message = _error_message(error)
    return {
        "error_type": type(error).__name__,
        "error_category": _error_category(message),
        "error_hash": fingerprint_text(message),
        "error_length": len(message),
    }


def log_query(
    query: str,
    *,
    client_kind: str,
    namespace: str,
    database: str,
    raw: bool,
    elapsed: float,
    retry_count: int = 0,
    error: BaseException | None = None,
) -> None:
    statement = _primary_statement(query)
    query_hash = _query_hash(query)
    slow = error is None and elapsed >= _slow_query_threshold_ms()
    status = "error" if error is not None else "ok"
    telemetry_registry().record_surreal_query(
        client=client_kind,
        database=database,
        statement=statement,
        query_hash=query_hash,
        elapsed_ms=elapsed,
        retry_count=retry_count,
        status=status,
        slow=slow,
    )
    fields: dict[str, object] = {
        "client": client_kind,
        "namespace": namespace,
        "database": database,
        "raw": raw,
        "elapsed_ms": elapsed,
        "retry_count": retry_count,
        "statement": statement,
        "statement_count": _statement_count(query),
        "tables": _query_tables(query),
        "query_hash": query_hash,
    }
    if error is not None:
        log.warning("surreal_query_failed", **_error_log_fields(error), **fields)
        return
    if elapsed >= _slow_query_threshold_ms():
        log.warning("surreal_query_slow", **fields)
        return
    if statement in _QUIET_SUCCESS_STATEMENTS:
        return
    log.debug("surreal_query_complete", **fields)


__all__ = ["elapsed_ms", "log_query", "query_start"]
