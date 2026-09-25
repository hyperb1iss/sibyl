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
BedrockInferenceScope = Literal["us", "global", "regional"]

BEDROCK_APIS: tuple[BedrockApi, ...] = ("invoke", "mantle")
BEDROCK_INFERENCE_SCOPES: tuple[BedrockInferenceScope, ...] = ("us", "global", "regional")
DEFAULT_BEDROCK_API: BedrockApi = "invoke"
DEFAULT_BEDROCK_INFERENCE_SCOPE: BedrockInferenceScope = "us"
DEFAULT_BEDROCK_EMBEDDING_MODEL = "cohere.embed-v4:0"

REGION_ENV_VARS = ("SIBYL_BEDROCK_REGION", "AWS_REGION", "AWS_DEFAULT_REGION")
API_KEY_ENV_VARS = ("SIBYL_BEDROCK_API_KEY", "AWS_BEARER_TOKEN_BEDROCK")

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
    """

    region: str
    api: BedrockApi = DEFAULT_BEDROCK_API
    inference_scope: BedrockInferenceScope = DEFAULT_BEDROCK_INFERENCE_SCOPE
    profile: str | None = None
    api_key: str | None = field(default=None, repr=False)

    @property
    def auth_mode(self) -> Literal["bearer", "sigv4"]:
        return "bearer" if self.api_key else "sigv4"

    @property
    def fingerprint(self) -> str:
        secret = hashlib.sha256(self.api_key.encode()).hexdigest() if self.api_key else ""
        payload = "|".join(
            (self.region, self.api, self.inference_scope, self.profile or "", secret)
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


def split_bedrock_model_id(model_id: str) -> tuple[str | None, str]:
    """Split a Bedrock ID into its vendor segment and bare model name.

    ``us.anthropic.claude-haiku-4-5-20251001-v1:0`` becomes
    ``("anthropic", "claude-haiku-4-5-20251001")``; an ID without a vendor
    segment comes back unchanged with ``None``. Mirrors pydantic-ai's private
    helper so model rules match the profile pydantic-ai resolves.
    """
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
    """The wire ID for an embedding model; an explicit profile ID wins."""
    if has_geo_prefix(model):
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


def _require_botocore() -> Any:
    try:
        import boto3
    except ImportError as exc:
        raise BedrockConfigError(
            "Amazon Bedrock needs boto3; install sibyl-core[bedrock] (sibyld ships it)"
        ) from exc
    return boto3


@lru_cache(maxsize=16)
def _boto_session(profile: str | None, region: str) -> Any:
    boto3 = _require_botocore()
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
    """Prove credentials resolve for this region without sending a model request."""
    if settings.api_key:
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

    _require_botocore()
    credentials: dict[str, Any] = (
        {"api_key": settings.api_key} if settings.api_key else {"aws_profile": settings.profile}
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
    "DEFAULT_BEDROCK_EMBEDDING_MODEL",
    "REGION_ENV_VARS",
    "BedrockApi",
    "BedrockConfigError",
    "BedrockCredentialStatus",
    "BedrockInferenceScope",
    "BedrockSettings",
    "anthropic_bedrock_client",
    "apply_inference_scope",
    "aws_credentials",
    "bedrock_embedding_model_id",
    "bedrock_region_configured",
    "frozen_aws_credentials",
    "has_geo_prefix",
    "remove_geo_prefix",
    "resolve_bedrock_credentials",
    "resolve_bedrock_settings",
    "sigv4_headers",
    "split_bedrock_model_id",
]
