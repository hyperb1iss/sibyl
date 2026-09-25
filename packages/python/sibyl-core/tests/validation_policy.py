"""Validation factory fakes that freeze an input budget the way the real factory does."""

from sibyl_core.ai.llm.config import (
    LLMConfig,
    LLMSurface,
    consolidation_input_budget,
    resolve_llm_config,
)
from sibyl_core.tasks._evidence_json import canonical


def offline_policy(**fields: object) -> str:
    """An offline model has no default of its own: 40,000, or the explicit setting."""
    budget = consolidation_input_budget(LLMConfig(provider="anthropic", model="offline"))
    return canonical({"model": "offline", "max_input_chars": budget, **fields})


def memory_model_factory(extractor):
    """Freeze the active memory model per call, so a model can change between stages."""

    async def factory(*_args, **_kwargs):
        config = (await resolve_llm_config(LLMSurface.MEMORY)).to_llm_config()
        budget = consolidation_input_budget(config)
        return extractor, canonical({"model": config.model, "max_input_chars": budget})

    return factory
