"""Read-only SurrealDB access for the usage-rerank harness.

The harness runs against a live, shared dev store that other agents and the API
daemon are using at the same time, so every statement it can issue is checked
against a mutation denylist before it leaves the process. A harness that reads
production dogfooding data must not be one typo away from writing to it.
"""

from __future__ import annotations

import re
from typing import Any

import httpx
import paths

# SurrealQL verbs that change state. Checked as whole words so a column named
# "created_at" or a value containing "update" cannot trip the guard.
_MUTATING_KEYWORDS = (
    "ALTER",
    "CREATE",
    "DEFINE",
    "DELETE",
    "INSERT",
    "MERGE",
    "PATCH",
    "RELATE",
    "REMOVE",
    "SET",
    "UPDATE",
    "UPSERT",
)

_MUTATION_PATTERN = re.compile(
    r"\b(?:" + "|".join(_MUTATING_KEYWORDS) + r")\b",
    re.IGNORECASE,
)

_COMMENT_PATTERN = re.compile(r"(?:--|#|//)[^\n]*")
_BLOCK_COMMENT_PATTERN = re.compile(r"/\*.*?\*/", re.DOTALL)
_STRING_PATTERN = re.compile(r"'[^']*'|\"[^\"]*\"")


class MutatingStatementError(RuntimeError):
    """Raised when a statement that could change store state reaches the client."""


class SurrealResponseError(RuntimeError):
    """Raised when the /sql endpoint answers with an unusable envelope."""


def assert_read_only(statement: str) -> None:
    """Reject any statement carrying a state-changing SurrealQL verb.

    String literals and comments are blanked before the check so that a query
    filtering on, say, source_surface = 'source_import' is not misread as DML.
    """
    stripped = _BLOCK_COMMENT_PATTERN.sub(" ", statement)
    stripped = _COMMENT_PATTERN.sub(" ", stripped)
    stripped = _STRING_PATTERN.sub(" ", stripped)
    match = _MUTATION_PATTERN.search(stripped)
    if match is not None:
        raise MutatingStatementError(
            f"refusing to send a mutating statement to the store: found {match.group(0)!r}"
        )


class ReadOnlySurrealStore:
    """Minimal HTTP client that can only read."""

    def __init__(
        self,
        *,
        url: str | None = None,
        username: str | None = None,
        password: str | None = None,
        namespace: str | None = None,
        database: str | None = None,
        timeout_seconds: float = 120.0,
    ) -> None:
        self._url = (url or paths.SURREAL_HTTP_URL).rstrip("/")
        self._auth = (
            username or paths.SURREAL_USERNAME,
            password or paths.SURREAL_PASSWORD,
        )
        self._namespace = namespace or paths.CONTENT_NAMESPACE
        self._database = database or paths.CONTENT_DATABASE
        self._timeout_seconds = timeout_seconds

    @property
    def target(self) -> str:
        return f"{self._url} ns={self._namespace} db={self._database}"

    def query(self, statement: str) -> list[Any]:
        """Run one read-only statement and return its result rows."""
        assert_read_only(statement)
        response = httpx.post(
            f"{self._url}/sql",
            content=statement.encode("utf-8"),
            headers={
                "Accept": "application/json",
                "surreal-ns": self._namespace,
                "surreal-db": self._database,
            },
            auth=self._auth,
            timeout=self._timeout_seconds,
        )
        response.raise_for_status()
        return extract_result(response.json())


def extract_result(payload: Any) -> list[Any]:
    """Pull the rows out of a SurrealDB /sql response envelope.

    The endpoint answers with one envelope per statement. Only the last
    statement's rows are of interest, and a non-OK status is an error the
    harness must surface rather than silently treat as an empty read.
    """
    envelopes = payload if isinstance(payload, list) else []
    if not envelopes:
        raise SurrealResponseError(f"unexpected SurrealDB response shape: {payload!r}")
    last = envelopes[-1]
    status = str(last.get("status") or "") if isinstance(last, dict) else ""
    if not status:
        raise SurrealResponseError(f"unexpected SurrealDB statement envelope: {last!r}")
    if status != "OK":
        raise SurrealResponseError(f"SurrealDB statement failed ({status}): {last.get('result')!r}")
    result = last.get("result")
    if result is None:
        return []
    if isinstance(result, list):
        return result
    return [result]


__all__ = [
    "MutatingStatementError",
    "ReadOnlySurrealStore",
    "SurrealResponseError",
    "assert_read_only",
    "extract_result",
]
