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
async def test_pending_reservation_fences_retry_after_completion_failure() -> None:
    organization_id = uuid4()
    stored_record: object | None = None

    async def get_record(*_args: object, **_kwargs: object) -> object:
        return stored_record

    async def save_record(*_args: object, **kwargs: object) -> object:
        nonlocal stored_record
        record = kwargs["record"]
        if stored_record is not None:
            raise RuntimeError("receipt store unavailable")
        stored_record = record
        return record

    request = SimpleNamespace(headers={"Idempotency-Key": "remember-1"})
    payload = {"body": {"title": "Durable reservation"}}
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
        replayed = await replay_idempotent_response(
            request,
            organization_id=organization_id,
            principal_id="user-1",
            method="POST",
            path="/memory/raw",
            payload=payload,
            response_model=_MutationResponse,
            content_session=None,
        )
        assert replayed is None

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

        with pytest.raises(HTTPException) as retry_error:
            await replay_idempotent_response(
                SimpleNamespace(headers={"Idempotency-Key": "remember-1"}),
                organization_id=organization_id,
                principal_id="user-1",
                method="POST",
                path="/memory/raw",
                payload=payload,
                response_model=_MutationResponse,
                content_session=None,
            )

    assert retry_error.value.status_code == 409
    assert "interrupted" in str(retry_error.value.detail)
