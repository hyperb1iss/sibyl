from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import graphiti_core
import pytest
from graphiti_core.driver.falkordb_driver import FalkorDriver

import sibyl_core.graph.cached_embedder as cached_embedder
from sibyl_core.backends.surreal import SurrealDriver
from sibyl_core.config import CoreConfig
from sibyl_core.graph import client as _graph_client
from sibyl_core.graph.client import GraphClient
from sibyl_core.graph.search_interface import FalkorDBSearchInterface


def test_build_fulltext_query_uses_unquoted_group_ids_for_falkordb() -> None:
    _graph_client._patch_falkordb_driver()
    driver = object.__new__(FalkorDriver)

    query = driver.build_fulltext_query(
        "vibes whimsy adding cheese",
        ["e7b94a25-dd4c-4fb8-b300-0c75e83998e2"],
        128,
    )

    assert query.startswith("(@group_id:e7b94a25-dd4c-4fb8-b300-0c75e83998e2)")
    assert '"e7b94a25-dd4c-4fb8-b300-0c75e83998e2"' not in query
    assert "(vibes | whimsy | adding | cheese)" in query


def test_clone_preserves_search_interface() -> None:
    _graph_client._patch_falkordb_driver()
    driver = object.__new__(FalkorDriver)
    driver._database = "default"  # type: ignore[attr-defined]
    driver.search_interface = FalkorDBSearchInterface()
    driver.some_shared_state = SimpleNamespace(marker="shared")

    cloned = driver.clone("org-123")

    assert cloned is not driver
    assert cloned._database == "org-123"  # type: ignore[attr-defined]
    assert driver._database == "default"  # type: ignore[attr-defined]
    assert cloned.search_interface is driver.search_interface
    assert cloned.some_shared_state is driver.some_shared_state


def test_core_config_uses_store_env(monkeypatch) -> None:
    monkeypatch.setenv("SIBYL_STORE", "surreal")

    settings = CoreConfig(_env_file=None)

    assert settings.store == "surreal"


def test_core_config_reads_surreal_token(monkeypatch) -> None:
    monkeypatch.setenv("SIBYL_SURREAL_TOKEN", "token-123")

    settings = CoreConfig(_env_file=None)

    assert settings.surreal_token.get_secret_value() == "token-123"


def test_core_config_ignores_removed_graph_backend_alias(monkeypatch) -> None:
    monkeypatch.delenv("SIBYL_STORE", raising=False)
    monkeypatch.setenv("SIBYL_GRAPH_BACKEND", "falkordb")

    settings = CoreConfig(_env_file=None)

    assert settings.store == "surreal"


@pytest.mark.asyncio
async def test_connect_dispatches_to_surreal_runtime(monkeypatch) -> None:
    client = GraphClient()
    connect_surreal = AsyncMock()
    connect_legacy = AsyncMock()

    monkeypatch.setattr(_graph_client.settings, "store", "surreal")
    monkeypatch.setattr(client, "_connect_surreal", connect_surreal)
    monkeypatch.setattr(client, "_connect_legacy", connect_legacy)

    await client.connect()

    connect_surreal.assert_awaited_once_with()
    connect_legacy.assert_not_awaited()


@pytest.mark.asyncio
async def test_connect_dispatches_to_legacy_runtime(monkeypatch) -> None:
    client = GraphClient()
    connect_surreal = AsyncMock()
    connect_legacy = AsyncMock()

    monkeypatch.setattr(_graph_client.settings, "store", "legacy")
    monkeypatch.setattr(client, "_connect_surreal", connect_surreal)
    monkeypatch.setattr(client, "_connect_legacy", connect_legacy)

    await client.connect()

    connect_legacy.assert_awaited_once_with()
    connect_surreal.assert_not_awaited()


@pytest.mark.asyncio
async def test_connect_surreal_constructs_surreal_driver(monkeypatch) -> None:
    client = GraphClient()

    class FakeGraphiti:
        def __init__(self, *, graph_driver, llm_client):
            self.driver = graph_driver
            self.llm_client = llm_client
            self.embedder = object()

        async def close(self) -> None:
            return None

    monkeypatch.setattr(_graph_client.settings, "surreal_url", "memory://")
    monkeypatch.setattr(_graph_client.settings, "surreal_data_dir", "")
    monkeypatch.setattr(_graph_client.settings, "surreal_username", "")
    monkeypatch.setattr(_graph_client.settings, "surreal_namespace_prefix", "tenant_")
    monkeypatch.setattr(_graph_client.settings, "surreal_database", "graph_test")
    monkeypatch.setattr(
        _graph_client.settings,
        "surreal_password",
        _graph_client.settings.surreal_password,
    )
    monkeypatch.setattr(
        _graph_client.settings,
        "surreal_token",
        _graph_client.settings.surreal_token,
    )
    monkeypatch.setattr(client, "_create_llm_client", lambda: object())

    monkeypatch.setattr(graphiti_core, "Graphiti", FakeGraphiti)
    monkeypatch.setattr(
        cached_embedder,
        "wrap_embedder_with_cache",
        lambda embedder, max_size: embedder,
    )

    await client._connect_surreal()

    assert isinstance(client.driver, SurrealDriver)
    assert client.driver.namespace_prefix == "tenant_"
    assert client.driver.default_database == "graph_test"
    assert client.is_connected is True


def test_get_org_driver_reuses_same_clone_for_same_org() -> None:
    client = GraphClient()
    base_driver = MagicMock()
    org_driver = MagicMock()
    client._client = SimpleNamespace(driver=base_driver)
    client._connected = True
    base_driver.clone.return_value = org_driver

    first = client.get_org_driver("org-123")
    second = client.get_org_driver("org-123")

    assert first is org_driver
    assert second is org_driver
    base_driver.clone.assert_called_once_with("org-123")


@pytest.mark.asyncio
async def test_disconnect_closes_cached_org_drivers_once() -> None:
    client = GraphClient()
    base_graphiti = SimpleNamespace(close=AsyncMock())
    org_driver = MagicMock()
    org_driver.close = AsyncMock()
    client._client = base_graphiti
    client._connected = True
    client._org_drivers["org-123"] = org_driver
    client._org_drivers["org-456"] = org_driver

    await client.disconnect()

    org_driver.close.assert_awaited_once_with()
    base_graphiti.close.assert_awaited_once_with()
    assert client._org_drivers == {}
