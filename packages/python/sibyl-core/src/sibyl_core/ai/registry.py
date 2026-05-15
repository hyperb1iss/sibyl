"""Curated AI model registry."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel

ProviderName = Literal["anthropic", "gemini", "openai", "cohere", "voyageai", "bedrock"]


class ModelKind(StrEnum):
    LLM = "llm"
    EMBEDDING = "embedding"


class ModelCapability(StrEnum):
    STRUCTURED_OUTPUT = "structured_output"
    STREAMING = "streaming"
    TOOL_USE = "tool_use"
    THINKING = "thinking"


class ModelEntry(BaseModel):
    alias: str
    snapshot: str
    kind: ModelKind
    provider: ProviderName
    provider_model_id: str
    pydantic_ai_model_class: str
    use_cases: tuple[str, ...] = ()
    capabilities: frozenset[ModelCapability] = frozenset()
    max_output_tokens: int | None = None
    embedding_dimensions: int | None = None
    default_temperature: float | None = None
    input_cost_per_mtok_usd: float
    output_cost_per_mtok_usd: float | None = None
    cost_source_url: str
    last_verified_at: datetime
    deprecated_after: datetime | None = None
    warning: str | None = None

    @property
    def is_custom(self) -> bool:
        return self.warning == "unverified_model"


_VERIFIED_AT = datetime(2026, 5, 15, tzinfo=UTC)

_ANTHROPIC_COST_SOURCE = "https://platform.claude.com/docs/en/about-claude/models/overview"
_GOOGLE_COST_SOURCE = "https://ai.google.dev/gemini-api/docs/pricing"
_OPENAI_COST_SOURCE = "https://developers.openai.com/api/docs/pricing"


class ModelRegistry:
    """Registry of operator-selectable models."""

    def __init__(self, entries: list[ModelEntry] | None = None) -> None:
        self._entries = tuple(entries or _DEFAULT_ENTRIES)
        self._by_alias = {entry.alias: entry for entry in self._entries}
        self._by_snapshot = {entry.snapshot: entry for entry in self._entries}

    def entries(self, *, kind: ModelKind | None = None) -> list[ModelEntry]:
        if kind is None:
            return list(self._entries)
        return [entry for entry in self._entries if entry.kind is kind]

    def llm_entries(self) -> list[ModelEntry]:
        return self.entries(kind=ModelKind.LLM)

    def embedding_entries(self) -> list[ModelEntry]:
        return self.entries(kind=ModelKind.EMBEDDING)

    def get(self, model: str, *, kind: ModelKind | None = None) -> ModelEntry | None:
        entry = self._by_alias.get(model) or self._by_snapshot.get(model)
        if entry is None:
            return None
        if kind is not None and entry.kind is not kind:
            return None
        return entry

    def require(self, model: str, *, kind: ModelKind | None = None) -> ModelEntry:
        entry = self.get(model, kind=kind)
        if entry is None:
            expected = f" {kind.value}" if kind else ""
            raise KeyError(f"Unknown{expected} model: {model}")
        return entry

    def recommended_for(self, use_case: str, *, kind: ModelKind = ModelKind.LLM) -> ModelEntry:
        for entry in self.entries(kind=kind):
            if use_case in entry.use_cases:
                return entry
        raise KeyError(f"No {kind.value} model recommendation for use case: {use_case}")

    def custom(self, provider: ProviderName, model: str, *, kind: ModelKind) -> ModelEntry:
        return ModelEntry(
            alias=model,
            snapshot=model,
            kind=kind,
            provider=provider,
            provider_model_id=model,
            pydantic_ai_model_class=_default_model_class(provider, kind),
            use_cases=(),
            capabilities=frozenset(),
            max_output_tokens=None,
            embedding_dimensions=None,
            default_temperature=None,
            input_cost_per_mtok_usd=0.0,
            output_cost_per_mtok_usd=None,
            cost_source_url="",
            last_verified_at=_VERIFIED_AT,
            warning="unverified_model",
        )


def _default_model_class(provider: ProviderName, kind: ModelKind) -> str:
    if kind is ModelKind.EMBEDDING:
        return "Embedder"
    return {
        "anthropic": "AnthropicModel",
        "gemini": "GoogleModel",
        "openai": "OpenAIResponsesModel",
    }.get(provider, "UnknownModel")


_DEFAULT_ENTRIES = [
    ModelEntry(
        alias="claude-haiku-4-5",
        snapshot="claude-haiku-4-5-20251001",
        kind=ModelKind.LLM,
        provider="anthropic",
        provider_model_id="claude-haiku-4-5-20251001",
        pydantic_ai_model_class="AnthropicModel",
        use_cases=("extraction", "default"),
        capabilities=frozenset(
            {ModelCapability.STRUCTURED_OUTPUT, ModelCapability.STREAMING, ModelCapability.THINKING}
        ),
        max_output_tokens=8192,
        default_temperature=0.0,
        input_cost_per_mtok_usd=1.0,
        output_cost_per_mtok_usd=5.0,
        cost_source_url=_ANTHROPIC_COST_SOURCE,
        last_verified_at=_VERIFIED_AT,
    ),
    ModelEntry(
        alias="claude-sonnet-4-6",
        snapshot="claude-sonnet-4-6",
        kind=ModelKind.LLM,
        provider="anthropic",
        provider_model_id="claude-sonnet-4-6",
        pydantic_ai_model_class="AnthropicModel",
        use_cases=("synthesis", "quality"),
        capabilities=frozenset(
            {
                ModelCapability.STRUCTURED_OUTPUT,
                ModelCapability.STREAMING,
                ModelCapability.TOOL_USE,
                ModelCapability.THINKING,
            }
        ),
        max_output_tokens=8192,
        default_temperature=0.2,
        input_cost_per_mtok_usd=3.0,
        output_cost_per_mtok_usd=15.0,
        cost_source_url=_ANTHROPIC_COST_SOURCE,
        last_verified_at=_VERIFIED_AT,
    ),
    ModelEntry(
        alias="gemini-3-flash",
        snapshot="gemini-3-flash-preview",
        kind=ModelKind.LLM,
        provider="gemini",
        provider_model_id="gemini-3-flash-preview",
        pydantic_ai_model_class="GoogleModel",
        use_cases=("cost-optimized-extraction", "extraction"),
        capabilities=frozenset(
            {ModelCapability.STRUCTURED_OUTPUT, ModelCapability.STREAMING, ModelCapability.THINKING}
        ),
        max_output_tokens=65536,
        default_temperature=0.0,
        input_cost_per_mtok_usd=0.5,
        output_cost_per_mtok_usd=3.0,
        cost_source_url=_GOOGLE_COST_SOURCE,
        last_verified_at=_VERIFIED_AT,
    ),
    ModelEntry(
        alias="gemini-3-1-flash-lite",
        snapshot="gemini-3.1-flash-lite",
        kind=ModelKind.LLM,
        provider="gemini",
        provider_model_id="gemini-3.1-flash-lite",
        pydantic_ai_model_class="GoogleModel",
        use_cases=("bulk", "bulk-crawling"),
        capabilities=frozenset(
            {ModelCapability.STRUCTURED_OUTPUT, ModelCapability.STREAMING, ModelCapability.THINKING}
        ),
        max_output_tokens=65536,
        default_temperature=0.0,
        input_cost_per_mtok_usd=0.25,
        output_cost_per_mtok_usd=1.5,
        cost_source_url=_GOOGLE_COST_SOURCE,
        last_verified_at=_VERIFIED_AT,
    ),
    ModelEntry(
        alias="gpt-5.4-mini",
        snapshot="gpt-5.4-mini",
        kind=ModelKind.LLM,
        provider="openai",
        provider_model_id="gpt-5.4-mini",
        pydantic_ai_model_class="OpenAIResponsesModel",
        use_cases=("openai-parity", "parity"),
        capabilities=frozenset(
            {
                ModelCapability.STRUCTURED_OUTPUT,
                ModelCapability.STREAMING,
                ModelCapability.TOOL_USE,
                ModelCapability.THINKING,
            }
        ),
        max_output_tokens=128000,
        default_temperature=0.0,
        input_cost_per_mtok_usd=0.75,
        output_cost_per_mtok_usd=4.5,
        cost_source_url=_OPENAI_COST_SOURCE,
        last_verified_at=_VERIFIED_AT,
    ),
    ModelEntry(
        alias="gpt-5.4-nano",
        snapshot="gpt-5.4-nano",
        kind=ModelKind.LLM,
        provider="openai",
        provider_model_id="gpt-5.4-nano",
        pydantic_ai_model_class="OpenAIResponsesModel",
        use_cases=("budget", "budget-extraction"),
        capabilities=frozenset(
            {
                ModelCapability.STRUCTURED_OUTPUT,
                ModelCapability.STREAMING,
                ModelCapability.TOOL_USE,
                ModelCapability.THINKING,
            }
        ),
        max_output_tokens=128000,
        default_temperature=0.0,
        input_cost_per_mtok_usd=0.2,
        output_cost_per_mtok_usd=1.25,
        cost_source_url=_OPENAI_COST_SOURCE,
        last_verified_at=_VERIFIED_AT,
    ),
]

model_registry = ModelRegistry()


def llm_entries() -> list[ModelEntry]:
    return model_registry.llm_entries()


def embedding_entries() -> list[ModelEntry]:
    return model_registry.embedding_entries()


def recommended_for(use_case: str, kind: ModelKind = ModelKind.LLM) -> ModelEntry:
    return model_registry.recommended_for(use_case, kind=kind)
