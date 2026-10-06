"""Raw capture writes are one atomic unit, so a lost commit race is replayed."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from sibyl_core.backends.surreal import dedicated_client as dedicated_client_module
from sibyl_core.backends.surreal.content_client import SurrealContentClient
from sibyl_core.backends.surreal.content_schema import bootstrap_content_schema
from sibyl_core.backends.surreal.dedicated_client import _can_replay_query
from sibyl_core.backends.surreal.schema_derivations import STORE_RAW_DERIVATIONS
from sibyl_core.services.content_models import RawMemory, raw_memory_record
from sibyl_core.services.content_raw_persistence import (
    _RAW_MEMORY_BULK_RETURN,
    _RAW_MEMORY_BULK_UPSERT_QUERY,
    replace_raw_memory_records_bulk,
)

_CONFLICT = "Transaction conflict: raw_captures index. This transaction can be retried"


def _raw_record(
    uuid: str, *, organization_id: str = "org-replay", title: str | None = None
) -> dict[str, Any]:
    captured_at = datetime(2026, 10, 6, 12, 0, tzinfo=UTC).replace(tzinfo=None)
    return raw_memory_record(
        RawMemory(
            id=uuid,
            organization_id=organization_id,
            source_id="source-replay",
            principal_id="user-replay",
            title=title or f"Replay {uuid}",
            raw_content=f"raw content for {uuid}",
            captured_at=captured_at,
            created_at=captured_at,
        )
    )


def test_raw_memory_bulk_upsert_is_one_replayable_unit() -> None:
    assert _can_replay_query(_RAW_MEMORY_BULK_UPSERT_QUERY) is True


def test_raw_memory_bulk_upsert_with_derivations_is_one_replayable_unit() -> None:
    with_derivations = _RAW_MEMORY_BULK_UPSERT_QUERY.replace(
        _RAW_MEMORY_BULK_RETURN, STORE_RAW_DERIVATIONS + _RAW_MEMORY_BULK_RETURN
    )
    assert STORE_RAW_DERIVATIONS.strip() in with_derivations
    assert with_derivations.index(STORE_RAW_DERIVATIONS.strip()) < with_derivations.index(
        "COMMIT TRANSACTION;"
    )
    assert _can_replay_query(with_derivations) is True


@pytest.mark.asyncio
async def test_raw_memory_bulk_upsert_returns_saved_rows_from_inside_the_transaction() -> None:
    client = SurrealContentClient(url="memory://")
    try:
        await bootstrap_content_schema(client, reset=True)

        saved = await replace_raw_memory_records_bulk(
            client, [_raw_record("raw-a"), _raw_record("raw-b")]
        )
        assert sorted(str(row["uuid"]) for row in saved) == ["raw-a", "raw-b"]
        assert all(row["revision"] == 1 for row in saved)

        rewritten = await replace_raw_memory_records_bulk(
            client, [_raw_record("raw-a", title="Replay raw-a again")]
        )
        assert rewritten[0]["uuid"] == "raw-a"
        assert rewritten[0]["title"] == "Replay raw-a again"
        assert rewritten[0]["revision"] == 2
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_raw_memory_bulk_upsert_is_replayed_after_a_transaction_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One lost commit race on raw_captures ends in a stored row, not a failure."""
    upsert_attempts: list[str] = []

    class FakeAsyncSurreal:
        def __init__(self, url: str) -> None:
            self.url = url

        async def signin(self, credentials: dict[str, str]) -> None:
            self.credentials = credentials

        async def use(self, namespace: str, database: str) -> None:
            self.namespace = namespace
            self.database = database

        async def query_raw(self, query: str, params: object | None = None) -> dict[str, object]:
            if query.strip() == "RETURN true;":
                return {"result": [{"status": "OK", "result": True, "time": "0"}]}
            upsert_attempts.append(query)
            if len(upsert_attempts) == 1:
                return {"result": [{"status": "ERR", "result": _CONFLICT, "time": "0"}]}
            rows = params["rows"] if isinstance(params, dict) else []
            return {"result": [{"status": "OK", "result": list(rows), "time": "0"}]}

        async def close(self) -> None:
            return None

    monkeypatch.setattr("surrealdb.AsyncSurreal", FakeAsyncSurreal)
    monkeypatch.setattr(
        dedicated_client_module, "_transaction_conflict_retry_delay", lambda _n: 0.0
    )

    client = SurrealContentClient(
        url="ws://localhost:8000/rpc",
        username="root",
        password="root",
        pool_size=1,
    )
    try:
        saved = await replace_raw_memory_records_bulk(client, [_raw_record("raw-conflict")])
    finally:
        await client.close()

    assert [row["uuid"] for row in saved] == ["raw-conflict"]
    assert len(upsert_attempts) == 2, "the conflicted commit must be sent again"
    assert upsert_attempts[0] == upsert_attempts[1] == _RAW_MEMORY_BULK_UPSERT_QUERY
