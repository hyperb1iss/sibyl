from types import SimpleNamespace

from graphiti_core.driver.falkordb_driver import FalkorDriver

from sibyl_core.graph import client as _graph_client  # noqa: F401
from sibyl_core.graph.search_interface import FalkorDBSearchInterface


def test_build_fulltext_query_uses_unquoted_group_ids_for_falkordb() -> None:
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
