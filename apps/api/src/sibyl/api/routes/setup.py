"""Setup wizard endpoints.

Public endpoints for detecting fresh installs and guiding first-time setup.
Status endpoint is always public. Other endpoints require authentication
once initial setup is complete.

Config update endpoints are admin-only after initial setup.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import replace

import structlog
from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from sibyl.config import settings
from sibyl.persistence.operations_runtime import (
    get_setup_status as get_runtime_setup_status,
    require_setup_mode_or_admin,
)
from sibyl.services.settings import SettingsService, get_settings_service
from sibyl_core.ai.bedrock import (
    API_KEY_ENV_VARS as BEDROCK_API_KEY_ENV_VARS,
    bedrock_region_configured,
    resolve_bedrock_credentials,
    resolve_bedrock_settings,
)
from sibyl_core.ai.errors import LLMConfigError
from sibyl_core.ai.llm.config import (
    LLMProviderName,
    LLMSurface,
    get_config_source,
    resolve_llm_config,
)
from sibyl_core.ai.validation import KeyValidationResult, check_provider_key
from sibyl_core.embeddings.providers import sentence_transformers_available
from sibyl_core.integration import agent_setup_markdown, install_commands, setup_command

router = APIRouter(prefix="/setup", tags=["setup"])
log = structlog.get_logger()


class SetupStatus(BaseModel):
    """Current setup state of the Sibyl instance."""

    needs_setup: bool = Field(description="True until setup has initialized an owner/admin org")
    sso_first_run: bool = Field(
        default=False,
        description="True when identity is SSO-only and no owner/admin exists "
        "yet: the first provider login completes setup, so clients should "
        "route to the provider instead of the local setup wizard",
    )
    has_users: bool = Field(description="True if at least one user exists")
    has_orgs: bool = Field(description="True if at least one org exists")
    setup_complete: bool = Field(
        description="True if an owner/admin organization has been initialized"
    )
    public_signups_enabled: bool = Field(
        description="True when post-setup self-serve account creation is enabled"
    )
    openai_configured: bool = Field(description="True if OpenAI API key is set")
    anthropic_configured: bool = Field(description="True if Anthropic API key is set")
    gemini_configured: bool = Field(description="True if Gemini API key is set")
    openai_valid: bool | None = Field(
        default=None, description="True if OpenAI key works (only checked if configured)"
    )
    anthropic_valid: bool | None = Field(
        default=None, description="True if Anthropic key works (only checked if configured)"
    )
    gemini_valid: bool | None = Field(
        default=None, description="True if Gemini key works (only checked if configured)"
    )
    bedrock_configured: bool = Field(
        default=False,
        description="True when an AWS region and an AWS credential source are present",
    )
    bedrock_llm: bool = Field(
        default=False,
        description="True when Bedrock is configured and the default LLM surface uses it",
    )
    bedrock_embeddings: bool = Field(
        default=False,
        description="True when Bedrock is configured and document embeddings use it",
    )
    bedrock_valid: bool | None = Field(
        default=None, description="True if Bedrock answers (only checked by validate-keys)"
    )
    providers_configured: bool = Field(
        default=False,
        description="True when every model provider this server uses is ready without "
        "user input: its key is set, or it needs none (cloud IAM or local models)",
    )
    configured_providers: list[str] = Field(
        default_factory=list, description="Names of the ready model providers"
    )


# Providers that authenticate with a key someone pastes in. Any other provider
# (cloud IAM such as Bedrock, or local embeddings) is ready once it is selected.
_PROVIDER_KEY_SETTINGS = {
    "anthropic": "anthropic_api_key",
    "gemini": "gemini_api_key",
    "openai": "openai_api_key",
}


async def _setting_or_default(service: SettingsService, key: str, default: str) -> str:
    value = await service.get(key)
    return value.strip() if isinstance(value, str) and value.strip() else default


async def _server_model_providers() -> tuple[bool, list[str]]:
    """Report whether the server's chosen model providers need nothing from users."""
    service = get_settings_service()
    try:
        # Each surface (memory, synthesis, crawler) can name its own provider.
        llm_providers = {
            (await resolve_llm_config(surface)).provider.value for surface in LLMSurface
        }
    except LLMConfigError:
        return False, []
    providers = {
        *llm_providers,
        await _setting_or_default(service, "embedding_provider", settings.embedding_provider),
        await _setting_or_default(
            service, "graph_embedding_provider", settings.graph_embedding_provider
        ),
    }
    ready: list[str] = []
    for provider in sorted(providers):
        key_setting = _PROVIDER_KEY_SETTINGS.get(provider)
        if key_setting is None or bool(await service.get(key_setting)):
            ready.append(provider)
    return len(ready) == len(providers), ready


