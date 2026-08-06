from __future__ import annotations

import asyncio
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


class _MutationResponse(BaseModel):
    value: str


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
async def test_interrupted_reservation_is_taken_over_and_completed_on_retry() -> None:
    """A reservation orphaned at 102 recovers on retry instead of bricking the key.

    Every caller of replay_idempotent_response executes under the serialize
    lock, so a pending record observed there proves the original executor is
    gone. The retry adopts the claim, re-executes, and completes the same
    record id; a later duplicate then replays the stored response.
    """
    organization_id = uuid4()
    stored_record: object | None = None
    fail_next_save = False

    async def get_record(*_args: object, **_kwargs: object) -> object:
        return stored_record

    async def save_record(*_args: object, **kwargs: object) -> object:
        nonlocal stored_record, fail_next_save
        if fail_next_save:
            fail_next_save = False
            raise RuntimeError("receipt store unavailable")
        stored_record = kwargs["record"]
        return stored_record

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

    request = SimpleNamespace(headers={"Idempotency-Key": "remember-1"})
    with (
        patch(
            "sibyl.api.idempotency.content_runtime.get_api_idempotency_record",
            side_effect=get_record,
        ),
        patch(
            "sibyl.api.idempotency.content_runtime.save_api_idempotency_record",
            side_effect=save_record,
        ),
    ):
        replayed = await replay_idempotent_response(request, **replay_kwargs())
        assert replayed is None

        fail_next_save = True
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

        retry_request = SimpleNamespace(headers={"Idempotency-Key": "remember-1"})
        retried = await replay_idempotent_response(retry_request, **replay_kwargs())
        assert retried is None, "retry must take the orphaned reservation over"

        pending_id = getattr(stored_record, "id", None)
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
        assert getattr(stored_record, "id", None) == pending_id
        assert getattr(stored_record, "response_status_code", None) == 200

        final = await replay_idempotent_response(
            SimpleNamespace(headers={"Idempotency-Key": "remember-1"}),
            **replay_kwargs(),
        )

    assert isinstance(final, _MutationResponse)
    assert final.value == "applied"


@pytest.mark.asyncio
async def test_pending_takeover_rejects_a_different_payload() -> None:
    """Takeover is scoped to the identical request: a new payload still 409s."""
    organization_id = uuid4()
    stored_record: object | None = None

    async def get_record(*_args: object, **_kwargs: object) -> object:
        return stored_record

    async def save_record(*_args: object, **kwargs: object) -> object:
        nonlocal stored_record
        stored_record = kwargs["record"]
        return stored_record

    with (
        patch(
            "sibyl.api.idempotency.content_runtime.get_api_idempotency_record",
            side_effect=get_record,
        ),
        patch(
            "sibyl.api.idempotency.content_runtime.save_api_idempotency_record",
            side_effect=save_record,
        ),
    ):
        reserved = await replay_idempotent_response(
            SimpleNamespace(headers={"Idempotency-Key": "remember-1"}),
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
                SimpleNamespace(headers={"Idempotency-Key": "remember-1"}),
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
