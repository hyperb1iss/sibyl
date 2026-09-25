"""Only validation-owned transports close across every allocation boundary."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

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


@pytest.mark.parametrize(
    ("model", "effort"), [("claude-opus-5", None), ("claude-opus-5-5", "high")]
)
@pytest.mark.parametrize("override", [None, "8192"])
async def test_opus_memory_output_default_binds_wire_policy_and_reservation(
    monkeypatch, override, model, effort
):
    import json

    import httpx2 as httpx

    from sibyl_core.ai.llm.budget import llm_budget_context, set_budget_enforcer
    from sibyl_core.ai.llm.config import EnvConfigSource
    from sibyl_core.ai.transport import RecordingAnthropicClient

    environment = {"SIBYL_LLM_MEMORY_MODEL": model, "ANTHROPIC_API_KEY": "offline"}
    if override is not None:
        environment["SIBYL_LLM_MEMORY_MAX_TOKENS"] = override
    monkeypatch.setattr(validation, "resolve_llm_config", EnvConfigSource(environment).resolve)
    monkeypatch.setattr(validation.settings, "consolidation_output_mode", "native_strict")
    monkeypatch.setattr(validation.settings, "consolidation_openrouter_provider", None)
    capacity = 32768 if override is None else 8192
    reservations = []
    requests = []

    class Budget:
        async def reserve(self, context, *, surface, estimated_tokens):
            reservations.append(estimated_tokens)

        async def settle(self, context, *, surface, reserved_tokens, actual_tokens, period=None):
            pass

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        assert reservations, "reservation must precede physical dispatch"
        assert body["max_tokens"] == capacity
        # Opus 5 runs at its own default; Opus 5.5 is pinned above its medium default.
        assert "thinking" not in body and body.get("output_config", {}).get("effort") == effort
        return httpx.Response(
            200,
            json={
                "id": "msg_offline",
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [
                    {
                        "type": "text",
                        "text": '{"findings":[],"abstention_reason":"Insufficient synthetic evidence"}',
                    }
                ],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 20},
            },
        )

    monkeypatch.setattr(
        providers,
        "RecordingAnthropicClient",
        lambda: RecordingAnthropicClient(transport=httpx.MockTransport(respond)),
    )
    extractor, policy = await validation.validation_extractor()
    parsed = json.loads(policy)
    assert parsed["max_tokens"] == capacity
    assert parsed["model_settings"]["max_tokens"] == capacity
    # Effort joins the policy only when set, so existing Opus 5 identities are unchanged.
    assert parsed["model_settings"].get("anthropic_effort") == effort
    assert ("anthropic_effort" in parsed["model_settings"]) is (effort is not None)
    assert extractor.max_tokens == capacity
    set_budget_enforcer(Budget())
    try:
        with llm_budget_context(user_id="owner", organization_id="org"):
            result = await extractor.extract_with_usage("Synthetic evidence")
        assert result.usage.requests == 1 and result.usage.transport_usage_complete
    finally:
        set_budget_enforcer(None)
        await validation._close_resources(extractor.resources)
    assert len(requests) == 1
    schema = await extractor.output_schema()
    expected = len("Synthetic evidence\n" + json.dumps(schema, sort_keys=True)) // 4 + capacity
    # One attempt is reserved up front; retries reserve as they dispatch.
    assert reservations == [expected]


async def test_memory_validation_runs_native_output_on_a_model_that_rejects_forced_tools(
    monkeypatch,
):
    import json

    import httpx2 as httpx

    from sibyl_core.ai.llm.config import EnvConfigSource
    from sibyl_core.ai.transport import RecordingAnthropicClient

    environment = {"SIBYL_LLM_MEMORY_MODEL": "claude-opus-5-5", "ANTHROPIC_API_KEY": "offline"}
    monkeypatch.setattr(validation, "resolve_llm_config", EnvConfigSource(environment).resolve)
    # The configured default is tool mode, which forces its output tool.
    monkeypatch.setattr(validation.settings, "consolidation_output_mode", "tool")
    monkeypatch.setattr(validation.settings, "consolidation_openrouter_provider", None)
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "msg_offline",
                "type": "message",
                "role": "assistant",
                "model": "claude-opus-5-5",
                "content": [
                    {
                        "type": "text",
                        "text": '{"findings":[],"abstention_reason":"Insufficient synthetic evidence"}',
                    }
                ],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 20},
            },
        )

    monkeypatch.setattr(
        providers,
        "RecordingAnthropicClient",
        lambda: RecordingAnthropicClient(transport=httpx.MockTransport(respond)),
    )
    extractor, policy = await validation.validation_extractor()
    try:
        assert json.loads(policy)["output_mode"] == "native_strict"
        assert extractor.output_mode == "native_strict"
        assert await extractor.resolved_output_mode() == "native_strict"
        assert await extractor.resolved_effort() == "high"
        result = await extractor.extract_with_usage("Synthetic evidence")
        assert result.usage.requests == 1
        assert "tool_choice" not in requests[0] and "tools" not in requests[0]
        assert requests[0]["output_config"]["format"]["type"] == "json_schema"
    finally:
        await validation._close_resources(extractor.resources)


class _Admitted(Exception):
    """Raised by the first step after a budget guard: the guard let the input through."""


class _Models:
    """A memory config source that serves one model per resolution, the last repeating."""

    def __init__(self, *models):
        self.models = list(models)

    async def resolve(self, surface):
        from sibyl_core.ai.llm.config import EnvConfigSource

        model = self.models.pop(0) if len(self.models) > 1 else self.models[0]
        environment = {"SIBYL_LLM_MEMORY_MODEL": model, "ANTHROPIC_API_KEY": "offline-only"}
        return await EnvConfigSource(environment).resolve(surface)

    async def invalidate(self, surface=None):
        return None


@pytest.fixture
def memory_models(monkeypatch):
    """Resolve the memory model through the global source, as production does."""
    from sibyl_core.ai.llm import config as llm_config

    monkeypatch.setattr(validation.settings, "consolidation_max_input_chars", None)
    monkeypatch.setattr(validation.settings, "consolidation_output_mode", "tool")
    monkeypatch.setattr(validation.settings, "consolidation_openrouter_provider", None)
    clients = []
    constructor = providers.RecordingAnthropicClient

    def create():
        client = constructor()
        clients.append(client)
        return client

    monkeypatch.setattr(providers, "RecordingAnthropicClient", create)

    def use(*models):
        monkeypatch.setattr(llm_config, "_config_source", _Models(*models))

    return use, clients


def _procedure(monkeypatch, chars):
    original = SimpleNamespace(
        prepared=SimpleNamespace(prompt="e" * chars, input_sha256="input"),
        snapshot_sha256="snapshot",
        source_bindings=[],
    )
    monkeypatch.setattr(
        validation, "prepare_stored_procedure_validation", AsyncMock(return_value=original)
    )
    monkeypatch.setattr(validation, "review_digest", Mock(side_effect=_Admitted))


@pytest.mark.parametrize("model", ["claude-opus-5", "claude-opus-5-5"])
async def test_procedure_guards_admit_input_over_40k_on_the_opus_default(
    memory_models, monkeypatch, model
):
    use, clients = memory_models
    use(model)
    _procedure(monkeypatch, 100_000)
    with pytest.raises(_Admitted):
        await invoke()
    assert len(clients) == 1 and clients[0].is_closed


async def test_procedure_refuses_input_over_40k_on_a_model_without_a_default(
    memory_models, monkeypatch
):
    use, clients = memory_models
    use("claude-haiku-4-5")
    _procedure(monkeypatch, 100_000)
    with pytest.raises(ConsolidationInputBudgetExceeded):
        await invoke()
    assert clients == []


async def test_a_model_change_after_the_procedure_precheck_fails_closed(memory_models, monkeypatch):
    use, clients = memory_models
    use("claude-opus-5-5", "claude-haiku-4-5")
    _procedure(monkeypatch, 100_000)
    with pytest.raises(
        ConsolidationInputBudgetExceeded, match=r"for claude-haiku-4-5\S* is 40000 characters"
    ):
        await invoke()
    assert len(clients) == 1 and clients[0].is_closed


@pytest.mark.parametrize("correction", [False, True])
@pytest.mark.parametrize(
    ("model", "admitted"), [("claude-opus-5-5", True), ("claude-haiku-4-5", False)]
)
async def test_reflection_guard_follows_the_extractors_model(
    memory_models, monkeypatch, correction, model, admitted
):
    from sibyl_core.services import ordinary_publication
    from sibyl_core.services import reflection_validation as reflection
    from sibyl_core.tasks import reflection_correction

    use, clients = memory_models
    use(model)
    original = SimpleNamespace(
        memory=SimpleNamespace(), prepared=SimpleNamespace(prompt="e" * 100_000)
    )
    monkeypatch.setattr(
        reflection_correction,
        "prepare_reflection_correction",
        lambda prepared, review: "e" * 100_000,
    )
    monkeypatch.setattr(
        ordinary_publication, "ordinary_semantic_digest", Mock(side_effect=_Admitted)
    )
    review = object() if correction else None
    expected = _Admitted if admitted else ConsolidationInputBudgetExceeded
    with pytest.raises(expected):
        await reflection.validate_reflection_stage(original, AsyncMock(), review)
    assert len(clients) == 1 and clients[0].is_closed