class ApiKeyValidation(BaseModel):
    """Result of validating API keys."""

    openai_valid: bool = Field(description="True if OpenAI API key works")
    anthropic_valid: bool = Field(description="True if Anthropic API key works")
    gemini_valid: bool = Field(description="True if Gemini API key works")
    openai_error: str | None = Field(default=None, description="Error message if OpenAI fails")
    anthropic_error: str | None = Field(
        default=None, description="Error message if Anthropic fails"
    )
    gemini_error: str | None = Field(default=None, description="Error message if Gemini fails")
    bedrock_valid: bool | None = Field(
        default=None, description="True if Bedrock answers; None when nothing uses Bedrock"
    )
    bedrock_error: str | None = Field(default=None, description="Error message if Bedrock fails")
    bedrock_llm: bool = Field(default=False, description="The default LLM surface uses Bedrock")
    bedrock_embeddings: bool = Field(default=False, description="Document embeddings use Bedrock")


async def _check_openai_key(key: str | None = None) -> tuple[bool, str | None]:
    """Validate OpenAI API key through the native LLM substrate.

    Args:
        key: API key to validate. If None, fetches from SettingsService.
    """
    if key is None:
        service = get_settings_service()
        key = await service.get_openai_key()

    if not key:
        return False, "No API key configured"

    return await _check_provider_key("openai", key)


async def _check_anthropic_key(key: str | None = None) -> tuple[bool, str | None]:
    """Validate Anthropic API key through the native LLM substrate.

    Args:
        key: API key to validate. If None, fetches from SettingsService.
    """
    if key is None:
        service = get_settings_service()
        key = await service.get_anthropic_key()

    if not key:
        return False, "No API key configured"

    return await _check_provider_key("anthropic", key)


async def _check_gemini_key(key: str | None = None) -> tuple[bool, str | None]:
    """Validate Gemini API key through the native LLM substrate.

    Args:
        key: API key to validate. If None, fetches from SettingsService.
    """
    if key is None:
        service = get_settings_service()
        key = await service.get_gemini_key()

    if not key:
        return False, "No API key configured"

    return await _check_provider_key("gemini", key)


async def bedrock_selection() -> tuple[bool, bool]:
    """Whether Bedrock serves the default LLM surface and the embedding planes.

    A plane counts only when its provider is set to ``bedrock`` and a Region
    is known. An AWS environment alone proves nothing: a laptop with a
    profile, or any IRSA pod, has one while every provider still points at a
    keyed API. Credentials are not required here because an instance role
    leaves no environment hint; validate-keys proves them with a real call.
    """
    if not bedrock_region_configured():
        return False, False
    try:
        resolved = await get_config_source().resolve(LLMSurface.DEFAULT)
        llm = resolved.provider.value == "bedrock"
    except Exception as e:
        log.warning("Could not resolve the default LLM provider", error=str(e))
        llm = False
    document = await effective_embedding_setting("embedding_provider")
    graph = await effective_embedding_setting("graph_embedding_provider")
    # The graph plane needs no key on bedrock, or on local when its optional
    # sentence-transformers dependency is installed (sibyld does not ship it).
    graph_covered = graph == "bedrock" or (graph == "local" and sentence_transformers_available())
    return llm, document == "bedrock" and graph_covered


async def effective_embedding_setting(key: str) -> str | None:
    """An embedding setting as the runtime reads it: environment, database, default.

    Saving a setting writes the environment too, so the environment is what
    the embedding providers see.
    """
    env_value = os.environ.get(f"SIBYL_{key.upper()}", "").strip()
    if env_value:
        return env_value
    stored = await get_settings_service().get(key)
    if stored:
        return str(stored)
    value = getattr(settings, key, None)
    return None if value is None else str(value)


