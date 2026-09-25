"""Amazon Bedrock plumbing shared by the Claude and Cohere embedding paths.

Settings, model identifiers and credentials resolve here so the LLM provider
and the embedding provider always agree on region, profile and inference
scope. This module only needs the standard library at import time; boto3 and
botocore load lazily from the ``bedrock`` extra.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from anthropic import AsyncAnthropicBedrock, AsyncAnthropicBedrockMantle

BedrockApi = Literal["invoke", "mantle"]
#: A geographic cross-region profile prefix, ``global``, or ``regional`` for
#: the bare foundation-model ID. ``us`` keeps data in the US and Canada.
BedrockInferenceScope = Literal[
    "us", "eu", "apac", "jp", "au", "ca", "us-gov", "global", "regional"
]

BEDROCK_APIS: tuple[BedrockApi, ...] = ("invoke", "mantle")
BEDROCK_INFERENCE_SCOPES: tuple[BedrockInferenceScope, ...] = (
    "us",
    "eu",
    "apac",
    "jp",
    "au",
    "ca",
    "us-gov",
    "global",
    "regional",
)
DEFAULT_BEDROCK_API: BedrockApi = "invoke"
DEFAULT_BEDROCK_INFERENCE_SCOPE: BedrockInferenceScope = "us"
DEFAULT_BEDROCK_EMBEDDING_MODEL = "cohere.embed-v4:0"
#: The output sizes Cohere Embed v4 produces on Bedrock.
COHERE_EMBED_V4_DIMENSIONS = (256, 512, 1024, 1536)

REGION_ENV_VARS = ("SIBYL_BEDROCK_REGION", "AWS_REGION", "AWS_DEFAULT_REGION")
API_KEY_ENV_VARS = ("SIBYL_BEDROCK_API_KEY", "AWS_BEARER_TOKEN_BEDROCK")
#: The Anthropic SDK's Mantle client also reads this one, so Sibyl must too or
#: it would report SigV4 while the client sends a bearer token.
MANTLE_API_KEY_ENV_VARS = (*API_KEY_ENV_VARS, "ANTHROPIC_AWS_API_KEY")

#: Geographic prefixes a cross-region inference profile ID can carry.
BEDROCK_GEO_PREFIXES = ("us", "eu", "apac", "jp", "au", "ca", "global", "us-gov")
_VERSION_SUFFIX = re.compile(r"(.+)-v\d+(?::\d+)?$")


class BedrockConfigError(ValueError):
    """Raised when Bedrock settings or credentials cannot be resolved."""


@dataclass(frozen=True, slots=True)
class BedrockSettings:
    """Resolved Bedrock connection settings.

    ``api_key`` is a Bedrock API key sent as a bearer token in place of SigV4.
    Without one, requests sign with the default AWS credential chain, which
    covers IRSA web identity, EKS Pod Identity, SSO profiles and instance roles.
    ``mantle_api_key`` is the extra key only the SDK's Mantle client reads; it
    never signs the Cohere calls on bedrock-runtime.
    """

    region: str
    api: BedrockApi = DEFAULT_BEDROCK_API
    inference_scope: BedrockInferenceScope = DEFAULT_BEDROCK_INFERENCE_SCOPE
    profile: str | None = None
    api_key: str | None = field(default=None, repr=False)
    mantle_api_key: str | None = field(default=None, repr=False)

    @property
    def claude_api_key(self) -> str | None:
        """The bearer token the Claude client sends, if any."""
        return self.api_key or (self.mantle_api_key if self.api == "mantle" else None)

    @property
    def auth_mode(self) -> Literal["bearer", "sigv4"]:
        return "bearer" if self.claude_api_key else "sigv4"

    @property
    def fingerprint(self) -> str:
        secrets = [
            hashlib.sha256(key.encode()).hexdigest() if key else ""
            for key in (self.api_key, self.mantle_api_key)
        ]
        payload = "|".join(
            (self.region, self.api, self.inference_scope, self.profile or "", *secrets)
        )
        return hashlib.sha256(payload.encode()).hexdigest()


def resolve_bedrock_settings(environ: Mapping[str, str] | None = None) -> BedrockSettings:
    """Resolve Bedrock settings from the environment, failing with a fix-it message."""
    env = os.environ if environ is None else environ
    region = _first(env, REGION_ENV_VARS)
    if region is None:
        raise BedrockConfigError(
            "Amazon Bedrock needs a region: set SIBYL_BEDROCK_REGION or AWS_REGION"
        )
    api = (_first(env, ("SIBYL_BEDROCK_API",)) or DEFAULT_BEDROCK_API).lower()
    if api not in BEDROCK_APIS:
        raise BedrockConfigError(
            f"SIBYL_BEDROCK_API must be one of {', '.join(BEDROCK_APIS)}, not {api!r}"
        )
    scope = (
        _first(env, ("SIBYL_BEDROCK_INFERENCE_SCOPE",)) or DEFAULT_BEDROCK_INFERENCE_SCOPE
    ).lower()
    if scope not in BEDROCK_INFERENCE_SCOPES:
        raise BedrockConfigError(
            "SIBYL_BEDROCK_INFERENCE_SCOPE must be one of "
            f"{', '.join(BEDROCK_INFERENCE_SCOPES)}, not {scope!r}"
        )
    profile = _first(env, ("SIBYL_BEDROCK_PROFILE",))
    api_key = _first(env, API_KEY_ENV_VARS)
    # With a profile the Mantle client signs SigV4 and ignores this variable.
    mantle_api_key = (
        _first(env, ("ANTHROPIC_AWS_API_KEY",)) if api == "mantle" and not profile else None
    )
    if api_key and profile:
        # The Anthropic SDK refuses a bearer token beside explicit AWS
        # credentials, and silently preferring one would hide the other.
        raise BedrockConfigError(
            "Set a Bedrock API key (SIBYL_BEDROCK_API_KEY or AWS_BEARER_TOKEN_BEDROCK) "
            "or SIBYL_BEDROCK_PROFILE, not both"
        )
    return BedrockSettings(
        region=region,
        api=api,
        inference_scope=scope,
        profile=profile,
        api_key=api_key,
        mantle_api_key=mantle_api_key,
    )


def bedrock_region_configured(environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return _first(env, REGION_ENV_VARS) is not None


# -----------------------------------------------------------------------------
# Model identifiers
# -----------------------------------------------------------------------------


def remove_geo_prefix(model_id: str) -> str:
    """Strip a cross-region inference prefix: ``us.cohere.embed-v4:0`` -> ``cohere.embed-v4:0``."""
    for prefix in BEDROCK_GEO_PREFIXES:
        if model_id.startswith(f"{prefix}."):
            return model_id.removeprefix(f"{prefix}.")
    return model_id


def has_geo_prefix(model_id: str) -> bool:
    return remove_geo_prefix(model_id) != model_id


def is_arn(model_id: str) -> bool:
    """Application inference profiles and provisioned throughput go by ARN."""
    return model_id.startswith("arn:")


_MODEL_BEARING_ARN_RESOURCES = ("inference-profile/", "foundation-model/")


def arn_model_id(model_id: str) -> str | None:
    """The model ID an inference-profile or foundation-model ARN names.

    ``arn:aws:bedrock:us-west-2:123456789012:inference-profile/us.anthropic.claude-opus-5-5``
    names ``us.anthropic.claude-opus-5-5``. Application inference profile and
    provisioned throughput ARNs are opaque, so they give ``None``.
    """
    if not is_arn(model_id):
        return None
    resource = model_id.split(":", 5)[-1]
    for prefix in _MODEL_BEARING_ARN_RESOURCES:
        if resource.startswith(prefix):
            return resource.removeprefix(prefix) or None
    return None


def split_bedrock_model_id(model_id: str) -> tuple[str | None, str]:
    """Split a Bedrock ID into its vendor segment and bare model name.

    ``us.anthropic.claude-haiku-4-5-20251001-v1:0`` becomes
    ``("anthropic", "claude-haiku-4-5-20251001")``; an ID without a vendor
    segment comes back unchanged with ``None``. Mirrors pydantic-ai's private
    helper so model rules match the profile pydantic-ai resolves.
    """
    model_id = arn_model_id(model_id) or model_id
    vendor, _, name = remove_geo_prefix(model_id).partition(".")
    if not name:
        return None, model_id
    if match := _VERSION_SUFFIX.match(name):
        name = match.group(1)
    return vendor, name


def apply_inference_scope(base_model_id: str, scope: BedrockInferenceScope) -> str:
    """Route a foundation-model ID through the configured inference profile.

    ``us`` and ``global`` select cross-region inference profiles; ``regional``
    sends the foundation-model ID as is, which only works where the model
    offers in-Region on-demand throughput.
    """
    if scope == "regional":
        return base_model_id
    return f"{scope}.{base_model_id}"


def bedrock_embedding_model_id(model: str, settings: BedrockSettings) -> str:
    """The wire ID for an embedding model; an explicit profile ID or ARN wins."""
    if has_geo_prefix(model) or is_arn(model):
        return model
    return apply_inference_scope(model, settings.inference_scope)


# -----------------------------------------------------------------------------
# Credentials
# -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BedrockCredentialStatus:
    """Which credential source resolved, never the credential itself."""

    region: str
    auth_mode: Literal["bearer", "sigv4"]
    method: str
    profile: str | None = None


def require_botocore() -> Any:
    """boto3, or a fix-it error naming the extra that ships it."""
    try:
        import boto3
    except ImportError as exc:
        raise BedrockConfigError(
            "Amazon Bedrock needs boto3; install sibyl-core[bedrock] (sibyld ships it)"
        ) from exc
    return boto3


@lru_cache(maxsize=16)
def _boto_session(profile: str | None, region: str) -> Any:
    boto3 = require_botocore()
    try:
        return boto3.Session(profile_name=profile, region_name=region)
    except Exception as exc:
        raise BedrockConfigError(f"AWS profile {profile!r} could not be loaded: {exc}") from exc


def aws_credentials(settings: BedrockSettings) -> Any:
    """The default-chain credentials object, which refreshes itself when it expires.

    Resolution can touch the network (instance metadata, SSO), so async
    callers run it through :func:`resolve_bedrock_credentials`.
    """
    session = _boto_session(settings.profile, settings.region)
    try:
        credentials = session.get_credentials()
    except Exception as exc:
        raise BedrockConfigError(f"AWS credentials could not be resolved: {exc}") from exc
    if credentials is None:
        raise BedrockConfigError(
            "No AWS credentials found for Amazon Bedrock. Use IRSA or Pod Identity on "
            "Kubernetes, an instance role, AWS_PROFILE or SIBYL_BEDROCK_PROFILE, or set "
            "SIBYL_BEDROCK_API_KEY for a Bedrock API key"
        )
    return credentials


def frozen_aws_credentials(settings: BedrockSettings) -> Any:
    """A consistent access key, secret and token snapshot, refreshing if due."""
    credentials = aws_credentials(settings)
    try:
        return credentials.get_frozen_credentials()
    except Exception as exc:
        raise BedrockConfigError(f"AWS credentials could not be refreshed: {exc}") from exc


async def resolve_bedrock_credentials(settings: BedrockSettings) -> BedrockCredentialStatus:
    """Prove the Claude client's credentials resolve without a model request."""
    if settings.claude_api_key:
        return BedrockCredentialStatus(
            region=settings.region, auth_mode="bearer", method="bedrock-api-key"
        )

    def resolve() -> str:
        credentials = aws_credentials(settings)
        frozen_aws_credentials(settings)
        return str(getattr(credentials, "method", None) or "unknown")

    method = await asyncio.to_thread(resolve)
    return BedrockCredentialStatus(
        region=settings.region, auth_mode="sigv4", method=method, profile=settings.profile
    )


