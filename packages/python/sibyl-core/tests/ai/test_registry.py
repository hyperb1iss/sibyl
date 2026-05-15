from __future__ import annotations

import pytest

from sibyl_core.ai.registry import ModelCapability, ModelKind, ModelRegistry, model_registry


def test_registry_has_initial_llm_entries() -> None:
    entries = model_registry.llm_entries()

    assert [entry.alias for entry in entries] == [
        "claude-haiku-4-5",
        "claude-sonnet-4-6",
        "gemini-3-flash",
        "gemini-3-1-flash-lite",
        "gpt-5.4-mini",
        "gpt-5.4-nano",
    ]
    assert model_registry.embedding_entries() == []


def test_registry_lookup_by_alias_and_snapshot() -> None:
    alias_entry = model_registry.require("claude-haiku-4-5", kind=ModelKind.LLM)
    snapshot_entry = model_registry.require("claude-haiku-4-5-20251001", kind=ModelKind.LLM)

    assert alias_entry == snapshot_entry
    assert alias_entry.provider == "anthropic"
    assert alias_entry.provider_model_id == "claude-haiku-4-5-20251001"
    assert ModelCapability.STRUCTURED_OUTPUT in alias_entry.capabilities


def test_registry_filters_kind() -> None:
    assert model_registry.get("claude-haiku-4-5", kind=ModelKind.EMBEDDING) is None


def test_registry_recommendation() -> None:
    entry = model_registry.recommended_for("default", kind=ModelKind.LLM)

    assert entry.alias == "claude-haiku-4-5"


def test_registry_custom_entry_is_marked_unverified() -> None:
    entry = model_registry.custom("openai", "gpt-experimental", kind=ModelKind.LLM)

    assert entry.alias == "gpt-experimental"
    assert entry.provider_model_id == "gpt-experimental"
    assert entry.capabilities == frozenset()
    assert entry.warning == "unverified_model"
    assert entry.is_custom is True


def test_registry_require_raises_for_unknown_model() -> None:
    with pytest.raises(KeyError, match="Unknown llm model"):
        ModelRegistry().require("missing-model", kind=ModelKind.LLM)