async def _check_bedrock() -> tuple[bool | None, str | None]:
    """Probe Bedrock through the AWS credential chain, only when something uses it.

    The Claude probe proves the Claude client's credentials. On Mantle those
    can be a key only that client reads, so embeddings routed to Bedrock also
    prove the credentials their own requests sign with.
    """
    llm, embeddings = await bedrock_selection()
    if not (llm or embeddings):
        return None, None
    try:
        result = await check_provider_key("bedrock", None)
        if result.valid and embeddings:
            bedrock = resolve_bedrock_settings()
            await resolve_bedrock_credentials(replace(bedrock, mantle_api_key=None))
    except Exception as e:
        log.warning("Bedrock validation failed", error=str(e))
        return False, str(e)
    return result.valid, _validation_error(result)


def bedrock_configured(environ: Mapping[str, str] | None = None) -> bool:
    """A region plus any AWS credential source; proving it works is validate-keys' job."""
    env = os.environ if environ is None else environ
    if not bedrock_region_configured(env):
        return False
    return any(env.get(name, "").strip() for name in _AWS_CREDENTIAL_HINTS)


#: Environment variables that point the AWS credential chain at a source: a
#: Bedrock API key, static keys, a profile, IRSA web identity, EKS Pod Identity
#: or an ECS task role. Instance roles leave no hint, so validate-keys probes.
_AWS_CREDENTIAL_HINTS = (
    *BEDROCK_API_KEY_ENV_VARS,
    "SIBYL_BEDROCK_PROFILE",
    "AWS_PROFILE",
    "AWS_ACCESS_KEY_ID",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
)


async def _check_provider_key(
    provider: LLMProviderName,
    key: str | None,
) -> tuple[bool, str | None]:
    if not key:
        return False, "No API key configured"

    try:
        result = await check_provider_key(provider, key)
    except Exception as e:
        log.warning("Provider key validation failed", provider=provider, error=str(e))
        return False, str(e)
    return result.valid, _validation_error(result)


def _validation_error(result: KeyValidationResult) -> str | None:
    if result.valid:
        return None
    return result.error or result.status


@router.get("/status", response_model=SetupStatus)
async def get_setup_status(
    validate_keys: bool = False,  # noqa: ARG001 - retained for API compatibility
) -> SetupStatus:
    """Check if this Sibyl instance needs initial setup.

    Returns the current setup state including:
    - Whether setup is complete
    - Whether API keys are configured

    This endpoint requires no authentication since it must work before
    setup completes, so it never performs provider key validation: doing
    so would let unauthenticated callers burn provider quota and incur
    cost using the server-stored OpenAI, Anthropic, and Gemini keys. Use
    the authenticated /setup/validate-keys route for full key validation.
    The validate_keys parameter is retained only for backward
    compatibility and is ignored.
    """
    setup_status = await get_runtime_setup_status()

    # Check if API keys are configured (non-empty)
    service = get_settings_service()
    openai_key = await service.get_openai_key()
    anthropic_key = await service.get_anthropic_key()
    gemini_key = await service.get_gemini_key()
    openai_configured = bool(openai_key)
    anthropic_configured = bool(anthropic_key)
    gemini_configured = bool(gemini_key)
    bedrock_llm, bedrock_embeddings = await bedrock_selection()
    providers_configured, configured_providers = await _server_model_providers()

    return SetupStatus(
        needs_setup=not setup_status.setup_complete,
        sso_first_run=(not setup_status.setup_complete) and settings.sso_only_instance,
        has_users=setup_status.has_users,
        has_orgs=setup_status.has_orgs,
        setup_complete=setup_status.setup_complete,
        public_signups_enabled=settings.public_signups_enabled,
        openai_configured=openai_configured,
        anthropic_configured=anthropic_configured,
        gemini_configured=gemini_configured,
        openai_valid=None,
        anthropic_valid=None,
        gemini_valid=None,
        bedrock_configured=bedrock_configured(),
        bedrock_llm=bedrock_llm,
        bedrock_embeddings=bedrock_embeddings,
        providers_configured=providers_configured,
        configured_providers=configured_providers,
    )


