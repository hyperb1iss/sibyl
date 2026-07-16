"""Language-model substrate."""

from importlib import import_module
from typing import Any

_EXPORTS = {
    "ConfigField": ("sibyl_core.ai.llm.config", "ConfigField"),
    "EnvConfigSource": ("sibyl_core.ai.llm.config", "EnvConfigSource"),
    "ExtractionResult": ("sibyl_core.ai.llm.extractor", "ExtractionResult"),
    "ExtractionUsage": ("sibyl_core.ai.llm.extractor", "ExtractionUsage"),
    "Extractor": ("sibyl_core.ai.llm.extractor", "Extractor"),
    "Generator": ("sibyl_core.ai.llm.generator", "Generator"),
    "LLMBudgetContext": ("sibyl_core.ai.llm.budget", "LLMBudgetContext"),
    "estimate_llm_tokens": ("sibyl_core.ai.llm.budget", "estimate_llm_tokens"),
    "get_budget_enforcer": ("sibyl_core.ai.llm.budget", "get_budget_enforcer"),
    "get_llm_budget_context": ("sibyl_core.ai.llm.budget", "get_llm_budget_context"),
    "llm_budget_context": ("sibyl_core.ai.llm.budget", "llm_budget_context"),
    "reserve_llm_budget": ("sibyl_core.ai.llm.budget", "reserve_llm_budget"),
    "set_budget_enforcer": ("sibyl_core.ai.llm.budget", "set_budget_enforcer"),
    "set_llm_budget_context": ("sibyl_core.ai.llm.budget", "set_llm_budget_context"),
    "LLMConfig": ("sibyl_core.ai.llm.config", "LLMConfig"),
    "LLMConfigSource": ("sibyl_core.ai.llm.config", "LLMConfigSource"),
    "LLMSurface": ("sibyl_core.ai.llm.config", "LLMSurface"),
    "ResolvedLLMConfig": ("sibyl_core.ai.llm.config", "ResolvedLLMConfig"),
    "get_config_source": ("sibyl_core.ai.llm.config", "get_config_source"),
    "invalidate_llm_config": ("sibyl_core.ai.llm.config", "invalidate_llm_config"),
    "resolve_llm_config": ("sibyl_core.ai.llm.config", "resolve_llm_config"),
    "set_config_source": ("sibyl_core.ai.llm.config", "set_config_source"),
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    try:
        module_name, attr_name = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(name) from exc
    module = import_module(module_name)
    value = getattr(module, attr_name)
    globals()[name] = value
    return value
