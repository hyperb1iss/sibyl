"""Claude on Amazon Bedrock, with the Bedrock transport stubbed: no AWS calls."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import httpx2 as httpx
import pytest
from pydantic import BaseModel
from pydantic_ai.exceptions import ModelHTTPError

from sibyl_core.ai import bedrock, clients, providers, validation
from sibyl_core.ai.bedrock import BedrockConfigError, resolve_bedrock_settings
from sibyl_core.ai.errors import LLMConfigError, LLMError
from sibyl_core.ai.llm import Extractor
from sibyl_core.ai.llm import extractor as extraction
from sibyl_core.ai.llm.config import (
    EnvConfigSource,
    LLMConfig,
    LLMSurface,
    memory_model_defaults,
)
from sibyl_core.ai.llm.extractor import effective_output_mode
from sibyl_core.ai.registry import canonical_model_alias
from sibyl_core.ai.transport import RecordingAnthropicClient, transport_policy

pytest.importorskip("boto3")

FIXTURE_KEY_ID = "AKIDEXAMPLEFIXTURE"


class Verdict(BaseModel):
    ok: bool
    summary: str


@pytest.fixture(autouse=True)
def aws_env(monkeypatch, tmp_path):
    """No ambient AWS state: no profiles, keys, metadata service or cached sessions."""
    for name in list(os.environ):
        if name.startswith(("AWS_", "SIBYL_BEDROCK_", "SIBYL_LLM_", "ANTHROPIC_")):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "missing-config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "missing-credentials"))
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    from anthropic.lib.bedrock import _auth

    bedrock._boto_session.cache_clear()
    _auth._get_session.cache_clear()
    yield
    bedrock._boto_session.cache_clear()
    _auth._get_session.cache_clear()
    clients.invalidate_agent_cache()


@pytest.fixture
def static_aws(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", FIXTURE_KEY_ID)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "fixture-secret")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "fixture-session")


def bedrock_config(model: str, **kwargs) -> LLMConfig:
    return LLMConfig(provider="bedrock", model=model, **kwargs)


# -----------------------------------------------------------------------------
# Settings and credentials
# -----------------------------------------------------------------------------


def test_region_prefers_sibyl_then_aws_region_then_default():
    assert resolve_bedrock_settings({"AWS_DEFAULT_REGION": "eu-west-1"}).region == "eu-west-1"
    assert (
        resolve_bedrock_settings({"AWS_REGION": "us-east-1", "AWS_DEFAULT_REGION": "x"}).region
        == "us-east-1"
    )
    assert (
        resolve_bedrock_settings(
            {"SIBYL_BEDROCK_REGION": "us-west-2", "AWS_REGION": "us-east-1"}
        ).region
        == "us-west-2"
    )


def test_missing_region_names_the_settings_that_fix_it():
    with pytest.raises(BedrockConfigError, match="SIBYL_BEDROCK_REGION or AWS_REGION"):
        resolve_bedrock_settings({})


def test_defaults_are_invoke_and_us_scope_with_sigv4():
    settings = resolve_bedrock_settings({"AWS_REGION": "us-west-2"})
    assert (settings.api, settings.inference_scope, settings.auth_mode) == ("invoke", "us", "sigv4")


@pytest.mark.parametrize(
    ("name", "value"),
    [("SIBYL_BEDROCK_INFERENCE_SCOPE", "mars"), ("SIBYL_BEDROCK_API", "converse")],
)
def test_unknown_scope_or_api_fails_clearly(name, value):
    with pytest.raises(BedrockConfigError, match=name):
        resolve_bedrock_settings({"AWS_REGION": "us-west-2", name: value})


def test_api_key_falls_back_to_the_aws_bearer_variable_and_never_prints():
    settings = resolve_bedrock_settings(
        {"AWS_REGION": "us-west-2", "AWS_BEARER_TOKEN_BEDROCK": "bedrock-key-secret"}
    )
    assert settings.auth_mode == "bearer"
    assert "bedrock-key-secret" not in repr(settings)
    assert "bedrock-key-secret" not in settings.fingerprint


def test_mantle_reads_the_extra_api_key_its_client_reads():
    env = {"AWS_REGION": "us-east-1", "ANTHROPIC_AWS_API_KEY": "k"}
    assert resolve_bedrock_settings(env).auth_mode == "sigv4"
    assert resolve_bedrock_settings({**env, "SIBYL_BEDROCK_API": "mantle"}).auth_mode == "bearer"


def test_api_key_and_profile_together_are_rejected():
    with pytest.raises(BedrockConfigError, match="not both"):
        resolve_bedrock_settings(
            {
                "AWS_REGION": "us-west-2",
                "SIBYL_BEDROCK_API_KEY": "k",
                "SIBYL_BEDROCK_PROFILE": "dev",
            }
        )


async def test_credentials_resolve_from_the_default_chain(static_aws):
    status = await bedrock.resolve_bedrock_credentials(resolve_bedrock_settings())
    assert (status.region, status.auth_mode, status.method) == ("us-west-2", "sigv4", "env")


async def test_missing_credentials_raise_a_fix_it_error(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    with pytest.raises(BedrockConfigError, match="No AWS credentials found"):
        await bedrock.resolve_bedrock_credentials(resolve_bedrock_settings())


# -----------------------------------------------------------------------------
# Model identifiers
# -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("scope", "model", "expected"),
    [
        ("us", "claude-opus-5-5", "us.anthropic.claude-opus-5-5"),
        ("global", "claude-opus-5-5", "global.anthropic.claude-opus-5-5"),
        ("regional", "claude-opus-5-5", "anthropic.claude-opus-5-5"),
        ("us", "claude-haiku-4-5", "us.anthropic.claude-haiku-4-5-20251001-v1:0"),
        ("global", "claude-haiku-4-5", "global.anthropic.claude-haiku-4-5-20251001-v1:0"),
        ("regional", "claude-haiku-4-5", "anthropic.claude-haiku-4-5-20251001-v1:0"),
        ("us", "claude-haiku-4-5-20251001", "us.anthropic.claude-haiku-4-5-20251001-v1:0"),
        ("us", "claude-sonnet-5", "us.anthropic.claude-sonnet-5"),
        ("global", "anthropic.claude-fable-5-1", "global.anthropic.claude-fable-5-1"),
        ("global", "us.anthropic.claude-opus-5", "us.anthropic.claude-opus-5"),
        ("eu", "claude-opus-5-5", "eu.anthropic.claude-opus-5-5"),
        ("us-gov", "claude-sonnet-4-5", "us-gov.anthropic.claude-sonnet-4-5-20250929-v1:0"),
        ("us", "claude-opus-4-6", "us.anthropic.claude-opus-4-6-v1"),
        (
            "us",
            "arn:aws:bedrock:us-west-2:123456789012:application-inference-profile/abc",
            "arn:aws:bedrock:us-west-2:123456789012:application-inference-profile/abc",
        ),
    ],
)
def test_bedrock_model_ids_follow_the_inference_scope(monkeypatch, scope, model, expected):
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    monkeypatch.setenv("SIBYL_BEDROCK_INFERENCE_SCOPE", scope)
    assert providers.resolve_provider_model_id(bedrock_config(model)) == expected


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("claude-haiku-4-5", "anthropic.claude-haiku-4-5"),
        ("us.anthropic.claude-haiku-4-5-20251001-v1:0", "anthropic.claude-haiku-4-5"),
        ("claude-opus-5-5", "anthropic.claude-opus-5-5"),
    ],
)
def test_mantle_uses_in_region_ids(monkeypatch, model, expected):
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("SIBYL_BEDROCK_API", "mantle")
    assert providers.resolve_provider_model_id(bedrock_config(model)) == expected


@pytest.mark.parametrize("model", ["gpt-5.4-mini", "amazon.nova-pro-v1:0"])
def test_bedrock_refuses_models_it_does_not_serve_here(monkeypatch, model):
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    with pytest.raises(LLMConfigError):
        providers.resolve_provider_model_id(bedrock_config(model))


@pytest.mark.parametrize(
    ("model", "alias"),
    [
        ("us.anthropic.claude-opus-5-5", "claude-opus-5-5"),
        ("anthropic.claude-haiku-4-5-20251001-v1:0", "claude-haiku-4-5"),
        ("global.anthropic.claude-fable-5-1", "claude-fable-5-1"),
        ("claude-opus-5", "claude-opus-5"),
    ],
)
def test_canonical_alias_strips_bedrock_routing(model, alias):
    assert canonical_model_alias(model) == alias


# -----------------------------------------------------------------------------
# Model rules shared across providers
# -----------------------------------------------------------------------------


def test_memory_defaults_hold_on_bedrock_for_alias_and_raw_id():
    alias = memory_model_defaults("bedrock", "claude-opus-5-5")
    raw = memory_model_defaults("bedrock", "us.anthropic.claude-opus-5-5")
    assert alias == raw == memory_model_defaults("anthropic", "claude-opus-5-5")
    assert alias is not None and alias.effort == "high"


def test_forced_tool_rule_and_effort_follow_the_alias(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    opus = bedrock_config("us.anthropic.claude-opus-5-5", effort="xhigh")
    assert providers.rejects_forced_tool_choice(opus)
    assert providers.anthropic_effort(opus) == "xhigh"
    assert providers.anthropic_effort(bedrock_config("claude-opus-5-5", effort="high")) == "high"
    assert not providers.rejects_forced_tool_choice(bedrock_config("claude-opus-5"))
    assert providers.anthropic_effort(bedrock_config("claude-haiku-4-5", effort="high")) is None


def test_native_output_is_gated_to_models_bedrock_accepts_it_for(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    # First party upgrades Opus 5.5 to native strict; Bedrock rejects
    # output_config.format for it, so tool output stays with tool_choice=auto.
    assert providers.prefers_native_output(LLMConfig(provider="anthropic", model="claude-opus-5-5"))
    opus = bedrock_config("claude-opus-5-5")
    assert not providers.prefers_native_output(opus)
    assert effective_output_mode("tool", opus) == "tool"
    profile = providers.resolved_model_profile(opus)
    assert profile["supports_json_schema_output"] is False
    assert profile["anthropic_supports_forced_tool_choice"] is False
    assert profile["anthropic_supports_effort"] is True
    haiku = providers.resolved_model_profile(bedrock_config("claude-haiku-4-5"))
    assert haiku["supports_json_schema_output"] is True
    monkeypatch.setenv("SIBYL_BEDROCK_API", "mantle")
    mantle = providers.resolved_model_profile(bedrock_config("claude-haiku-4-5"))
    assert mantle["supports_json_schema_output"] is False


def test_transport_policy_records_the_bedrock_api(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    policy = transport_policy(bedrock_config("claude-haiku-4-5"))
    assert policy["provider"] == "bedrock"
    assert policy["bedrock_api"] == "invoke"
    assert policy["receipt_version"] == "sibyl-sdk-attempt-v2"
    assert policy["max_retries"] == 2


# -----------------------------------------------------------------------------
# Wire behavior through the production factory
# -----------------------------------------------------------------------------


def claude_response(model: str, content: list[dict], stop_reason: str = "end_turn") -> dict:
    return {
        "id": "msg_bedrock_fixture",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 2},
    }


@asynccontextmanager
async def bedrock_extractor(monkeypatch, respond, *, model, output_mode="tool", effort=None):
    env = {
        "SIBYL_LLM_MEMORY_PROVIDER": "bedrock",
        "SIBYL_LLM_MEMORY_MODEL": model,
        "SIBYL_LLM_MEMORY_TRANSPORT_MAX_RETRIES": "0",
    }
    if effort:
        env["SIBYL_LLM_MEMORY_EFFORT"] = effort
    source = EnvConfigSource(env)
    monkeypatch.setattr(clients, "resolve_llm_config", source.resolve)
    monkeypatch.setattr(extraction, "reserve_llm_budget", AsyncMock())
    clients.invalidate_agent_cache()
    async with RecordingAnthropicClient(transport=httpx.MockTransport(respond)) as http:
        monkeypatch.setattr(providers, "RecordingAnthropicClient", lambda: http)
        try:
            yield Extractor(
                Verdict,
                surface=LLMSurface.MEMORY,
                output_mode=output_mode,
                max_tokens=512,
                output_retries=0,
            )
        finally:
            clients.invalidate_agent_cache()


async def test_opus_5_5_on_bedrock_signs_routes_and_keeps_effort(monkeypatch, static_aws):
    wires = []

    def respond(request):
        wires.append(request)
        body = json.loads(request.content)
        tool = body["tools"][0]["name"]
        return httpx.Response(
            200,
            headers={"x-amzn-requestid": "3f1c2a9e-aws-request"},
            json=claude_response(
                "claude-opus-5-5",
                [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": tool,
                        "input": {"ok": True, "summary": "s"},
                    }
                ],
                "tool_use",
            ),
        )

    async with bedrock_extractor(monkeypatch, respond, model="claude-opus-5-5") as extractor:
        assert await extractor.resolved_output_mode() == "tool"
        assert await extractor.resolved_effort() == "high"
        result = await extractor.extract_with_usage("Synthetic evidence")

    request = wires[0]
    assert str(request.url) == (
        "https://bedrock-runtime.us-west-2.amazonaws.com/model/us.anthropic.claude-opus-5-5/invoke"
    )
    authorization = request.headers["authorization"]
    assert authorization.startswith("AWS4-HMAC-SHA256")
    assert f"Credential={FIXTURE_KEY_ID}/" in authorization
    assert "/us-west-2/bedrock/aws4_request" in authorization
    assert request.headers["x-amz-security-token"] == "fixture-session"
    wire = json.loads(request.content)
    assert wire["anthropic_version"] == "bedrock-2023-05-31"
    assert "model" not in wire
    assert wire["output_config"] == {"effort": "high"}
    assert wire["tool_choice"] == {"type": "auto"}
    assert "strict" not in wire["tools"][0]
    assert "temperature" not in wire
    assert result.output == Verdict(ok=True, summary="s")
    assert result.usage.transport_attempts[0].request_id == "3f1c2a9e-aws-request"
    assert result.usage.transport_usage_complete is True
    # us. inference profiles bill at the in-Region rate, 10% over global.
    assert result.usage.cost_usd == pytest.approx(10 * 4.4e-6 + 2 * 22e-6)


async def test_haiku_on_bedrock_keeps_native_strict_output(monkeypatch, static_aws):
    monkeypatch.setenv("SIBYL_BEDROCK_INFERENCE_SCOPE", "global")
    wires = []

    def respond(request):
        wires.append(request)
        return httpx.Response(
            200,
            json=claude_response(
                "claude-haiku-4-5-20251001",
                [{"type": "text", "text": json.dumps({"ok": True, "summary": "s"})}],
            ),
        )

    async with bedrock_extractor(
        monkeypatch, respond, model="claude-haiku-4-5", output_mode="native_strict"
    ) as extractor:
        result = await extractor.extract_with_usage("Synthetic evidence")

    assert wires[0].url.path == "/model/global.anthropic.claude-haiku-4-5-20251001-v1:0/invoke"
    wire = json.loads(wires[0].content)
    assert wire["output_config"]["format"]["type"] == "json_schema"
    assert "tools" not in wire
    # global. profiles bill at the global rate.
    assert result.usage.cost_usd == pytest.approx(10 * 1e-6 + 2 * 5e-6)


async def test_native_strict_on_a_model_bedrock_rejects_fails_before_sending(
    monkeypatch, static_aws
):
    def respond(request):
        raise AssertionError("no request should be sent")

    async with bedrock_extractor(
        monkeypatch, respond, model="claude-opus-5-5", output_mode="native_strict"
    ) as extractor:
        with pytest.raises(LLMError) as caught:
            await extractor.extract_with_usage("Synthetic evidence")
    assert "does not support native strict extraction" in caught.value.details["cause"]


async def test_mantle_and_bearer_keys_use_their_own_endpoint_and_auth(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("SIBYL_BEDROCK_API", "mantle")
    monkeypatch.setenv("SIBYL_BEDROCK_API_KEY", "bedrock-api-key")
    wires = []

    def respond(request):
        wires.append(request)
        return httpx.Response(
            200,
            headers={"x-amzn-requestid": "req_mantle"},
            json=claude_response("claude-haiku-4-5-20251001", [{"type": "text", "text": "ok"}]),
        )

    async with RecordingAnthropicClient(transport=httpx.MockTransport(respond)) as http:
        monkeypatch.setattr(providers, "RecordingAnthropicClient", lambda: http)
        model = providers.build_model(bedrock_config("claude-haiku-4-5", max_tokens=8))
        from pydantic_ai import Agent

        await Agent(model).run("ping")

    request = wires[0]
    assert request.url.host == "bedrock-mantle.us-east-1.api.aws"
    assert request.url.path == "/anthropic/v1/messages"
    assert "anthropic-beta" not in request.headers
    assert json.loads(request.content)["model"] == "anthropic.claude-haiku-4-5"
    assert request.headers["authorization"] == "Bearer bedrock-api-key"


def test_agent_cache_key_changes_with_bedrock_settings(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    config = bedrock_config("claude-haiku-4-5")
    first = clients._config_fingerprint(config)
    monkeypatch.setenv("SIBYL_BEDROCK_INFERENCE_SCOPE", "global")
    assert clients._config_fingerprint(config) != first


# -----------------------------------------------------------------------------
# Probe
# -----------------------------------------------------------------------------


async def test_probe_without_a_region_reports_missing_credentials_without_a_call(monkeypatch):
    monkeypatch.setattr(validation, "build_model", pytest.fail)
    result = await validation.check_provider_key("bedrock", None)
    assert result.valid is False
    assert result.status == "missing_credentials"
    assert "SIBYL_BEDROCK_REGION" in (result.error or "")
    assert result.model == "claude-haiku-4-5"


async def test_probe_without_credentials_reports_missing_credentials(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    monkeypatch.setattr(validation, "build_model", pytest.fail)
    result = await validation.check_provider_key("bedrock", None)
    assert result.status == "missing_credentials"
    assert "No AWS credentials found" in (result.error or "")


async def test_surface_test_reports_missing_credentials_before_any_call(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    monkeypatch.setattr(validation, "build_model", pytest.fail)
    source = EnvConfigSource({"SIBYL_LLM_PROVIDER": "bedrock"})
    result = await validation.test_surface_config(LLMSurface.DEFAULT, source)
    assert result.valid is False
    assert result.status == "missing_credentials"


def test_sdk_credential_failures_map_to_missing_credentials():
    for message in (
        "could not resolve credentials from session",
        "Could not resolve AWS credentials from session",
    ):
        assert validation._status_for_exception(RuntimeError(message)) == "missing_credentials"


async def test_probe_with_credentials_makes_one_minimal_call(monkeypatch, static_aws):
    wires = []

    def respond(request):
        wires.append(request)
        return httpx.Response(
            200, json=claude_response("claude-haiku-4-5-20251001", [{"type": "text", "text": "ok"}])
        )

    async with RecordingAnthropicClient(transport=httpx.MockTransport(respond)) as http:
        monkeypatch.setattr(providers, "RecordingAnthropicClient", lambda: http)
        result = await validation.check_provider_key("bedrock", None)

    assert result.valid is True, result.error
    assert len(wires) == 1
    assert json.loads(wires[0].content)["max_tokens"] == validation.PROBE_MAX_TOKENS


def test_bedrock_validation_exception_for_a_bad_model_maps_to_model_not_found():
    for message in (
        "The provided model identifier is invalid.",
        "Invocation of model ID anthropic.claude-haiku-4-5-20251001-v1:0 with on-demand "
        "throughput isn't supported. Retry your request with the ID or ARN of an inference "
        "profile that contains this model.",
    ):
        error = ModelHTTPError(400, "us.anthropic.claude-nope", {"message": message})
        assert validation._status_for_exception(error) == "model_not_found"
    other = ModelHTTPError(400, "m", {"message": "output_config.format: Extra inputs"})
    assert validation._status_for_exception(other) == "network"


# -----------------------------------------------------------------------------
# Startup
# -----------------------------------------------------------------------------


@pytest.mark.parametrize("provider", ["bedrock", "gemini"])
def test_every_llm_provider_setting_imports_the_config(provider):
    env = {**os.environ, "SIBYL_LLM_PROVIDER": provider}
    completed = subprocess.run(
        [sys.executable, "-c", "import sibyl_core.config"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_bedrock_responses_price_by_the_routed_id(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("SIBYL_BEDROCK_API_KEY", "k")
    invoke = providers.build_model(bedrock_config("claude-haiku-4-5"))
    assert extraction._bedrock_price_ref(invoke) == "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    monkeypatch.setenv("SIBYL_BEDROCK_API", "mantle")
    mantle = providers.build_model(bedrock_config("claude-haiku-4-5"))
    assert mantle.model_name == "anthropic.claude-haiku-4-5"
    assert extraction._bedrock_price_ref(mantle) == "anthropic.claude-haiku-4-5-20251001-v1:0"
    first_party = providers.build_model(LLMConfig(provider="anthropic", model="claude-haiku-4-5"))
    assert extraction._bedrock_price_ref(first_party) is None
