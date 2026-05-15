"""LLM configuration resolution."""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Literal, Protocol, cast

from pydantic import BaseModel, Field, SecretStr

from sibyl_core.ai.errors import LLMConfigError
from sibyl_core.ai.registry import ProviderName

LLMProviderName = Literal["anthropic", "gemini", "openai"]

class LLMSurface(StrEnum):
    DEFAULT = "default"
    CRAWLER = "crawler"
    SYNTHESIS = "synthesis"


class LLMConfig(BaseModel):
    provider: LLMProviderName
    model: str
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, gt=0)
    timeout_seconds: float = Field(default=60.0, gt=0.0)
    api_key: SecretStr | None = None


class ConfigField[T](BaseModel):
    value: T
    source: Literal["env", "db", "default"]
    locked_by_env: bool = False
    env_var: str | None = None


class ResolvedLLMConfig(BaseModel):
    surface: LLMSurface
    provider: ConfigField[LLMProviderName]
    model: ConfigField[str]
    temperature: ConfigField[float]
    max_tokens: ConfigField[int | None]
    timeout_seconds: ConfigField[float]
    api_key: ConfigField[SecretStr | None]
    cached_at: datetime | None = None

    def to_llm_config(self) -> LLMConfig:
        return LLMConfig(
            provider=self.provider.value,
            model=self.model.value,
            temperature=self.temperature.value,
            max_tokens=self.max_tokens.value,
            timeout_seconds=self.timeout_seconds.value,
            api_key=self.api_key.value,
        )


class LLMConfigSource(Protocol):
    async def resolve(self, surface: LLMSurface) -> ResolvedLLMConfig: ...

    async def invalidate(self, surface: LLMSurface | None = None) -> None: ...


class EnvConfigSource:
    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        self._environ = environ if environ is not None else os.environ

    async def resolve(self, surface: LLMSurface) -> ResolvedLLMConfig:
        provider = self._resolve_provider(surface)
        return ResolvedLLMConfig(
            surface=surface,
            provider=provider,
            model=self._resolve_string(
                surface,
                "MODEL",
                default="claude-haiku-4-5",
            ),
            temperature=self._resolve_float(surface, "TEMPERATURE", default=0.0),
            max_tokens=self._resolve_int(surface, "MAX_TOKENS", default=None),
            timeout_seconds=self._resolve_float(surface, "TIMEOUT_SECONDS", default=60.0),
            api_key=self._resolve_api_key(provider.value),
        )

    async def invalidate(self, surface: LLMSurface | None = None) -> None:
        return None

    def _resolve_provider(self, surface: LLMSurface) -> ConfigField[LLMProviderName]:
        field = self._resolve_string(surface, "PROVIDER", default="anthropic")
        if field.value not in {"anthropic", "gemini", "openai"}:
            raise LLMConfigError(
                f"Unsupported LLM provider: {field.value}",
                provider=field.value,
                surface=surface.value,
            )
        return ConfigField[LLMProviderName](
            value=cast(LLMProviderName, field.value),
            source=field.source,
            locked_by_env=field.locked_by_env,
            env_var=field.env_var,
        )

    def _resolve_string(self, surface: LLMSurface, field: str, *, default: str) -> ConfigField[str]:
        resolved = self._lookup_env(_surface_env_names(surface, field))
        if resolved is not None:
            env_var, value = resolved
            return ConfigField(value=value, source="env", locked_by_env=True, env_var=env_var)

        resolved = self._lookup_env((f"SIBYL_LLM_{field}",))
        if resolved is not None:
            env_var, value = resolved
            return ConfigField(value=value, source="env", locked_by_env=True, env_var=env_var)

        return ConfigField(value=default, source="default")

    def _resolve_float(self, surface: LLMSurface, field: str, *, default: float) -> ConfigField[float]:
        resolved = self._resolve_optional_env(surface, field)
        if resolved is None:
            return ConfigField(value=default, source="default")
        env_var, raw_value = resolved
        try:
            value = float(raw_value)
        except ValueError as exc:
            raise LLMConfigError(
                f"Invalid float for {env_var}: {raw_value}",
                surface=surface.value,
            ) from exc
        return ConfigField(value=value, source="env", locked_by_env=True, env_var=env_var)

    def _resolve_int(
        self,
        surface: LLMSurface,
        field: str,
        *,
        default: int | None,
    ) -> ConfigField[int | None]:
        resolved = self._resolve_optional_env(surface, field)
        if resolved is None:
            return ConfigField(value=default, source="default")
        env_var, raw_value = resolved
        try:
            value = int(raw_value)
        except ValueError as exc:
            raise LLMConfigError(
                f"Invalid integer for {env_var}: {raw_value}",
                surface=surface.value,
            ) from exc
        return ConfigField(value=value, source="env", locked_by_env=True, env_var=env_var)

    def _resolve_optional_env(self, surface: LLMSurface, field: str) -> tuple[str, str] | None:
        resolved = self._lookup_env(_surface_env_names(surface, field))
        if resolved is not None:
            return resolved
        resolved = self._lookup_env((f"SIBYL_LLM_{field}",))
        if resolved is not None:
            return resolved
        return None

    def _resolve_api_key(self, provider: LLMProviderName) -> ConfigField[SecretStr | None]:
        resolved = self._lookup_env(_api_key_env_names(provider))
        if resolved is None:
            return ConfigField(value=None, source="default")
        env_var, value = resolved
        return ConfigField(
            value=SecretStr(value),
            source="env",
            locked_by_env=True,
            env_var=env_var,
        )

    def _lookup_env(self, names: tuple[str, ...]) -> tuple[str, str] | None:
        for name in names:
            value = self._environ.get(name, "").strip()
            if value:
                return name, value
        return None


_config_source: LLMConfigSource = EnvConfigSource()


def set_config_source(source: LLMConfigSource) -> None:
    global _config_source
    _config_source = source


def get_config_source() -> LLMConfigSource:
    return _config_source


async def resolve_llm_config(surface: LLMSurface = LLMSurface.DEFAULT) -> ResolvedLLMConfig:
    return await _config_source.resolve(surface)


async def invalidate_llm_config(surface: LLMSurface | None = None) -> None:
    await _config_source.invalidate(surface)


def _surface_env_names(surface: LLMSurface, field: str) -> tuple[str, ...]:
    return (f"SIBYL_LLM_{surface.value.upper()}_{field}",)


def _api_key_env_names(provider: ProviderName) -> tuple[str, ...]:
    return {
        "anthropic": ("SIBYL_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"),
        "gemini": ("SIBYL_GEMINI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"),
        "openai": ("SIBYL_OPENAI_API_KEY", "OPENAI_API_KEY"),
    }.get(provider, ())
