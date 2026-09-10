"""Completed validation usage survives result-write errors without redispatch."""

import json
from unittest.mock import AsyncMock

import pytest

from sibyl_core.services import procedure_validation as validation
from sibyl_core.services.validation_execution import (
    ValidationExecution,
    ValidationExecutionUnavailable,
)
from tests.test_validation_execution import admitted_pair as admitted_pair
from tests.test_validation_execution import candidate as candidate
from tests.test_validation_execution import content_store as content_store
from tests.test_validation_execution import evidence as evidence
from tests.test_validation_execution import proposal as proposal
from tests.test_validation_execution import rows

_REAL_FACTORY = validation.validation_extractor


def arguments(candidate):
    return {
        "organization_id": "org",
        "principal_id": "owner",
        "parent_id": candidate.id,
        "authorize": AsyncMock(),
    }


@pytest.mark.parametrize("committed", [False, True])
async def test_result_write_error_recovers_exact_result_without_redispatch(
    candidate, monkeypatch, committed
):
    original = ValidationExecution.record_result
    completed = []

    async def write_failure(self, result):
        completed.append(result)
        if committed:
            await original(self, result)
        raise OSError("result acknowledgment failed")

    monkeypatch.setattr(ValidationExecution, "record_result", write_failure)
    first = await validation.validate_stored_procedure(**arguments(candidate))
    assert first["status"] == "no_findings" and first["usage"]["requests"] == 1
    row = (await rows("memory_validation_executions"))[0]
    assert row["state"] == "returned"
    assert json.loads(row["usage_json"])["requests"] == 1
    second = await validation.validate_stored_procedure(**arguments(candidate))
    assert first == second and len(completed) == 1


@pytest.mark.parametrize("state", ["fenced", "failed", "cancelled"])
async def test_result_recovery_never_replaces_concurrent_terminal_state(
    candidate, monkeypatch, state
):
    error = OSError("write failed")

    async def write_failure(self, result):
        await validation._query(
            "UPDATE memory_validation_executions SET state=$state, error_type='concurrent' WHERE uuid=$uuid;",
            uuid=self.id,
            state=state,
        )
        raise error

    monkeypatch.setattr(ValidationExecution, "record_result", write_failure)
    with pytest.raises(OSError) as caught:
        await validation.validate_stored_procedure(**arguments(candidate))
    assert caught.value is error and error.extraction_usage["requests"] == 1
    row = (await rows("memory_validation_executions"))[0]
    assert row["state"] == state and row["error_type"] == "concurrent"
    assert row.get("result_json") is None


async def test_persistent_outage_retains_original_error_and_known_usage(candidate, monkeypatch):
    error = OSError("original result write unavailable")
    completed = []
    load = ValidationExecution.load

    async def write_failure(self, result):
        completed.append(result)
        raise error

    monkeypatch.setattr(ValidationExecution, "record_result", write_failure)
    monkeypatch.setattr(ValidationExecution, "load", AsyncMock(side_effect=OSError("still down")))
    with pytest.raises(OSError) as caught:
        await validation.validate_stored_procedure(**arguments(candidate))
    assert caught.value is error
    assert error.extraction_usage == completed[0].usage.model_dump(mode="json")
    assert isinstance(error.__cause__, OSError) and str(error.__cause__) == "still down"
    row = (await rows("memory_validation_executions"))[0]
    assert row["state"] == "running" and row.get("usage_json") is None
    monkeypatch.setattr(ValidationExecution, "load", load)
    with pytest.raises(ValidationExecutionUnavailable, match="running"):
        await validation.validate_stored_procedure(**arguments(candidate))
    assert len(completed) == 1


async def test_different_recorded_result_is_not_replaced(candidate, monkeypatch):
    original = ValidationExecution.record_result
    error = OSError("lost acknowledgment")

    async def write_failure(self, result):
        await original(self, result)
        await validation._query(
            "UPDATE memory_validation_executions SET usage_json='{}' WHERE uuid=$uuid;",
            uuid=self.id,
        )
        raise error

    monkeypatch.setattr(ValidationExecution, "record_result", write_failure)
    with pytest.raises(OSError) as caught:
        await validation.validate_stored_procedure(**arguments(candidate))
    assert caught.value is error
    row = (await rows("memory_validation_executions"))[0]
    assert row["state"] == "recorded" and row["usage_json"] == "{}"