@router.get(
    "/validate-keys",
    response_model=ApiKeyValidation,
    dependencies=[Depends(require_setup_mode_or_admin)],
)
async def validate_api_keys() -> ApiKeyValidation:
    """Validate that configured API keys work.

    Makes test requests to OpenAI and Anthropic APIs to verify
    the configured keys are valid and have appropriate permissions.

    During initial setup: accessible without auth.
    After setup: requires owner/admin authentication.
    """
    openai_valid, openai_error = await _check_openai_key()
    anthropic_valid, anthropic_error = await _check_anthropic_key()
    gemini_valid, gemini_error = await _check_gemini_key()
    bedrock_valid, bedrock_error = await _check_bedrock()
    bedrock_llm, bedrock_embeddings = await bedrock_selection()

    return ApiKeyValidation(
        openai_valid=openai_valid,
        anthropic_valid=anthropic_valid,
        gemini_valid=gemini_valid,
        openai_error=openai_error,
        anthropic_error=anthropic_error,
        gemini_error=gemini_error,
        bedrock_valid=bedrock_valid,
        bedrock_error=bedrock_error,
        bedrock_llm=bedrock_llm,
        bedrock_embeddings=bedrock_embeddings,
    )


class ConnectInfo(BaseModel):
    """What a machine needs to connect to this server. Public and secret-free."""

    server_url: str = Field(description="Public base URL clients connect to")
    server_version: str = Field(description="Version this server runs")
    minimum_client_version: str | None = Field(
        default=None, description="Oldest CLI this server accepts, when a floor is set"
    )
    sso_enabled: bool = Field(description="True when sign-in goes through OIDC SSO")
    local_auth_enabled: bool = Field(description="True when email and password sign-in works")
    setup_command: str = Field(description="The command that connects a machine")
    agent_url: str = Field(description="Public URL of the agent setup document")
    install: dict[str, str] = Field(
        description="One copyable install-and-setup line per OS: macos, linux, windows"
    )


def _server_url() -> str:
    return settings.server_url.rstrip("/")


def _agent_url(server_url: str) -> str:
    """The short `/agent` page when the web app shares the API's origin, else the API route.

    `/agent` is served by the web app, so it only resolves on the server URL when
    one ingress fronts both; a split deployment points at the API document.
    """
    frontend = settings.frontend_url.rstrip("/")
    if frontend == server_url:
        return f"{server_url}/agent"
    return f"{server_url}/api/setup/agent.md"


def _minimum_client_version() -> str | None:
    return (settings.minimum_client_version or "").strip() or None


@router.get("/connect", response_model=ConnectInfo)
async def get_connect_info() -> ConnectInfo:
    """Describe how a machine connects to this server.

    Public so the connect card and the agent setup document work before and
    after sign-in. Everything returned is already visible to anyone who can
    reach the server: its URL, version, version floor, and sign-in methods.
    """
    from sibyl import __version__

    server_url = _server_url()
    return ConnectInfo(
        server_url=server_url,
        server_version=__version__,
        minimum_client_version=_minimum_client_version(),
        sso_enabled=bool(settings.oidc.providers),
        local_auth_enabled=settings.local_auth_enabled,
        setup_command=setup_command(server_url),
        agent_url=_agent_url(server_url),
        install=install_commands(server_url),
    )


@router.get("/agent.md", response_class=PlainTextResponse)
async def get_agent_setup() -> PlainTextResponse:
    """Markdown an AI coding agent follows to connect this machine.

    Public by design: a person pastes the URL into an agent that has no
    session yet. It carries only the public connect facts, never credentials.
    """
    return PlainTextResponse(
        agent_setup_markdown(
            _server_url(),
            minimum_client_version=_minimum_client_version(),
            sso=bool(settings.oidc.providers),
        ),
        media_type="text/markdown; charset=utf-8",
    )


class ConfigUpdateRequest(BaseModel):
    """Request to update server configuration."""

    openai_api_key: str | None = Field(
        default=None, description="OpenAI API key (leave empty to keep existing)"
    )
    anthropic_api_key: str | None = Field(
        default=None, description="Anthropic API key (leave empty to keep existing)"
    )
    gemini_api_key: str | None = Field(
        default=None, description="Gemini API key (leave empty to keep existing)"
    )


class ConfigUpdateResponse(BaseModel):
    """Response after updating server configuration."""

    success: bool = Field(description="True if config was updated")
    openai_configured: bool = Field(description="True if OpenAI key is now configured")
    anthropic_configured: bool = Field(description="True if Anthropic key is now configured")
    gemini_configured: bool = Field(description="True if Gemini key is now configured")
    openai_valid: bool | None = Field(
        default=None, description="True if OpenAI key works (validated if provided)"
    )
    anthropic_valid: bool | None = Field(
        default=None, description="True if Anthropic key works (validated if provided)"
    )
    gemini_valid: bool | None = Field(
        default=None, description="True if Gemini key works (validated if provided)"
    )
    openai_error: str | None = Field(default=None, description="Error if OpenAI validation failed")
    anthropic_error: str | None = Field(
        default=None, description="Error if Anthropic validation failed"
    )
    gemini_error: str | None = Field(default=None, description="Error if Gemini validation failed")


