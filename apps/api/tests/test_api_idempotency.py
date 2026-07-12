from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import BaseModel

from sibyl.api.idempotency import (
    complete_idempotency_record,
    idempotency_request_hash,
    replay_idempotent_response,
    save_idempotent_response,
    serialize_idempotent_request,
    try_reclaim_idempotency_record,
)
from sibyl.coordination._local.locks import LocalLockManager
from sibyl.persistence.content_common import ApiIdempotencyRecord


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
async def test_held_idempotency_lock_has_structured_conflict_code() -> None:
    lock_manager = AsyncMock()
    lock_manager.acquire.return_value = None

    @serialize_idempotent_request
    async def mutate(
        *, http_request: SimpleNamespace, org: object, ctx: object
    ) -> dict[str, object]:
        return {"applied": True}

    request = SimpleNamespace(
        headers={"Idempotency-Key": "remember-1"},
        method="POST",
        url=SimpleNamespace(path="/memory/raw"),
    )
    with (
        patch("sibyl.api.idempotency.get_locks", return_value=lock_manager),
        pytest.raises(HTTPException) as error,
    ):
        await mutate(
            http_request=request,
            org=SimpleNamespace(id=uuid4()),
            ctx=SimpleNamespace(user_id="user-1"),
        )

    assert error.value.detail["code"] == "idempotency_lock_held"


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
        patch(
            "sibyl.api.idempotency.content_runtime.compare_and_set_api_idempotency_record",
            side_effect=RuntimeError("receipt store unavailable"),
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
    assert retry_error.value.detail["code"] == "idempotency_interrupted_pending"
    assert "interrupted" in retry_error.value.detail["message"]


@pytest.mark.asyncio
async def test_completion_compare_and_set_advances_claim_revision() -> None:
    claim = ApiIdempotencyRecord(
        organization_id=uuid4(),
        principal_id="user-1",
        idempotency_key="remember-1",
        method="POST",
        path="/memory/raw",
        request_hash="hash-1",
        response_status_code=102,
        response_body={},
        claim_token="claim-1",
        claim_revision=3,
    )

    async def compare_and_set(*_args: object, **kwargs: object) -> ApiIdempotencyRecord:
        record = kwargs["record"]
        assert isinstance(record, ApiIdempotencyRecord)
        return record

    with patch(
        "sibyl.api.idempotency.content_runtime.compare_and_set_api_idempotency_record",
        side_effect=compare_and_set,
    ) as compare:
        completed = await complete_idempotency_record(
            claim,
            response_status_code=201,
            response_body={"value": "applied"},
            content_session=None,
        )

    assert completed.claim_revision == 4
    assert completed.response_status_code == 201
    assert completed.response_body == {"value": "applied"}
    assert compare.await_args.kwargs["expected_claim_token"] == "claim-1"
    assert compare.await_args.kwargs["expected_claim_revision"] == 3


@pytest.mark.asyncio
async def test_reclaimed_claim_fences_old_receipt_completion() -> None:
    claim = ApiIdempotencyRecord(
        organization_id=uuid4(),
        principal_id="user-1",
        idempotency_key="remember-1",
        method="POST",
        path="/memory/raw",
        request_hash="hash-1",
        response_status_code=102,
        response_body={},
        claim_token="claim-1",
        claim_revision=2,
    )
    reclaimed = replace(
        claim,
        claim_token="claim-2",
        claim_revision=3,
        updated_at=datetime.now(UTC).replace(tzinfo=None),
    )

    with (
        patch(
            "sibyl.api.idempotency.content_runtime.compare_and_set_api_idempotency_record",
            return_value=None,
        ),
        patch(
            "sibyl.api.idempotency.content_runtime.get_api_idempotency_record",
            return_value=reclaimed,
        ),
        pytest.raises(HTTPException) as error,
    ):
        await complete_idempotency_record(
            claim,
            response_status_code=201,
            response_body={"value": "applied"},
            content_session=None,
        )

    assert error.value.status_code == 503
    assert error.value.detail["code"] == "idempotency_receipt_pending"


@pytest.mark.asyncio
async def test_stale_pending_claim_reclaims_with_new_fencing_token() -> None:
    updated_at = datetime(2026, 7, 12, 12, 0, tzinfo=UTC).replace(tzinfo=None)
    claim = ApiIdempotencyRecord(
        organization_id=uuid4(),
        principal_id="user-1",
        idempotency_key="remember-1",
        method="POST",
        path="/memory/raw",
        request_hash="hash-1",
        response_status_code=102,
        response_body={},
        claim_token="claim-1",
        claim_revision=2,
        updated_at=updated_at,
    )

    async def compare_and_set(*_args: object, **kwargs: object) -> ApiIdempotencyRecord:
        record = kwargs["record"]
        assert isinstance(record, ApiIdempotencyRecord)
        return record

    with patch(
        "sibyl.api.idempotency.content_runtime.compare_and_set_api_idempotency_record",
        side_effect=compare_and_set,
    ) as compare:
        reclaimed = await try_reclaim_idempotency_record(
            claim,
            stale_before=updated_at + timedelta(seconds=1),
            content_session=None,
        )

    assert reclaimed is not None
    assert reclaimed.claim_token != claim.claim_token
    assert reclaimed.claim_revision == 3
    assert compare.await_args.kwargs["stale_before"] == updated_at + timedelta(seconds=1)


@pytest.mark.asyncio
async def test_fresh_pending_claim_cannot_reclaim() -> None:
    updated_at = datetime(2026, 7, 12, 12, 0, tzinfo=UTC).replace(tzinfo=None)
    claim = ApiIdempotencyRecord(
        organization_id=uuid4(),
        principal_id="user-1",
        idempotency_key="remember-1",
        method="POST",
        path="/memory/raw",
        request_hash="hash-1",
        response_status_code=102,
        response_body={},
        updated_at=updated_at,
    )

    compare = AsyncMock()
    with patch(
        "sibyl.api.idempotency.content_runtime.compare_and_set_api_idempotency_record",
        compare,
    ):
        reclaimed = await try_reclaim_idempotency_record(
            claim,
            stale_before=updated_at - timedelta(seconds=1),
            content_session=None,
        )

    assert reclaimed is None
    compare.assert_not_awaited()


@pytest.mark.asyncio
async def test_payload_mismatch_has_structured_conflict_code() -> None:
    record = ApiIdempotencyRecord(
        organization_id=uuid4(),
        principal_id="user-1",
        idempotency_key="remember-1",
        method="POST",
        path="/memory/raw",
        request_hash=idempotency_request_hash({"value": "first"}),
        response_status_code=201,
        response_body={"value": "applied"},
    )
    request = SimpleNamespace(headers={"Idempotency-Key": "remember-1"})

    with (
        patch(
            "sibyl.api.idempotency.content_runtime.get_api_idempotency_record",
            return_value=record,
        ),
        pytest.raises(HTTPException) as error,
    ):
        await replay_idempotent_response(
            request,
            organization_id=record.organization_id,
            principal_id=record.principal_id,
            method=record.method,
            path=record.path,
            payload={"value": "second"},
            response_model=_MutationResponse,
            content_session=None,
        )

    assert error.value.detail["code"] == "idempotency_payload_mismatch"
