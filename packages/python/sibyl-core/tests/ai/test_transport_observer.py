"""External accounting observes dispatch before requests and preserves uncertainty."""

import asyncio
from types import SimpleNamespace

import pytest

from sibyl_core.ai.transport import (
    _record_send,
    collect_transport_attempts,
    observe_transport_attempts,
    reserve_uncovered_transport_attempts,
)


class Journal:
    def __init__(self):
        self.rows = []

    async def before_dispatch(self):
        self.rows.append({"state": "potentially_dispatched", "outcome": None})
        return str(len(self.rows) - 1)

    async def after_dispatch(self, attempt_id, outcome):
        self.rows[int(attempt_id)].update(state="returned", outcome=outcome.model_dump())


async def test_transport_observer_precedes_send_without_exposing_payloads():
    journal = Journal()

    async def send(request):
        assert journal.rows == [{"state": "potentially_dispatched", "outcome": None}]
        return SimpleNamespace(status_code=200, headers={"x-request-id": "safe-request"})

    with observe_transport_attempts(journal), collect_transport_attempts() as attempts:
        await _record_send(send, {"secret": "private-body"}, request_id_header="x-request-id")
    assert journal.rows[0]["outcome"] == attempts[0].model_dump()
    assert "private-body" not in str(journal.rows)
    assert not journal.rows[0]["outcome"]["usage_known"]


async def test_failed_durable_dispatch_prevents_send():
    journal = Journal()

    async def unavailable():
        raise OSError("storage unavailable")

    journal.before_dispatch = unavailable

    async def send(request):
        pytest.fail("request dispatched without durable accounting")

    with observe_transport_attempts(journal), pytest.raises(OSError):
        await _record_send(send, None, request_id_header="x-request-id")


async def test_denied_budget_does_not_create_dispatch():
    journal = Journal()

    async def denied():
        raise ValueError("budget denied")

    async def send(request):
        pytest.fail("request dispatched after budget denial")

    with (
        observe_transport_attempts(journal),
        reserve_uncovered_transport_attempts(0, denied),
        pytest.raises(ValueError, match="budget denied"),
    ):
        await _record_send(send, None, request_id_header="x-request-id")
    assert journal.rows == []


@pytest.mark.parametrize("failure", [TimeoutError("private details"), asyncio.CancelledError()])
async def test_failed_requests_record_only_safe_unknown_outcome(failure):
    journal = Journal()

    async def send(request):
        raise failure

    with observe_transport_attempts(journal), pytest.raises(type(failure)):
        await _record_send(send, None, request_id_header="x-request-id")
    assert journal.rows[0]["outcome"]["exception_type"] == type(failure).__name__
    assert not journal.rows[0]["outcome"]["usage_known"]
    assert "private details" not in str(journal.rows)


async def test_outcome_storage_failure_leaves_dispatch_unknown():
    journal = Journal()

    async def failed_finish(attempt_id, outcome):
        raise OSError("write failed")

    journal.after_dispatch = failed_finish

    async def send(request):
        return SimpleNamespace(status_code=200, headers={})

    with observe_transport_attempts(journal), pytest.raises(OSError):
        await _record_send(send, None, request_id_header="x-request-id")
    assert journal.rows == [{"state": "potentially_dispatched", "outcome": None}]


async def test_concurrent_and_nested_observers_keep_separate_attempts():
    first, second, inner = Journal(), Journal(), Journal()

    async def send(request):
        await asyncio.sleep(0)
        return SimpleNamespace(status_code=200, headers={"x-request-id": "invalid secret\n"})

    async def request(journal):
        with observe_transport_attempts(journal):
            await _record_send(send, None, request_id_header="x-request-id")

    with observe_transport_attempts(first):
        await asyncio.gather(request(second), request(inner))
        await _record_send(send, None, request_id_header="x-request-id")
    await _record_send(send, None, request_id_header="x-request-id")
    for journal in (first, second, inner):
        assert len(journal.rows) == 1
        assert journal.rows[0]["outcome"]["request_id"] is None


async def test_cancelled_send_stays_cancelled_when_outcome_storage_fails():
    journal = Journal()

    async def failed_finish(attempt_id, outcome):
        raise OSError("storage unavailable")

    journal.after_dispatch = failed_finish

    async def send(request):
        raise asyncio.CancelledError()

    with observe_transport_attempts(journal), pytest.raises(asyncio.CancelledError):
        await _record_send(send, None, request_id_header="x-request-id")
    assert journal.rows == [{"state": "potentially_dispatched", "outcome": None}]
