"""Settings and LLM configuration changed on one replica reach the others.

Replica A is the process that handles the admin's request; replica B is any
other API replica or worker. They share a settings table and one invalidation
channel, nothing else. Replica B runs the production handlers, installed with
``install_cache_invalidation_handlers`` over B's own settings service and LLM
config source. Announcing only queues a message and each topic has its own
worker, so tests ``settle`` both buses before asserting what B did.

Runtime settings (provider keys, embedding provider, model and size) follow one
rule at startup, after a change on any replica, and after a resubscription:
a setting whose variables the deployment set keeps the deployment's value,
and every other one mirrors the stored value. The tests below pin each path
to that rule, so no two processes given the same deployment can disagree.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from sibyl import cache_invalidation
from sibyl.ai.llm import service as llm_service
from sibyl.ai.llm.config_source import DBSettingsConfigSource
from sibyl.api.routes import settings as settings_routes
from sibyl.cache_invalidation import (
    RUNTIME_SETTINGS_TOPIC,
    announce_llm_runtime_invalidated,
    announce_runtime_settings_changed,
    install_cache_invalidation_handlers,
)
from sibyl.coordination.invalidation import CacheInvalidationBus
from sibyl.persistence.settings_types import SystemSettingRecord
from sibyl.services import settings as settings_module
from sibyl.services.settings import (
    RUNTIME_SETTING_ENV_VARS,
    RUNTIME_SETTING_LOCK_ENV_VARS,
    SettingsService,
    sync_runtime_settings,
)
from sibyl_core.ai.llm import config as llm_config
from sibyl_core.ai.llm.config import LLMSurface
from tests.invalidation_channel import FakeChannel, FakeTransport

_RUNTIME_ENV_NAMES = sorted(
    {name for names in RUNTIME_SETTING_LOCK_ENV_VARS.values() for name in names}
    | {name for names in RUNTIME_SETTING_ENV_VARS.values() for name in names}
)


class SettingsTable:
    """The shared system_settings table, stored in plain text for the test."""

    def __init__(self) -> None:
        self.rows: dict[str, SystemSettingRecord] = {}
        self.failing_key: str | None = None

    async def get(self, session: object, *, key: str) -> SystemSettingRecord | None:
        del session
        if key == self.failing_key:
            raise ConnectionError(f"read of {key} failed")
        return self.rows.get(key)

    async def save(self, session: object, *, setting: SystemSettingRecord) -> None:
        del session
        self.rows[setting.key] = setting

    async def delete(self, session: object, *, key: str) -> bool:
        del session
        return self.rows.pop(key, None) is not None

    def store(self, key: str, value: str) -> None:
        self.rows[key] = SystemSettingRecord(key=key, value=value, is_secret=False)


@asynccontextmanager
async def _no_session() -> AsyncIterator[None]:
    yield None


@dataclass
class Replicas:
    table: SettingsTable
    a_bus: CacheInvalidationBus
    b_bus: CacheInvalidationBus
    a: SettingsService
    b: SettingsService

    async def settle(self) -> None:
        await self.a_bus.flush()
        await self.b_bus.drain()


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
def runtime_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, str]]:
    """Start every test from an empty deployment environment and restore the real one.

    Tests write the deployment's variables into the returned dict and
    ``os.environ`` through ``deploy``.
    """
    saved = {name: os.environ.get(name) for name in _RUNTIME_ENV_NAMES}
    for name in _RUNTIME_ENV_NAMES:
        os.environ.pop(name, None)
    deployment: dict[str, str] = {}
    monkeypatch.setattr(settings_module, "_deployment_environment", deployment)
    yield deployment
    for name, value in saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


def deploy(deployment: dict[str, str], **variables: str) -> None:
    """Variables the deployment set before the process started."""
    deployment.update(variables)
    os.environ.update(variables)


@pytest.fixture
def rebuilds(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    rebuild = AsyncMock()
    monkeypatch.setattr(settings_module, "reset_settings_dependent_runtimes", rebuild)
    return rebuild


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
    return Replicas(table=table, a_bus=a_bus, b_bus=b_bus, a=a, b=b)


@pytest.mark.parametrize("linked", [False, True], ids=["no-channel", "channel"])
async def test_a_rotated_key_is_served_by_the_other_replica(
    table: SettingsTable, monkeypatch: pytest.MonkeyPatch, linked: bool
) -> None:
    replicas = await _replicas(table, monkeypatch, linked=linked)
    await replicas.a.set("anthropic_api_key", "sk-old")
    await replicas.settle()
    assert await replicas.b.get("anthropic_api_key") == "sk-old"

    await replicas.a.set("anthropic_api_key", "sk-rotated")
    await replicas.settle()

    served = await replicas.b.get("anthropic_api_key")
    # Without the channel, B serves its cached copy until the 60s TTL lapses.
    assert served == ("sk-rotated" if linked else "sk-old")


async def test_a_deleted_setting_is_forgotten_by_the_other_replica(
    table: SettingsTable, monkeypatch: pytest.MonkeyPatch
) -> None:
    replicas = await _replicas(table, monkeypatch, linked=True)
    monkeypatch.delenv("SIBYL_LLM_BUDGET_MONTHLY_ORG_TOKENS", raising=False)
    await replicas.a.set("llm.budget.monthly_org_tokens", "5000", is_secret=False)
    await replicas.settle()
    assert await replicas.b.get("llm.budget.monthly_org_tokens") == "5000"

    assert await replicas.a.delete("llm.budget.monthly_org_tokens") is True
    await replicas.settle()

    assert await replicas.b.get("llm.budget.monthly_org_tokens") is None


async def test_runtime_settings_saved_on_one_replica_reach_the_others_environment(
    table: SettingsTable, monkeypatch: pytest.MonkeyPatch, rebuilds: AsyncMock
) -> None:
    replicas = await _replicas(table, monkeypatch, linked=True)
    table.store("openai_api_key", "sk-old")
    table.store("graph_embedding_provider", "openai")
    table.store("gemini_api_key", "gemini-old")
    await sync_runtime_settings(rebuild=False, reason="B starts")
    assert os.environ["OPENAI_API_KEY"] == "sk-old"
    assert os.environ["GOOGLE_API_KEY"] == "gemini-old"

    await replicas.a.set("openai_api_key", "sk-new")
    await replicas.a.set("graph_embedding_provider", "gemini", is_secret=False)
    await replicas.a.delete("gemini_api_key")
    await announce_runtime_settings_changed(
        ["openai_api_key", "graph_embedding_provider", "gemini_api_key"]
    )
    await replicas.settle()

    assert os.environ["OPENAI_API_KEY"] == "sk-new"
    assert os.environ["SIBYL_GRAPH_EMBEDDING_PROVIDER"] == "gemini"
    assert "GEMINI_API_KEY" not in os.environ
    assert "GOOGLE_API_KEY" not in os.environ
    rebuilds.assert_awaited_once()


async def test_a_failed_read_leaves_the_environment_untouched(
    table: SettingsTable, monkeypatch: pytest.MonkeyPatch, rebuilds: AsyncMock
) -> None:
    monkeypatch.setattr(settings_module, "_settings_service", SettingsService(_no_session))
    table.store("embedding_provider", "openai")
    table.store("embedding_model", "text-embedding-3-small")
    await sync_runtime_settings(rebuild=False, reason="start")

    table.store("embedding_provider", "gemini")
    table.store("embedding_model", "gemini-embedding-001")
    table.failing_key = "embedding_dimensions"
    with pytest.raises(ConnectionError):
        await sync_runtime_settings(reason="change")

    # Nothing half-applied: the provider did not move without its model.
    assert os.environ["SIBYL_EMBEDDING_PROVIDER"] == "openai"
    assert os.environ["SIBYL_EMBEDDING_MODEL"] == "text-embedding-3-small"
    rebuilds.assert_not_awaited()


async def test_a_swap_that_stops_partway_still_rebuilds(
    table: SettingsTable, monkeypatch: pytest.MonkeyPatch, rebuilds: AsyncMock
) -> None:
    monkeypatch.setattr(settings_module, "_settings_service", SettingsService(_no_session))
    table.store("embedding_provider", "gemini")
    table.store("embedding_model", "gemini-embedding-001")

    class _Environ(dict[str, str]):
        def __setitem__(self, key: str, value: str) -> None:
            if key == "SIBYL_EMBEDDING_MODEL":
                raise OSError("environment write failed")
            super().__setitem__(key, value)

    environ = _Environ(os.environ)
    monkeypatch.setattr(settings_module, "os", SimpleNamespace(environ=environ))
    with pytest.raises(OSError, match="environment write failed"):
        await sync_runtime_settings(reason="change")

    rebuilds.assert_awaited_once()


async def test_a_replica_that_missed_the_change_converges_when_it_resubscribes(
    table: SettingsTable, monkeypatch: pytest.MonkeyPatch, rebuilds: AsyncMock
) -> None:
    replicas = await _replicas(table, monkeypatch, linked=False)
    table.store("graph_embedding_model", "text-embedding-3-small")
    await sync_runtime_settings(rebuild=False, reason="B starts")

    # The change lands while B is cut off: its announcement never arrives.
    await replicas.a.set("graph_embedding_model", "text-embedding-3-large", is_secret=False)
    await announce_runtime_settings_changed(["graph_embedding_model"])
    await replicas.settle()
    assert os.environ["SIBYL_GRAPH_EMBEDDING_MODEL"] == "text-embedding-3-small"

    await replicas.b_bus.reset()
    await replicas.b_bus.drain()

    assert os.environ["SIBYL_GRAPH_EMBEDDING_MODEL"] == "text-embedding-3-large"
    rebuilds.assert_awaited_once()


async def test_startup_and_live_apply_resolve_settings_the_same_way(
    table: SettingsTable, monkeypatch: pytest.MonkeyPatch, rebuilds: AsyncMock
) -> None:
    """A replica that applied a change live and one started afterwards agree."""
    replicas = await _replicas(table, monkeypatch, linked=True)
    table.store("embedding_provider", "openai")
    table.store("embedding_model", "text-embedding-3-small")
    await settings_module.load_runtime_settings_from_db()
    assert os.environ["SIBYL_EMBEDDING_MODEL"] == "text-embedding-3-small"

    await replicas.a.set("embedding_model", "text-embedding-3-large", is_secret=False)
    await announce_runtime_settings_changed(["embedding_model"])
    await replicas.settle()
    live = {name: os.environ.get(name) for name in _RUNTIME_ENV_NAMES}

    for name in _RUNTIME_ENV_NAMES:
        os.environ.pop(name, None)
    await settings_module.load_runtime_settings_from_db()
    started = {name: os.environ.get(name) for name in _RUNTIME_ENV_NAMES}

    assert started == live
    assert started["SIBYL_EMBEDDING_MODEL"] == "text-embedding-3-large"


async def test_a_setting_the_deployment_pins_keeps_its_value_everywhere(
    table: SettingsTable,
    monkeypatch: pytest.MonkeyPatch,
    runtime_env: dict[str, str],
    rebuilds: AsyncMock,
) -> None:
    monkeypatch.setattr(settings_module, "_settings_service", SettingsService(_no_session))
    deploy(runtime_env, SIBYL_GRAPH_EMBEDDING_PROVIDER="openai", SIBYL_GEMINI_API_KEY="gem-env")
    # Stored before the deployment pinned it, or through some other path.
    table.store("graph_embedding_provider", "gemini")
    table.store("gemini_api_key", "gem-stored")

    at_startup = await sync_runtime_settings(rebuild=False, reason="startup")
    assert os.environ["SIBYL_GRAPH_EMBEDDING_PROVIDER"] == "openai"
    # The deployment's key fills the aliases it left unset; the stored one is set aside.
    assert os.environ["GEMINI_API_KEY"] == "gem-env"
    assert os.environ["GOOGLE_API_KEY"] == "gem-env"
    assert set(at_startup.overridden_keys) == {"graph_embedding_provider", "gemini_api_key"}

    table.store("graph_embedding_provider", "local")
    live = await sync_runtime_settings(reason="announced")
    assert os.environ["SIBYL_GRAPH_EMBEDDING_PROVIDER"] == "openai"
    assert live.changed_env_vars == ()
    rebuilds.assert_not_awaited()


def _patch_request() -> Request:
    return Request({"type": "http", "method": "PATCH", "path": "/settings", "headers": []})


@pytest.mark.parametrize(
    ("field", "value", "variable"),
    [
        ("graph_embedding_provider", "gemini", "SIBYL_GRAPH_EMBEDDING_PROVIDER"),
        ("embedding_dimensions", 768, "SIBYL_EMBEDDING_DIMENSIONS"),
        ("openai_api_key", "sk-ui", "SIBYL_OPENAI_API_KEY"),
    ],
)
async def test_the_settings_route_refuses_a_setting_the_deployment_pins(
    monkeypatch: pytest.MonkeyPatch,
    runtime_env: dict[str, str],
    field: str,
    value: object,
    variable: str,
) -> None:
    deploy(runtime_env, **{variable: "from-deployment"})
    monkeypatch.setattr(settings_routes, "require_settings_owner", AsyncMock())
    service = AsyncMock()
    monkeypatch.setattr(settings_routes, "get_settings_service", lambda: service)
    applied = AsyncMock()
    monkeypatch.setattr(settings_routes, "sync_runtime_settings", applied)

    with pytest.raises(HTTPException) as refused:
        await settings_routes.update_settings(
            _patch_request(), body=settings_routes.UpdateSettingsRequest(**{field: value})
        )

    assert refused.value.status_code == 409
    assert refused.value.detail == {
        "code": "LOCKED_BY_ENV",
        "fields": [{"field": field, "env_var": variable}],
        "deployment_owned": [{"field": field, "env_var": variable}],
    }
    service.set.assert_not_awaited()
    applied.assert_not_awaited()


async def test_resubmitting_the_deployments_own_values_saves_only_the_rest(
    monkeypatch: pytest.MonkeyPatch, runtime_env: dict[str, str]
) -> None:
    """The default Helm values pin the document model and size; the web form sends all six."""
    deploy(
        runtime_env,
        SIBYL_EMBEDDING_MODEL="text-embedding-3-small",
        SIBYL_EMBEDDING_DIMENSIONS="1536",
    )
    monkeypatch.setattr(settings_routes, "require_settings_owner", AsyncMock())
    service = AsyncMock()
    monkeypatch.setattr(settings_routes, "get_settings_service", lambda: service)
    applied = AsyncMock()
    monkeypatch.setattr(settings_routes, "sync_runtime_settings", applied)
    monkeypatch.setattr(settings_routes, "announce_runtime_settings_changed", AsyncMock())

    response = await settings_routes.update_settings(
        _patch_request(),
        body=settings_routes.UpdateSettingsRequest(
            embedding_provider="openai",
            embedding_model="text-embedding-3-small",
            embedding_dimensions=1536,
            graph_embedding_provider="openai",
            graph_embedding_model="text-embedding-3-large",
            graph_embedding_dimensions=1024,
        ),
    )

    assert response.updated == [
        "embedding_provider",
        "graph_embedding_provider",
        "graph_embedding_model",
        "graph_embedding_dimensions",
    ]
    saved = [call.args[0] for call in service.set.await_args_list]
    assert "embedding_model" not in saved
    assert "embedding_dimensions" not in saved
    applied.assert_awaited_once()

    with pytest.raises(HTTPException) as refused:
        await settings_routes.update_settings(
            _patch_request(),
            body=settings_routes.UpdateSettingsRequest(
                embedding_model="text-embedding-3-small", embedding_dimensions=768
            ),
        )
    assert refused.value.detail["fields"] == [
        {"field": "embedding_dimensions", "env_var": "SIBYL_EMBEDDING_DIMENSIONS"}
    ]
    assert {owned["field"] for owned in refused.value.detail["deployment_owned"]} == {
        "embedding_model",
        "embedding_dimensions",
    }


async def test_settings_report_what_the_deployment_owns(
    monkeypatch: pytest.MonkeyPatch, runtime_env: dict[str, str]
) -> None:
    deploy(
        runtime_env,
        SIBYL_EMBEDDING_DIMENSIONS="1536",
        SIBYL_OPENAI_API_KEY="sk-deploy-1234567890",
    )
    monkeypatch.setattr(settings_routes, "require_settings_owner", AsyncMock())
    service = AsyncMock()
    service.get_all.return_value = {
        # A value stored before the deployment pinned the setting.
        "embedding_dimensions": {
            "configured": True,
            "source": "database",
            "is_secret": False,
            "masked": None,
            "value": "768",
        },
        "embedding_model": {
            "configured": True,
            "source": "database",
            "is_secret": False,
            "masked": None,
            "value": "text-embedding-3-large",
        },
    }
    monkeypatch.setattr(settings_routes, "get_settings_service", lambda: service)

    response = await settings_routes.get_settings(_patch_request())

    dimensions = response.settings["embedding_dimensions"]
    assert dimensions.locked_by_env is True
    assert dimensions.env_var == "SIBYL_EMBEDDING_DIMENSIONS"
    assert dimensions.source == "environment"
    assert dimensions.value == "1536"  # what is in effect, not what is stored
    key = response.settings["openai_api_key"]
    assert key.locked_by_env is True
    assert key.value is None
    assert key.masked is not None
    assert "1234567890" not in key.masked
    assert response.settings["embedding_model"].locked_by_env is False


async def test_settings_routes_apply_locally_then_announce(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    async def sync(**kwargs: object) -> None:
        events.append("sync")

    async def announce(keys: list[str]) -> None:
        events.append(f"announce:{','.join(keys)}")

    monkeypatch.setattr(settings_routes, "sync_runtime_settings", sync)
    monkeypatch.setattr(settings_routes, "announce_runtime_settings_changed", announce)
    monkeypatch.setattr(settings_routes, "require_settings_owner", AsyncMock())
    monkeypatch.setattr(settings_routes, "is_setup_mode", AsyncMock(return_value=False))
    service = AsyncMock()
    service.delete.return_value = True
    monkeypatch.setattr(settings_routes, "get_settings_service", lambda: service)

    await settings_routes.update_settings(
        _patch_request(),
        body=settings_routes.UpdateSettingsRequest(graph_embedding_model="text-embedding-3-large"),
    )
    await settings_routes.delete_setting(_patch_request(), key="graph_embedding_model")
    await settings_routes.delete_setting(_patch_request(), key="llm.budget.monthly_org_tokens")

    assert events == [
        "sync",
        "announce:graph_embedding_model",
        "sync",
        "announce:graph_embedding_model",
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
    replicas = await _replicas(table, monkeypatch, linked=True)
    stored = {"llm.crawler.model": "claude-haiku-5-5", "anthropic_api_key": "sk-ant"}
    b_source = DBSettingsConfigSource(_SettingsValues(stored), environ={})  # type: ignore[arg-type]
    llm_config.set_config_source(b_source)
    assert (await b_source.resolve(LLMSurface.CRAWLER)).model.value == "claude-haiku-5-5"

    stored["llm.crawler.model"] = "claude-sonnet-5-5"
    assert (await b_source.resolve(LLMSurface.CRAWLER)).model.value == "claude-haiku-5-5"
    await announce_llm_runtime_invalidated(LLMSurface.CRAWLER.value)
    await replicas.settle()

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


async def test_resubscribing_clears_settings_and_llm_caches_and_resyncs_the_runtime(
    table: SettingsTable, monkeypatch: pytest.MonkeyPatch, rebuilds: AsyncMock
) -> None:
    bus = CacheInvalidationBus()
    service = SettingsService(_no_session)
    monkeypatch.setattr(settings_module, "_settings_service", service)
    stored = {"llm.default.model": f"model-{uuid4().hex[:6]}"}
    source = DBSettingsConfigSource(_SettingsValues(stored), environ={})  # type: ignore[arg-type]
    llm_config.set_config_source(source)
    install_cache_invalidation_handlers(bus)
    table.store("embedding_model", "old")
    assert await service.get("embedding_model") == "old"
    await source.resolve(LLMSurface.DEFAULT)
    table.store("embedding_model", "new")
    stored["llm.default.model"] = "model-new"

    await bus.reset()
    await bus.drain()

    assert await service.get("embedding_model") == "new"
    assert (await source.resolve(LLMSurface.DEFAULT)).model.value == "model-new"
    assert os.environ["SIBYL_EMBEDDING_MODEL"] == "new"
    rebuilds.assert_awaited_once()
    assert RUNTIME_SETTINGS_TOPIC in bus._workers