@router.post("/config", response_model=ConfigUpdateResponse)
async def update_config(
    body: ConfigUpdateRequest,
    _admin: object | None = Depends(require_setup_mode_or_admin),
) -> ConfigUpdateResponse:
    """Update server configuration (API keys).

    During initial setup: accessible without auth.
    After setup: requires owner/admin authentication.

    Keys are validated before being saved. If validation fails, the key
    is still saved but the response indicates the error.
    """
    service = get_settings_service()

    openai_valid: bool | None = None
    anthropic_valid: bool | None = None
    gemini_valid: bool | None = None
    openai_error: str | None = None
    anthropic_error: str | None = None
    gemini_error: str | None = None

    # Update OpenAI key if provided
    if body.openai_api_key is not None:
        openai_valid, openai_error = await _check_openai_key(body.openai_api_key)
        await service.set(
            "openai_api_key",
            body.openai_api_key,
            is_secret=True,
            description="OpenAI API key for embeddings and entity extraction",
        )
        log.info("OpenAI API key updated", valid=openai_valid)

    # Update Anthropic key if provided
    if body.anthropic_api_key is not None:
        anthropic_valid, anthropic_error = await _check_anthropic_key(body.anthropic_api_key)
        await service.set(
            "anthropic_api_key",
            body.anthropic_api_key,
            is_secret=True,
            description="Anthropic API key for Claude-powered extraction workflows",
        )
        log.info("Anthropic API key updated", valid=anthropic_valid)

    if body.gemini_api_key is not None:
        gemini_valid, gemini_error = await _check_gemini_key(body.gemini_api_key)
        await service.set(
            "gemini_api_key",
            body.gemini_api_key,
            is_secret=True,
            description="Gemini API key for Google embeddings",
        )
        log.info("Gemini API key updated", valid=gemini_valid)

    # Get current config state
    openai_key = await service.get_openai_key()
    anthropic_key = await service.get_anthropic_key()
    gemini_key = await service.get_gemini_key()

    return ConfigUpdateResponse(
        success=True,
        openai_configured=bool(openai_key),
        anthropic_configured=bool(anthropic_key),
        gemini_configured=bool(gemini_key),
        openai_valid=openai_valid,
        anthropic_valid=anthropic_valid,
        gemini_valid=gemini_valid,
        openai_error=openai_error,
        anthropic_error=anthropic_error,
        gemini_error=gemini_error,
    )


class ConfigStatusResponse(BaseModel):
    """Current server configuration status."""

    openai_configured: bool = Field(description="True if OpenAI key is configured")
    anthropic_configured: bool = Field(description="True if Anthropic key is configured")
    gemini_configured: bool = Field(description="True if Gemini key is configured")
    openai_source: str = Field(description="Source of OpenAI key: database, environment, or none")
    anthropic_source: str = Field(
        description="Source of Anthropic key: database, environment, or none"
    )
    gemini_source: str = Field(description="Source of Gemini key: database, environment, or none")


@router.get("/config", response_model=ConfigStatusResponse)
async def get_config_status(
    _admin: object | None = Depends(require_setup_mode_or_admin),
) -> ConfigStatusResponse:
    """Get current server configuration status.

    During initial setup: accessible without auth.
    After setup: requires owner/admin authentication.

    Returns whether each API key is configured and its source (database or environment).
    Does not return the actual key values for security.
    """
    service = get_settings_service()

    openai_value, openai_source = await service.get_with_source("openai_api_key")
    anthropic_value, anthropic_source = await service.get_with_source("anthropic_api_key")
    gemini_value, gemini_source = await service.get_with_source("gemini_api_key")

    return ConfigStatusResponse(
        openai_configured=bool(openai_value),
        anthropic_configured=bool(anthropic_value),
        gemini_configured=bool(gemini_value),
        openai_source=openai_source,
        anthropic_source=anthropic_source,
        gemini_source=gemini_source,
    )
