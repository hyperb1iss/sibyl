"""Runtime glue for installing the DB-backed LLM config source."""

from __future__ import annotations

from sibyl.ai.llm.budget import DBLLMBudgetEnforcer
from sibyl.ai.llm.config_source import DBSettingsConfigSource
from sibyl.services.settings import SettingsService, get_settings_service
from sibyl_core.ai.clients import invalidate_agent_cache
from sibyl_core.ai.llm.budget import set_budget_enforcer
from sibyl_core.ai.llm.config import LLMSurface, get_config_source, set_config_source


def install_db_config_source(
    settings_service: SettingsService | None = None,
) -> DBSettingsConfigSource:
    settings = settings_service or get_settings_service()
    source = DBSettingsConfigSource(settings)
    set_config_source(source)
    set_budget_enforcer(DBLLMBudgetEnforcer(settings))
    invalidate_agent_cache()
    return source


async def invalidate_llm_runtime(surface: LLMSurface | None = None) -> None:
    await get_config_source().invalidate(surface)
    invalidate_agent_cache(surface)
