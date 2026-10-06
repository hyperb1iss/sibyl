from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterator
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import BaseModel

from sibyl.api.idempotency import (
    replay_idempotent_response,
    save_idempotent_response,
    serialize_idempotent_request,
)
from sibyl.coordination._local.locks import LocalLockManager
from sibyl.persistence.content_common import ApiIdempotencyRecord


class _MutationResponse(BaseModel):
    value: str


class _IdempotencyStore:
    """In-memory stand-in for the content table and its unique scope index.

    reserve() claims a scope with one write or returns the record holding it,
    complete() writes the response onto a reservation, get() reads it back:
    the three seams the runtime exposes. Each counts its calls so a test can
    pin the round trips a keyed request costs.
    """

    def __init__(self) -> None:
        self.records: dict[tuple[str, str, str, str, str], ApiIdempotencyRecord] = {}
        self.reserve_calls = 0
        self.complete_calls = 0
        self.get_calls = 0
        self.fail_next_complete = False

    @staticmethod
    def _scope(record: ApiIdempotencyRecord) -> tuple[str, str, str, str, str]:
        return (
            str(record.organization_id),
            record.principal_id,
            record.method,
            record.path,
            record.idempotency_key,
        )

    async def reserve(
        self, _session: object, *, record: ApiIdempotencyRecord
    ) -> tuple[ApiIdempotencyRecord, bool]:
        self.reserve_calls += 1
        existing = self.records.get(self._scope(record))
        if existing is not None:
            return existing, False
        self.records[self._scope(record)] = record
        return record, True

    async def complete(
        self, _session: object, *, record: ApiIdempotencyRecord
    ) -> ApiIdempotencyRecord | None:
        self.complete_calls += 1
        if self.fail_next_complete:
            self.fail_next_complete = False
            raise RuntimeError("receipt store unavailable")
        stored = self.records.get(self._scope(record))
        if stored is None or stored.id != record.id:
            return None
        completed = replace(
            stored,
            response_status_code=record.response_status_code,
            response_body=record.response_body,
        )
        self.records[self._scope(record)] = completed
        return completed

    async def get(self, _session: object, **scope: object) -> ApiIdempotencyRecord | None:
        self.get_calls += 1
        key = (
            str(scope["organization_id"]),
            str(scope["principal_id"]),
            str(scope["method"]),
            str(scope["path"]),
            str(scope["idempotency_key"]),
        )
        return self.records.get(key)

    @contextlib.contextmanager
    def patched(self) -> Iterator[None]:
        with (
            patch(
                "sibyl.api.idempotency.content_runtime.reserve_api_idempotency_record",
                side_effect=self.reserve,
            ),
            patch(
                "sibyl.api.idempotency.content_runtime.complete_api_idempotency_record",
                side_effect=self.complete,
            ),
            patch(
                "sibyl.api.idempotency.content_runtime.get_api_idempotency_record",
                side_effect=self.get,
            ),
        ):
            yield


def _request(key: str) -> SimpleNamespace:
    return SimpleNamespace(headers={"Idempotency-Key": key})


@pytest.mark.asyncio
async def test_concurrent_idempotent_requests_execute_mutation_once() -> None:
    lock_manager = LocalLockManager()
    records: dict[str, dict[str, object]] = {}
    mutation_count = 0

    @serialize_idempotent_request
    async def mutate(
        *, http_request: SimpleNamespace, org: object, ctx: object
    ) -> dict[str, object]:
        nonlocal mutation_count
        key = http_request.headers["Idempotency-Key"]
        if key in records:
            return {**records[key], "replayed": True}
        mutation_count += 1
        await asyncio.sleep(0.01)
        response = {"operation_id": key, "replayed": False}
        records[key] = response
        return response

    def request() -> SimpleNamespace:
        return SimpleNamespace(
            headers={"Idempotency-Key": "remember-1"},
            method="POST",
            url=SimpleNamespace(path="/memory/raw"),
        )

    org = SimpleNamespace(id=uuid4())
    ctx = SimpleNamespace(user_id=str(uuid4()))
    with patch("sibyl.api.idempotency.get_locks", return_value=lock_manager):
        first, second = await asyncio.gather(
            mutate(http_request=request(), org=org, ctx=ctx),
            mutate(http_request=request(), org=org, ctx=ctx),
        )

    assert mutation_count == 1
    assert {first["replayed"], second["replayed"]} == {False, True}