def sigv4_headers(
    settings: BedrockSettings,
    *,
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: bytes,
    service: str = "bedrock",
) -> dict[str, str]:
    """Sign one request the way the Anthropic SDK's Bedrock client does."""
    require_botocore()
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest

    signable = {key: value for key, value in headers.items() if key.lower() != "connection"}
    request = AWSRequest(method=method.upper(), url=url, headers=signable, data=body)
    SigV4Auth(frozen_aws_credentials(settings), service, settings.region).add_auth(request)
    prepared = request.prepare()
    return {key: value for key, value in dict(prepared.headers).items() if value is not None}


# -----------------------------------------------------------------------------
# Claude clients
# -----------------------------------------------------------------------------


def anthropic_bedrock_client(
    settings: BedrockSettings,
    *,
    http_client: Any,
    max_retries: int,
) -> AsyncAnthropicBedrock | AsyncAnthropicBedrockMantle:
    """The Anthropic SDK client for the configured Bedrock API.

    ``invoke`` is InvokeModel on bedrock-runtime, which takes the
    ``bedrock:InvokeModel`` IAM actions and cross-region inference profile
    IDs. ``mantle`` is the Anthropic Messages API on bedrock-mantle, which
    takes ``bedrock-mantle:CreateInference`` and in-Region model IDs.
    """
    from anthropic import AsyncAnthropicBedrock, AsyncAnthropicBedrockMantle

    require_botocore()
    credentials: dict[str, Any] = (
        {"api_key": settings.claude_api_key}
        if settings.claude_api_key
        else {"aws_profile": settings.profile}
    )
    if settings.api == "mantle":
        return AsyncAnthropicBedrockMantle(
            aws_region=settings.region,
            max_retries=max_retries,
            http_client=http_client,
            **credentials,
        )
    return AsyncAnthropicBedrock(
        aws_region=settings.region,
        max_retries=max_retries,
        http_client=http_client,
        **credentials,
    )


def _first(environ: Mapping[str, str], names: tuple[str, ...]) -> str | None:
    for name in names:
        value = environ.get(name, "").strip()
        if value:
            return value
    return None


__all__ = [
    "API_KEY_ENV_VARS",
    "BEDROCK_APIS",
    "BEDROCK_GEO_PREFIXES",
    "BEDROCK_INFERENCE_SCOPES",
    "COHERE_EMBED_V4_DIMENSIONS",
    "DEFAULT_BEDROCK_EMBEDDING_MODEL",
    "MANTLE_API_KEY_ENV_VARS",
    "REGION_ENV_VARS",
    "BedrockApi",
    "BedrockConfigError",
    "BedrockCredentialStatus",
    "BedrockInferenceScope",
    "BedrockSettings",
    "anthropic_bedrock_client",
    "apply_inference_scope",
    "arn_model_id",
    "aws_credentials",
    "bedrock_embedding_model_id",
    "bedrock_region_configured",
    "frozen_aws_credentials",
    "has_geo_prefix",
    "is_arn",
    "remove_geo_prefix",
    "require_botocore",
    "resolve_bedrock_credentials",
    "resolve_bedrock_settings",
    "sigv4_headers",
    "split_bedrock_model_id",
]
