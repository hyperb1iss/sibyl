"""Settings and LLM configuration changed on one replica reach the others.

Replica A is the process that handles the admin's request; replica B is any
other API replica or worker. They share a settings table and one invalidation
channel, nothing else. Replica B runs the production handlers, installed with
``install_cache_invalidation_handlers`` over B's own settings service and LLM
config source.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from sibyl import cache_invalidation
from sibyl.ai.llm import service as llm_service
from sibyl.ai.llm.config_source import DBSettingsConfigSource
from sibyl.api.routes import settings as settings_routes
from sibyl.cache_invalidation import (
    announce_llm_runtime_invalidated,
    announce_runtime_settings_changed,
    install_cache_invalidation_handlers,
)
from sibyl.coordination.invalidation import CacheInvalidationBus
from sibyl.persistence.settings_types import SystemSettingRecord
from sibyl.services import settings as settings_module
from sibyl.services.settings import SettingsService
from sibyl_core.ai.llm import config as llm_config
from sibyl_core.ai.llm.config import LLMSurface
from tests.invalidation_channel import FakeChannel, FakeTransport


class SettingsTable:
    """The shared system_settings table, stored in plain text for the test."""

    def __init__(self) -> None:
        self.rows: dict[str, SystemSettingRecord] = {}
        self.reads = 0

    async def get(self, session: object, *, key: str) -> SystemSettingRecord | None:
        del session
        self.reads += 1
        return self.rows.get(key)

    async def save(self, session: object, *, setting: SystemSettingRecord) -> None:
        del session
        self.rows[setting.key] = setting

    async def delete(self, session: object, *, key: str) -> bool:
        del session
        return self.rows.pop(key, None) is not None


@asynccontextmanager
async def _no_session() -> AsyncIterator[None]:
    yield None


@dataclass
class Replicas:
    table: SettingsTable
    a_bus: CacheInvalidationBus
    a: SettingsService
    b: SettingsService


@pytest.fixture
def table(monkeypatch: pytest.MonkeyPatch) -> SettingsTable:
    table = SettingsTable()
    monkeypatch.setattr(settings_module, "get_system_setting", table.get)
    monkeypatch.setattr(settings_module, "save_system_setting", table.save)
    monkeypatch.setattr(settings_module, "delete_system_setting", table.delete)
    monkeypatch.setattr(settings_module, "encrypt_value", lambda value: f"enc:{value}")
    monkeypatch.setattr(settings_module, "decrypt_value", lambda value: value.removeprefix("enc:"))
    return table


@pytest.fixture(autouse=True)
def _restore_config_source() -> Iterator[None]:
    original = llm_config.get_config_source()
    yield
    llm_config.set_config_source(original)


async def _replicas(
    table: SettingsTable, monkeypatch: pytest.MonkeyPatch, *, linked: bool
) -> Replicas:
    channel = FakeChannel()
    a_bus, b_bus = CacheInvalidationBus(), CacheInvalidationBus()
    a, b = SettingsService(_no_session), SettingsService(_no_session)
    # Announcements made in this process go out on A's bus; the handlers that
    # run on B resolve B's settings service, as they would in B's process.
    monkeypatch.setattr(cache_invalidation, "get_cache_invalidation_bus", lambda: a_bus)
    monkeypatch.setattr(settings_module, "_settings_service", b)
    install_cache_invalidation_handlers(b_bus)
    if linked:
        await a_bus.attach(FakeTransport(channel))
        await b_bus.attach(FakeTransport(channel))
    return Replicas(table=table, a_bus=a_bus, a=a, b=b)


@pytest.mark.parametrize("linked", [False, True], ids=["no-channel", "channel"])
async def test_a_rotated_key_is_served_by_the_other_replica(
    table: SettingsTable, monkeypatch: pytest.MonkeyPatch, linked: bool
) -> None:
    replicas = await _replicas(table, monkeypatch, linked=linked)
    await replicas.a.set("anthropic_api_key", "sk-old")
    assert await replicas.b.get("anthropic_api_key") == "sk-old"

    await replicas.a.set("anthropic_api_key", "sk-rotated")

    served = await replicas.b.get("anthropic_api_key")
    # Without the channel, B serves its cached copy until the 60s TTL lapses.
    assert served == ("sk-rotated" if linked else "sk-old")


async def test_a_deleted_setting_is_forgotten_by_the_other_replica(
    table: SettingsTable, monkeypatch: pytest.MonkeyPatch
) -> None:
    replicas = await _replicas(table, monkeypatch, linked=True)
    monkeypatch.delenv("SIBYL_LLM_BUDGET_MONTHLY_ORG_TOKENS", raising=False)
    await replicas.a.set("llm.budget.monthly_org_tokens", "5000", is_secret=False)
    assert await replicas.b.get("llm.budget.monthly_org_tokens") == "5000"

    assert await replicas.a.delete("llm.budget.monthly_org_tokens") is True

    assert await replicas.b.get("llm.budget.monthly_org_tokens") is None


async def test_runtime_settings_saved_on_one_replica_reach_the_others_environment(
    table: SettingsTable, monkeypatch: pytest.MonkeyPatch
) -> None:
    replicas = await _replicas(table, monkeypatch, linked=True)
    reset = AsyncMock()
    monkeypatch.setattr(settings_module, "reset_settings_dependent_runtimes", reset)
    # B started with the old key and provider in its environment.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-old")
    monkeypatch.setenv("SIBYL_GRAPH_EMBEDDING_PROVIDER", "openai")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-old")
    monkeypatch.setenv("GOOGLE_API_KEY", "gemini-old")

    await replicas.a.set("openai_api_key", "sk-new")
    await replicas.a.set("graph_embedding_provider", "gemini", is_secret=False)
    # The admin removed the Gemini key on A, which popped it from A's environment.
    table.rows.pop("gemini_api_key", None)
    await announce_runtime_settings_changed(
        ["openai_api_key", "graph_embedding_provider", "gemini_api_key", "not_a_runtime_key"]
    )

    assert os.environ["OPENAI_API_KEY"] == "sk-new"
    assert os.environ["SIBYL_GRAPH_EMBEDDING_PROVIDER"] == "gemini"
    assert "GEMINI_API_KEY" not in os.environ
    assert "GOOGLE_API_KEY" not in os.environ
    reset.assert_awaited_once()


async def test_settings_routes_announce_what_they_applied_locally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from starlette.requests import Request

    announced = AsyncMock()
    monkeypatch.setattr(settings_routes, "announce_runtime_settings_changed", announced)
    monkeypatch.setattr(settings_routes, "require_settings_owner", AsyncMock())
    monkeypatch.setattr(settings_routes, "_try_reset_graph_client", AsyncMock())
    monkeypatch.setattr(settings_routes, "is_setup_mode", AsyncMock(return_value=False))
    service = AsyncMock()
    service.delete.return_value = True
    monkeypatch.setattr(settings_routes, "get_settings_service", lambda: service)
    # Registers the variable for restoration; the route writes and pops it.
    monkeypatch.setenv("SIBYL_GRAPH_EMBEDDING_MODEL", "text-embedding-3-small")
    request = Request({"type": "http", "method": "PATCH", "path": "/settings", "headers": []})

    await settings_routes.update_settings(
        request,
        body=settings_routes.UpdateSettingsRequest(graph_embedding_model="text-embedding-3-large"),
    )
    await settings_routes.delete_setting(request, key="graph_embedding_model")

    assert [call.args for call in announced.await_args_list] == [
        (["graph_embedding_model"],),
        (["graph_embedding_model"],),
    ]


class _SettingsValues:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values

    async def get_llm_setting(self, surface: str, field: str) -> str | None:
        return self.values.get(f"llm.{surface}.{field}")

    async def get_database_value(self, key: str, *, decrypt: bool = True) -> str | None:
        del decrypt
        return self.values.get(key)


async def test_llm_config_changed_on_one_replica_is_re_resolved_on_the_other(
    table: SettingsTable, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _replicas(table, monkeypatch, linked=True)
    stored = {"llm.crawler.model": "claude-haiku-5-5", "anthropic_api_key": "sk-ant"}
    b_source = DBSettingsConfigSource(_SettingsValues(stored), environ={})  # type: ignore[arg-type]
    llm_config.set_config_source(b_source)
    assert (await b_source.resolve(LLMSurface.CRAWLER)).model.value == "claude-haiku-5-5"

    stored["llm.crawler.model"] = "claude-sonnet-5-5"
    assert (await b_source.resolve(LLMSurface.CRAWLER)).model.value == "claude-haiku-5-5"
    await announce_llm_runtime_invalidated(LLMSurface.CRAWLER.value)

    assert (await b_source.resolve(LLMSurface.CRAWLER)).model.value == "claude-sonnet-5-5"


async def test_invalidate_llm_runtime_announces_after_its_local_invalidation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    async def local(surface: LLMSurface | None) -> None:
        events.append(f"local:{surface.value if surface else None}")

    async def announce(surface: str | None) -> None:
        events.append(f"announce:{surface}")

    monkeypatch.setattr(llm_service, "invalidate_local_llm_runtime", local)
    monkeypatch.setattr(llm_service, "announce_llm_runtime_invalidated", announce)

    await llm_service.invalidate_llm_runtime(LLMSurface.CRAWLER)
    await llm_service.invalidate_llm_runtime()

    assert events == ["local:crawler", "announce:crawler", "local:None", "announce:None"]


async def test_resubscribing_clears_settings_and_llm_caches(
    table: SettingsTable, monkeypatch: pytest.MonkeyPatch
) -> None:
    bus = CacheInvalidationBus()
    service = SettingsService(_no_session)
    monkeypatch.setattr(settings_module, "_settings_service", service)
    stored = {"llm.default.model": f"model-{uuid4().hex[:6]}"}
    source = DBSettingsConfigSource(_SettingsValues(stored), environ={})  # type: ignore[arg-type]
    llm_config.set_config_source(source)
    install_cache_invalidation_handlers(bus)
    table.rows["embedding_model"] = SystemSettingRecord(
        key="embedding_model", value="old", is_secret=False
    )
    await service.get("embedding_model")
    await source.resolve(LLMSurface.DEFAULT)
    table.rows["embedding_model"] = SystemSettingRecord(
        key="embedding_model", value="new", is_secret=False
    )
    stored["llm.default.model"] = "model-new"

    await bus.reset()

    assert await service.get("embedding_model") == "new"
    assert (await source.resolve(LLMSurface.DEFAULT)).model.value == "model-new"
