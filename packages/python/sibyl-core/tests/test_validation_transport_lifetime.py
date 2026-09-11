"""Only validation-owned transports close across every allocation boundary."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr

from sibyl_core.ai import providers
from sibyl_core.ai.llm.config import LLMConfig
from sibyl_core.services import procedure_validation as validation
from sibyl_core.tasks.consolidation import ConsolidationInputBudgetExceeded


@pytest.fixture(params=["openai", "anthropic"])
def owned_transport(request, monkeypatch):
    provider = request.param
    config = LLMConfig(
        provider=provider,
        model="offline-model",
        api_key=SecretStr("offline-only"),
        max_tokens=128,
    )
    monkeypatch.setattr(
        validation,
        "resolve_llm_config",
        AsyncMock(return_value=SimpleNamespace(to_llm_config=lambda: config)),
    )
    monkeypatch.setattr(validation.settings, "consolidation_output_mode", "tool")
    monkeypatch.setattr(validation.settings, "consolidation_openrouter_provider", None)
    name = "RecordingOpenAIClient" if provider == "openai" else "RecordingAnthropicClient"
    constructor = getattr(providers, name)
    clients = []

    def create():
        client = constructor()
        clients.append(client)
        return client

    monkeypatch.setattr(providers, name, create)
    original = SimpleNamespace(prepared=SimpleNamespace(prompt="evidence"))
    monkeypatch.setattr(
        validation, "prepare_stored_procedure_validation", AsyncMock(return_value=original)
    )
    return provider, clients


async def invoke():
    return await validation.validate_stored_procedure(
        organization_id="org", principal_id="owner", parent_id="parent", authorize=AsyncMock()
    )


@pytest.mark.parametrize("outcome", ["success", "resume", "failure", "cancel"])
async def test_owned_transport_closes_after_validation(owned_transport, monkeypatch, outcome):
    _, clients = owned_transport
    error = asyncio.CancelledError() if outcome == "cancel" else RuntimeError("failed")
    result = {"status": outcome}
    run = AsyncMock(return_value=result)
    if outcome in {"failure", "cancel"}:
        run.side_effect = error
    monkeypatch.setattr(validation, "_validate_prepared_procedure", run)
    if run.side_effect:
        with pytest.raises(type(error)):
            await invoke()
    else:
        assert await invoke() == result
    assert len(clients) == 1 and clients[0].is_closed


async def test_prompt_budget_rejects_before_transport_allocation(owned_transport, monkeypatch):
    _, clients = owned_transport
    monkeypatch.setattr(validation.settings, "consolidation_max_input_chars", 1)
    with pytest.raises(ConsolidationInputBudgetExceeded):
        await invoke()
    assert clients == []


async def test_provider_schema_budget_closes_allocated_transport(owned_transport, monkeypatch):
    _, clients = owned_transport
    monkeypatch.setattr(validation.settings, "consolidation_max_input_chars", len("evidence") + 1)
    with pytest.raises(ConsolidationInputBudgetExceeded):
        await invoke()
    assert len(clients) == 1 and clients[0].is_closed


@pytest.mark.parametrize("boundary", ["model", "agent", "extractor", "schema", "schema_cancel"])
async def test_failed_construction_closes_allocated_transport(
    owned_transport, monkeypatch, boundary
):
    provider, clients = owned_transport
    error = (
        asyncio.CancelledError() if boundary == "schema_cancel" else RuntimeError("construction")
    )

    def fail(*args, **kwargs):
        raise error

    if boundary == "model":
        name = "OpenAIResponsesModel" if provider == "openai" else "AnthropicModel"
        monkeypatch.setattr(providers, name, fail)
    elif boundary == "agent":
        monkeypatch.setattr(validation, "Agent", fail)
    elif boundary == "extractor":
        monkeypatch.setattr(validation, "_OwnedValidationExtractor", fail)
    else:
        monkeypatch.setattr(validation.Extractor, "output_schema", AsyncMock(side_effect=error))
    with pytest.raises(type(error)):
        await validation.validation_extractor()
    assert len(clients) == 1 and clients[0].is_closed


async def test_injected_extractor_remains_caller_owned(owned_transport, monkeypatch):
    _, clients = owned_transport
    extractor, policy = await validation.validation_extractor()
    injected = validation.Extractor(validation.CriticOutput, agent=extractor._agent)
    monkeypatch.setattr(
        validation, "validation_extractor", AsyncMock(return_value=(injected, policy))
    )
    monkeypatch.setattr(validation, "_validate_prepared_procedure", AsyncMock(return_value={}))
    try:
        await invoke()
        assert len(clients) == 1 and not clients[0].is_closed
    finally:
        await extractor.resources.aclose()


async def test_cancellation_waits_for_owned_transport_close():
    from contextlib import AsyncExitStack

    started = asyncio.Event()
    release = asyncio.Event()
    closed = asyncio.Event()

    async def close():
        started.set()
        await release.wait()
        closed.set()

    resources = AsyncExitStack()
    resources.push_async_callback(close)
    task = asyncio.create_task(validation._close_resources(resources))
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()


@pytest.mark.parametrize("outcome", ["success", "resume", "failure", "cancel"])
@pytest.mark.parametrize("correction", [False, True])
async def test_reflection_transport_closes_after_stage(
    owned_transport, monkeypatch, outcome, correction
):
    from sibyl_core.services import reflection_validation as reflection

    _, clients = owned_transport
    error = asyncio.CancelledError() if outcome == "cancel" else RuntimeError("failed")
    result = {"status": outcome}
    run = AsyncMock(return_value=result)
    if outcome in {"failure", "cancel"}:
        run.side_effect = error
    monkeypatch.setattr(reflection, "_validate_prepared_reflection", run)
    review = object() if correction else None
    if run.side_effect:
        with pytest.raises(type(error)):
            await reflection.validate_reflection_stage(SimpleNamespace(), AsyncMock(), review)
    else:
        assert (
            await reflection.validate_reflection_stage(SimpleNamespace(), AsyncMock(), review)
            == result
        )
    assert run.call_args.args[-1] is review
    assert len(clients) == 1 and clients[0].is_closed


async def test_reflection_schema_budget_closes_allocated_transport(owned_transport, monkeypatch):
    from sibyl_core.services import reflection_validation as reflection

    _, clients = owned_transport
    monkeypatch.setattr(validation.settings, "consolidation_max_input_chars", 1)
    original = SimpleNamespace(
        memory=SimpleNamespace(), prepared=SimpleNamespace(prompt="evidence")
    )
    with pytest.raises(ConsolidationInputBudgetExceeded):
        await reflection.validate_reflection_stage(original, AsyncMock())
    assert len(clients) == 1 and clients[0].is_closed
