"""Tests for sibyl_core.graph.search_interface."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from sibyl_core.graph.search_interface import FalkorDBSearchInterface


class TestFalkorDBSearchInterface:
    """Ensure fallback Graphiti searches do not mutate shared driver state."""

    @pytest.mark.asyncio
    async def test_node_similarity_search_uses_driver_copy(self) -> None:
        interface = FalkorDBSearchInterface()
        original_search_interface = object()
        driver = SimpleNamespace(search_interface=original_search_interface, marker="shared")

        async def fake_node_similarity_search(*args, **kwargs):  # type: ignore[no-untyped-def]
            passed_driver = args[0]
            assert passed_driver is not driver
            assert passed_driver.marker == "shared"
            assert passed_driver.search_interface is None
            assert driver.search_interface is original_search_interface
            return ["node-result"]

        with patch(
            "graphiti_core.search.search_utils.node_similarity_search",
            side_effect=fake_node_similarity_search,
        ):
            result = await interface.node_similarity_search(
                driver,
                [0.1, 0.2],
                None,
                ["org-123"],
                10,
                0.7,
            )

        assert result == ["node-result"]
        assert driver.search_interface is original_search_interface

    @pytest.mark.asyncio
    async def test_fallback_searches_do_not_race_on_shared_driver(self) -> None:
        interface = FalkorDBSearchInterface()
        original_search_interface = object()
        driver = SimpleNamespace(search_interface=original_search_interface, marker="shared")

        async def fake_node_similarity_search(*args, **kwargs):  # type: ignore[no-untyped-def]
            passed_driver = args[0]
            assert passed_driver is not driver
            assert passed_driver.search_interface is None
            assert driver.search_interface is original_search_interface
            await asyncio.sleep(0)
            assert driver.search_interface is original_search_interface
            return ["node-result"]

        async def fake_episode_fulltext_search(*args, **kwargs):  # type: ignore[no-untyped-def]
            passed_driver = args[0]
            assert passed_driver is not driver
            assert passed_driver.search_interface is None
            assert driver.search_interface is original_search_interface
            await asyncio.sleep(0)
            assert driver.search_interface is original_search_interface
            return ["episode-result"]

        with (
            patch(
                "graphiti_core.search.search_utils.node_similarity_search",
                side_effect=fake_node_similarity_search,
            ),
            patch(
                "graphiti_core.search.search_utils.episode_fulltext_search",
                side_effect=fake_episode_fulltext_search,
            ),
        ):
            node_result, episode_result = await asyncio.gather(
                interface.node_similarity_search(
                    driver,
                    [0.1, 0.2],
                    None,
                    ["org-123"],
                    10,
                    0.7,
                ),
                interface.episode_fulltext_search(
                    driver,
                    "graph search",
                    None,
                    ["org-123"],
                    10,
                ),
            )

        assert node_result == ["node-result"]
        assert episode_result == ["episode-result"]
        assert driver.search_interface is original_search_interface
