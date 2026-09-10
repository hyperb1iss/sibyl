"""Durable dispatch accounting survives validation and source-fence failures."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from sibyl_core.ai.llm.extractor import Extractor
from sibyl_core.ai.transport import TransportAttempt
from sibyl_core.services import eval_publication as publication
from sibyl_core.services import procedure_validation as validation
from sibyl_core.services.validation_execution import (
    ValidationExecution,
    ValidationExecutionUnavailable,
)
from sibyl_core.tasks.memory_validation import CriticOutput
from tests.test_eval_publication import admitted_pair as admitted_pair
from tests.test_eval_publication import content_store as content_store
from tests.test_eval_publication import evidence as evidence
from tests.test_eval_publication import proposal as proposal
from tests.test_eval_publication import rows

_REAL_EXTRACT = Extractor.extract_with_usage


@pytest.fixture
async def candidate(proposal, monkeypatch):
    op, result = proposal
    stored = await publication.store_consolidation(op, result)
    monkeypatch.setattr(Extractor, "extract_with_usage", _REAL_EXTRACT)
    reader = Extractor(
        CriticOutput,
        agent=Agent(TestModel(custom_output_args={"findings": []}), output_type=CriticOutput),
    )
    monkeypatch.setattr(
        validation, "validation_extractor", AsyncMock(return_value=(reader, '{"model":"offline"}'))
    )
    return stored.memory


async def test_validation_execution_replay_preserves_result(candidate):
    auth = AsyncMock()
    first = await validation.validate_stored_procedure(
        organization_id="org", principal_id="owner", parent_id=candidate.id, authorize=auth
    )
    assert (await rows("memory_validation_executions"))[0]["state"] == "returned"
    second = await validation.validate_stored_procedure(
        organization_id="org", principal_id="owner", parent_id=candidate.id, authorize=auth
    )
    assert first == second
    assert first["status"] == "no_findings" and first["submission"] is None
    assert first["usage"]["requests"] == 1
    assert len(await rows("memory_validation_executions")) == 1


async def test_validation_execution_physical_begin_unknown(content_store):
    from sibyl_core.tasks.procedure_review import review_digest

    request = {
        "org": "org",
        "principal": "owner",
        "parent": "parent",
        "source_bindings": [],
        "policy": "{}",
    }
    execution = ValidationExecution(review_digest(request), "org", "owner")
    assert await execution.begin(
        parent_id="parent", source_ids=["source"], policy="{}", request=request
    )
    first = await execution.before_dispatch()
    second = await execution.before_dispatch()
    await execution.after_dispatch(second, TransportAttempt(status_code=200, request_id="safe"))
    attempts = await rows("memory_validation_attempts")
    assert len(attempts) == 2
    assert next(x for x in attempts if x["uuid"] == first).get("outcome_json") is None
    assert (
        json.loads(next(x for x in attempts if x["uuid"] == second)["outcome_json"])["usage_known"]
        is False
    )


async def test_validation_execution_wrong_owner_zero_calls(candidate):
    with pytest.raises(ValidationExecutionUnavailable):
        await validation.validate_stored_procedure(
            organization_id="org",
            principal_id="other",
            parent_id=candidate.id,
            authorize=AsyncMock(),
        )
    assert not await rows("memory_validation_executions")


async def test_validation_execution_final_authority_failure_retains_usage(candidate):
    calls = 0

    async def auth():
        nonlocal calls
        calls += 1
        if calls == 3:
            raise PermissionError("authority retired")

    with pytest.raises(PermissionError):
        await validation.validate_stored_procedure(
            organization_id="org", principal_id="owner", parent_id=candidate.id, authorize=auth
        )
    row = (await rows("memory_validation_executions"))[0]
    assert row["state"] == "fenced"
    assert json.loads(row["usage_json"])["requests"] == 1
    assert row["result_json"]


@pytest.mark.parametrize("behavior", ["retry", "cancel", "purge"])
async def test_validation_execution_actual_sdk_durable_attempts(candidate, monkeypatch, behavior):
    from openai import AsyncOpenAI, _base_client

    httpx = getattr(_base_client, "httpx2", None) or _base_client.httpx
    from pydantic_ai.models.openai import OpenAIResponsesModel
    from pydantic_ai.providers.openai import OpenAIProvider

    from sibyl_core.ai.transport import RecordingOpenAIClient
    from sibyl_core.services import content_client

    sent = []

    async def respond(request):
        sent.append(request)
        assert len(await rows("memory_validation_attempts")) == len(sent)
        if behavior == "cancel":
            raise asyncio.CancelledError()
        if behavior == "retry" and len(sent) < 3:
            return httpx.Response(500, json={"error": {"message": "offline"}})
        if behavior == "purge":
            async with content_client.surreal_content_client() as client:
                await client.execute_query("DELETE raw_captures WHERE uuid=$id;", id=candidate.id)
        name = json.loads(request.content)["tools"][0]["name"]
        return httpx.Response(
            200,
            json={
                "id": "resp_offline",
                "object": "response",
                "created_at": 0,
                "status": "completed",
                "model": "offline",
                "output": [
                    {
                        "id": "fc_offline",
                        "type": "function_call",
                        "call_id": "call_offline",
                        "name": name,
                        "arguments": '{"findings":[]}',
                        "status": "completed",
                    }
                ],
                "usage": {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10},
            },
        )

    async with AsyncOpenAI(
        api_key="private-test-key",
        max_retries=2,
        http_client=RecordingOpenAIClient(transport=httpx.MockTransport(respond)),
    ) as client:
        model = OpenAIResponsesModel("offline", provider=OpenAIProvider(openai_client=client))
        reader = Extractor(
            CriticOutput, agent=Agent(model, output_type=CriticOutput), output_retries=0
        )
        monkeypatch.setattr(
            validation,
            "validation_extractor",
            AsyncMock(return_value=(reader, '{"model":"offline","transport_retries":2}')),
        )
        if behavior == "retry":
            result = await validation.validate_stored_procedure(
                organization_id="org",
                principal_id="owner",
                parent_id=candidate.id,
                authorize=AsyncMock(),
            )
            assert result["usage"]["input_tokens"] == 7 and result["usage"]["output_tokens"] == 3
            assert len(sent) == 3
            assert [
                json.loads(row["outcome_json"])["status_code"]
                for row in await rows("memory_validation_attempts")
            ].count(500) == 2
        else:
            with pytest.raises(
                asyncio.CancelledError if behavior == "cancel" else ValidationExecutionUnavailable
            ):
                await validation.validate_stored_procedure(
                    organization_id="org",
                    principal_id="owner",
                    parent_id=candidate.id,
                    authorize=AsyncMock(),
                )
            row = (await rows("memory_validation_executions"))[0]
            assert row["state"] == ("cancelled" if behavior == "cancel" else "fenced")
            assert row["usage_json"]
            if behavior == "purge":
                assert row["purged"] is True and row.get("result_json") is None
                assert json.loads(row["usage_json"])["input_tokens"] == 7
            else:
                assert json.loads(row["usage_json"])["usage_complete"] is False
        assert "private-test-key" not in str(await rows("memory_validation_attempts"))
        assert all(
            not json.loads(row["outcome_json"])["usage_known"]
            for row in await rows("memory_validation_attempts")
        )


async def test_validation_execution_final_snapshot_cas(candidate, monkeypatch):
    from sibyl_core.services import content_client

    query = validation._query
    mutated = False

    async def race(sql, **params):
        nonlocal mutated
        if "SET state = IF" in sql and not mutated:
            mutated = True
            async with content_client.surreal_content_client() as client:
                await client.execute_query(
                    "UPDATE raw_captures SET metadata.changed_after_review=true WHERE uuid=$id;",
                    id=candidate.id,
                )
        return await query(sql, **params)

    monkeypatch.setattr(validation, "_query", race)
    with pytest.raises(ValidationExecutionUnavailable):
        await validation.validate_stored_procedure(
            organization_id="org",
            principal_id="owner",
            parent_id=candidate.id,
            authorize=AsyncMock(),
        )
    row = (await rows("memory_validation_executions"))[0]
    assert mutated and row["state"] == "fenced"
    assert json.loads(row["usage_json"])["requests"] == 1


async def test_validation_execution_duplicate_claim_does_not_repeat(candidate):
    async def call():
        return await validation.validate_stored_procedure(
            organization_id="org",
            principal_id="owner",
            parent_id=candidate.id,
            authorize=AsyncMock(),
        )

    outcomes = await asyncio.gather(call(), call(), return_exceptions=True)
    assert any(isinstance(value, dict) and value["status"] == "no_findings" for value in outcomes)
    assert all(isinstance(value, dict | ValidationExecutionUnavailable) for value in outcomes)
    executions = await rows("memory_validation_executions")
    assert len(executions) == 1 and executions[0]["state"] == "returned"
    assert json.loads(executions[0]["usage_json"])["requests"] == 1


@pytest.mark.parametrize("prior_version", [36, 37])
async def test_validation_execution_schema_upgrade_preserves_sources(candidate, prior_version):
    from sibyl_core.backends.surreal.content_schema import (
        CONTENT_SCHEMA_CURRENT_VERSION,
        bootstrap_content_schema,
    )
    from sibyl_core.backends.surreal.schema_version import get_schema_version

    before = await rows("raw_captures")
    states = await rows("source_states")
    from sibyl_core.services.content_client import surreal_content_client

    async with surreal_content_client() as client:
        if prior_version == 36:
            await client.execute_query("REMOVE TABLE memory_validation_attempts;")
            await client.execute_query("REMOVE TABLE memory_validation_executions;")
        await client.execute_query("REMOVE FIELD validation_write_witness ON source_states;")
        await client.execute_query(
            "UPDATE schema_version SET version=$prior WHERE name='content';", prior=prior_version
        )
        await bootstrap_content_schema(client)
        assert (
            await get_schema_version(client.execute_query, name="content")
            == CONTENT_SCHEMA_CURRENT_VERSION
        )
        info = await client.execute_query("INFO FOR TABLE source_states;")
        assert "validation_write_witness" in str(info)
    assert await rows("raw_captures") == before
    assert await rows("source_states") == states
    assert await rows("memory_validation_executions") == []


async def test_validation_execution_factory_freezes_actual_policy(monkeypatch):
    from types import SimpleNamespace

    from pydantic import SecretStr

    from sibyl_core.ai.llm.config import LLMConfig

    config = LLMConfig(
        provider="openai",
        model="offline-unregistered",
        api_key=SecretStr("never-persist"),
        max_tokens=1234,
        temperature=0.25,
    )
    resolver = AsyncMock(return_value=SimpleNamespace(to_llm_config=lambda: config))
    monkeypatch.setattr(validation, "resolve_llm_config", resolver)
    monkeypatch.setattr(validation.settings, "consolidation_output_mode", "tool")
    monkeypatch.setattr(validation.settings, "consolidation_openrouter_provider", None)
    extractor, encoded = await validation.validation_extractor()
    policy = json.loads(encoded)
    assert policy["model"] == "offline-unregistered"
    assert policy["model_settings"]["temperature"] == 0.25
    assert policy["max_tokens"] == extractor.max_tokens == 1234
    assert policy["output_retries"] == extractor.output_retries == 2
    assert policy["transport"]["max_retries"] == extractor._agent.model.client.max_retries == 2
    assert policy["schema"] == await extractor.output_schema()
    assert "never-persist" not in encoded
    resolver.assert_awaited_once()
    config.model = "changed-after-resolution"
    assert extractor._agent.model.model_name == "offline-unregistered"
    await extractor._agent.model.client.close()


async def test_validation_execution_budget_denial_has_no_execution(candidate, monkeypatch):
    from sibyl_core.tasks.consolidation import ConsolidationInputBudgetExceeded

    monkeypatch.setattr(validation.settings, "consolidation_max_input_chars", 1)
    with pytest.raises(ConsolidationInputBudgetExceeded) as error:
        await validation.validate_stored_procedure(
            organization_id="org",
            principal_id="owner",
            parent_id=candidate.id,
            authorize=AsyncMock(),
        )
    assert error.value.actual_chars > error.value.max_input_chars == 1
    assert not await rows("memory_validation_executions")


async def test_validation_execution_cancellation_during_result_write(candidate, monkeypatch):
    entered = asyncio.Event()
    release = asyncio.Event()
    original = ValidationExecution.record_result

    async def delayed(self, result):
        entered.set()
        await release.wait()
        await original(self, result)

    monkeypatch.setattr(ValidationExecution, "record_result", delayed)
    task = asyncio.create_task(
        validation.validate_stored_procedure(
            organization_id="org",
            principal_id="owner",
            parent_id=candidate.id,
            authorize=AsyncMock(),
        )
    )
    await entered.wait()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    row = (await rows("memory_validation_executions"))[0]
    assert row["state"] == "recorded"
    assert json.loads(row["usage_json"])["requests"] == 1
    with pytest.raises(ValidationExecutionUnavailable):
        await ValidationExecution(row["uuid"], "org", "owner").result()

    forbidden = AsyncMock(side_effect=AssertionError("recorded result repeated extraction"))
    monkeypatch.setattr(validation, "run_memory_validation", forbidden)

    async def resume():
        return await validation.validate_stored_procedure(
            organization_id="org",
            principal_id="owner",
            parent_id=candidate.id,
            authorize=AsyncMock(),
        )

    first, second = await asyncio.gather(resume(), resume())
    assert first == second and first["status"] == "no_findings"
    assert first["usage"]["requests"] == 1
    forbidden.assert_not_awaited()
