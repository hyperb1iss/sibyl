"""SurrealDB fulltext query helpers."""

from __future__ import annotations

DEFAULT_FULLTEXT_QUERY_MAX_LENGTH = 128


def build_fulltext_query(
    query: str,
    max_query_length: int = DEFAULT_FULLTEXT_QUERY_MAX_LENGTH,
) -> str:
    sanitized = "".join(c for c in query if c.isprintable() and c not in ('"', "'")).strip()
    if not sanitized:
        return ""
    if len(sanitized) > max_query_length:
        sanitized = sanitized[:max_query_length]
    return sanitized