@pytest.mark.asyncio
async def test_a_keyed_request_costs_one_reservation_and_one_completion_write() -> None:
    """Reserve and complete are one write each; nothing is read on the happy path.

    The reservation used to be a lookup, an UPSERT and a CREATE fallback, and
    completion another UPSERT carrying the body, so every keyed CLI mutation
    paid three to four content writes plus a read before its own work.
    """
    store = _IdempotencyStore()
    organization_id = uuid4()
    request = _request("remember-1")
    kwargs = {
        "organization_id": organization_id,
        "principal_id": "user-1",
        "method": "POST",
        "path": "/memory/raw",
        "payload": {"body": {"title": "One write each"}},
        "content_session": None,
    }

    with store.patched():
        assert (
            await replay_idempotent_response(request, response_model=_MutationResponse, **kwargs)
            is None
        )
        await save_idempotent_response(
            request,
            response=_MutationResponse(value="applied"),
            status_code=200,
            **kwargs,
        )
        replayed = await replay_idempotent_response(
            _request("remember-1"), response_model=_MutationResponse, **kwargs
        )

    assert store.reserve_calls == 2, "one claim, one replay lookup through the same write"
    assert store.complete_calls == 1
    assert store.get_calls == 0
    assert isinstance(replayed, _MutationResponse)
    assert replayed.value == "applied"
    stored = next(iter(store.records.values()))
    assert stored.response_status_code == 200
    assert stored.response_body == {"value": "applied"}


@pytest.mark.asyncio
async def test_interrupted_reservation_is_taken_over_and_completed_on_retry() -> None:
    """A reservation orphaned at 102 recovers on retry instead of bricking the key.

    Every caller of replay_idempotent_response executes under the serialize
    lock, so a pending record observed there proves the original executor is
    gone. The retry adopts the claim, re-executes, and completes the same
    record id; a later duplicate then replays the stored response.
    """
    store = _IdempotencyStore()
    organization_id = uuid4()
    payload = {"body": {"title": "Durable reservation"}}

    def replay_kwargs() -> dict[str, object]:
        return {
            "organization_id": organization_id,
            "principal_id": "user-1",
            "method": "POST",
            "path": "/memory/raw",
            "payload": payload,
            "response_model": _MutationResponse,
            "content_session": None,
        }

    request = _request("remember-1")
    with store.patched():
        replayed = await replay_idempotent_response(request, **replay_kwargs())
        assert replayed is None

        store.fail_next_complete = True
        with pytest.raises(HTTPException) as completion_error:
            await save_idempotent_response(
                request,
                organization_id=organization_id,
                principal_id="user-1",
                method="POST",
                path="/memory/raw",
                payload=payload,
                response=_MutationResponse(value="applied"),
                status_code=200,
                content_session=None,
            )
        assert completion_error.value.status_code == 503

        retry_request = _request("remember-1")
        retried = await replay_idempotent_response(retry_request, **replay_kwargs())
        assert retried is None, "retry must take the orphaned reservation over"

        pending_id = next(iter(store.records.values())).id
        await save_idempotent_response(
            retry_request,
            organization_id=organization_id,
            principal_id="user-1",
            method="POST",
            path="/memory/raw",
            payload=payload,
            response=_MutationResponse(value="applied"),
            status_code=200,
            content_session=None,
        )
        stored = next(iter(store.records.values()))
        assert stored.id == pending_id
        assert stored.response_status_code == 200

        final = await replay_idempotent_response(_request("remember-1"), **replay_kwargs())

    assert isinstance(final, _MutationResponse)
    assert final.value == "applied"