async def test_terminal_write_after_recovery_read_is_not_overwritten(candidate, monkeypatch):
    from sibyl_core.services import validation_execution as execution_store

    error = OSError("first write failed")
    original_query = execution_store._query
    raced = []

    async def write_failure(self, result):
        raise error

    async def race(query, **params):
        if "AND request_sha256 = $uuid AND state = 'running' AND purged = false" in query:
            await original_query(
                "UPDATE memory_validation_executions SET state='cancelled', error_type='concurrent' WHERE uuid=$uuid;",
                uuid=params["uuid"],
            )
            raced.append(True)
        return await original_query(query, **params)

    monkeypatch.setattr(ValidationExecution, "record_result", write_failure)
    monkeypatch.setattr(execution_store, "_query", race)
    with pytest.raises(OSError) as caught:
        await validation.validate_stored_procedure(**arguments(candidate))
    assert caught.value is error and raced == [True]
    row = (await rows("memory_validation_executions"))[0]
    assert row["state"] == "cancelled" and row["error_type"] == "concurrent"
    assert row.get("result_json") is None and row.get("usage_json") is None


async def test_real_execution_and_replay_close_each_factory_owned_client(candidate, monkeypatch):
    from types import SimpleNamespace

    from pydantic import SecretStr

    from sibyl_core.ai import providers
    from sibyl_core.ai.llm.config import LLMConfig

    reader, _ = await validation.validation_extractor()
    # The candidate fixture supplies a caller-owned offline reader.
    monkeypatch.setattr(validation, "validation_extractor", _REAL_FACTORY)
    config = LLMConfig(provider="openai", model="offline", api_key=SecretStr("offline-only"))
    monkeypatch.setattr(
        validation,
        "resolve_llm_config",
        AsyncMock(return_value=SimpleNamespace(to_llm_config=lambda: config)),
    )
    monkeypatch.setattr(validation.settings, "consolidation_output_mode", "tool")
    monkeypatch.setattr(validation.settings, "consolidation_openrouter_provider", None)
    clients = []
    create = providers.RecordingOpenAIClient

    def tracked():
        client = create()
        clients.append(client)
        return client

    monkeypatch.setattr(providers, "RecordingOpenAIClient", tracked)
    run = validation.run_memory_validation

    async def offline(prepared, owned):
        return await run(prepared, reader)

    monkeypatch.setattr(validation, "run_memory_validation", offline)
    first = await validation.validate_stored_procedure(**arguments(candidate))
    monkeypatch.setattr(
        validation, "run_memory_validation", AsyncMock(side_effect=AssertionError("redispatch"))
    )
    second = await validation.validate_stored_procedure(**arguments(candidate))
    assert first == second and first["status"] == "no_findings"
    assert len(clients) == 2 and all(client.is_closed for client in clients)


@pytest.mark.parametrize("timing", ["before_read", "before_update"])
@pytest.mark.parametrize("mutation", ["changed", "noncanonical"])
async def test_result_recovery_binds_exact_canonical_request(
    candidate, monkeypatch, timing, mutation
):
    from sibyl_core.services import validation_execution as execution_store

    failure = OSError("original persistence failure")
    original_query = execution_store._query
    changed = []

    async def mutate(identity):
        row = (await rows("memory_validation_executions"))[0]
        value = (
            "{}" if mutation == "changed" else json.dumps(json.loads(row["request_json"]), indent=2)
        )
        await original_query(
            "UPDATE memory_validation_executions SET request_json=$value WHERE uuid=$uuid;",
            uuid=identity,
            value=value,
        )
        changed.append(value)

    async def write_failure(self, result):
        if timing == "before_read":
            await mutate(self.id)
        raise failure

    async def race(query, **params):
        if timing == "before_update" and "AND request_json = $request_json RETURN AFTER" in query:
            await mutate(params["uuid"])
        return await original_query(query, **params)

    monkeypatch.setattr(ValidationExecution, "record_result", write_failure)
    monkeypatch.setattr(execution_store, "_query", race)
    with pytest.raises(OSError) as caught:
        await validation.validate_stored_procedure(**arguments(candidate))
    assert caught.value is failure and failure.extraction_usage["requests"] == 1
    row = (await rows("memory_validation_executions"))[0]
    assert len(changed) == 1 and row["request_json"] == changed[0]
    assert row["state"] == "running" and row.get("result_json") is None
    assert row.get("usage_json") is None