@pytest.mark.asyncio
async def test_pending_takeover_rejects_a_different_payload() -> None:
    """Takeover is scoped to the identical request: a new payload still 409s."""
    store = _IdempotencyStore()
    organization_id = uuid4()

    with store.patched():
        reserved = await replay_idempotent_response(
            _request("remember-1"),
            organization_id=organization_id,
            principal_id="user-1",
            method="POST",
            path="/memory/raw",
            payload={"body": {"title": "original"}},
            response_model=_MutationResponse,
            content_session=None,
        )
        assert reserved is None

        with pytest.raises(HTTPException) as mismatch:
            await replay_idempotent_response(
                _request("remember-1"),
                organization_id=organization_id,
                principal_id="user-1",
                method="POST",
                path="/memory/raw",
                payload={"body": {"title": "tampered"}},
                response_model=_MutationResponse,
                content_session=None,
            )

    assert mismatch.value.status_code == 409
    assert "different request" in str(mismatch.value.detail)


@pytest.mark.asyncio
async def test_busy_idempotency_lock_exposes_retryable_error_code() -> None:
    from contextlib import asynccontextmanager

    from sibyl.locks import LockAcquisitionError

    @asynccontextmanager
    async def busy_lock(**_kwargs):
        raise LockAcquisitionError("request", "org")
        yield

    @serialize_idempotent_request
    async def mutate(*, http_request, org, ctx):
        pytest.fail("Busy lock must not execute the handler")

    request = SimpleNamespace(
        headers={"Idempotency-Key": "same-request"},
        method="POST",
        url=SimpleNamespace(path="/memory/raw"),
    )
    with (
        patch("sibyl.api.idempotency.idempotency_lock", busy_lock),
        pytest.raises(HTTPException) as caught,
    ):
        await mutate(http_request=request, org=SimpleNamespace(id=uuid4()), ctx=None)
    assert caught.value.status_code == 409
    assert caught.value.detail["error"] == "idempotency_in_progress"


@pytest.mark.asyncio
async def test_migration_pending_receipt_requires_reconciliation_but_completed_replays() -> None:
    store = _IdempotencyStore()
    organization_id = uuid4()

    payload = {"body": {"title": "migration"}, "query": {"replay_interrupted": False}}
    kwargs = {
        "organization_id": organization_id,
        "principal_id": "user-1",
        "method": "POST",
        "path": "/entities",
        "payload": payload,
        "response_model": _MutationResponse,
        "content_session": None,
        "replay_interrupted": False,
    }

    request = _request("migration-1")
    with store.patched():
        assert await replay_idempotent_response(request, **kwargs) is None
        pending = next(iter(store.records.values()))
        retry = _request("migration-1")
        with pytest.raises(HTTPException) as uncertain:
            await replay_idempotent_response(retry, **kwargs)
        assert uncertain.value.status_code == 409
        assert uncertain.value.detail["error"] == "idempotency_reconciliation_required"
        assert next(iter(store.records.values())) is pending
        assert not hasattr(retry, "_sibyl_idempotency_claim")

        with pytest.raises(HTTPException, match="different request"):
            await replay_idempotent_response(
                retry,
                **{
                    **kwargs,
                    "payload": {"body": {"title": "migration"}},
                    "replay_interrupted": True,
                },
            )
        await save_idempotent_response(
            request,
            organization_id=organization_id,
            principal_id="user-1",
            method="POST",
            path="/entities",
            payload=payload,
            response=_MutationResponse(value="applied"),
            status_code=201,
            content_session=None,
        )
        replay = await replay_idempotent_response(retry, **kwargs)
        assert replay.value == "applied"
