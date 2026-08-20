"""Static contracts for the community service module boundaries."""

from __future__ import annotations

import ast
import importlib
import inspect
from pathlib import Path
from types import ModuleType

import sibyl_core.services.graph_communities as community_api
from sibyl_core.services import (
    graph_community_clusters,
    graph_community_detection,
    graph_community_hierarchy,
    graph_community_managers,
    graph_community_membership,
    graph_community_models,
    graph_community_selection,
    graph_community_snapshot,
)

PUBLIC_EXPORTS = [
    "CLUSTER_CACHE",
    "CLUSTER_CACHE_TTL",
    "CLUSTER_DETAIL_MAX_EDGES",
    "CLUSTER_DETAIL_MAX_NODES",
    "CLUSTER_SUMMARY_MEMBER_SAMPLE",
    "DETECTION_MAX_ENTITIES",
    "DETECTION_MAX_RELATIONSHIPS",
    "GRAPH_LOD_CACHE",
    "GRAPH_LOD_CACHE_TTL",
    "GRAPH_RESOLUTION_DETAIL",
    "GRAPH_RESOLUTION_OVERVIEW",
    "GRAPH_SNAPSHOT_CACHE",
    "GRAPH_SNAPSHOT_CACHE_TTL",
    "GRAPH_SNAPSHOT_LOADS",
    "HIERARCHICAL_CACHE",
    "HIERARCHICAL_CACHE_TTL",
    "OVERVIEW_MAX_CLUSTERS",
    "OVERVIEW_NODE_THRESHOLD",
    "ClusterSummary",
    "CommunityConfig",
    "DetectedCommunity",
    "GraphSnapshot",
    "HierarchicalGraphData",
    "detect_communities",
    "detect_communities_leiden",
    "detect_communities_louvain",
    "export_to_networkx",
    "get_cluster_nodes",
    "get_clusters_for_visualization",
    "get_community_members",
    "get_entity_communities",
    "get_hierarchical_graph",
    "link_hierarchy",
    "partition_to_communities",
    "store_communities",
]

MODULES: dict[str, ModuleType] = {
    "graph_community_clusters": graph_community_clusters,
    "graph_community_detection": graph_community_detection,
    "graph_community_hierarchy": graph_community_hierarchy,
    "graph_community_managers": graph_community_managers,
    "graph_community_membership": graph_community_membership,
    "graph_community_models": graph_community_models,
    "graph_community_selection": graph_community_selection,
    "graph_community_snapshot": graph_community_snapshot,
}

ALLOWED_IMPORTS = {
    "graph_community_clusters": {
        "graph_community_detection",
        "graph_community_managers",
        "graph_community_models",
        "graph_community_selection",
        "graph_community_snapshot",
    },
    "graph_community_detection": {
        "graph_community_managers",
        "graph_community_models",
        "graph_community_selection",
        "graph_community_snapshot",
    },
    "graph_community_hierarchy": {
        "graph_community_detection",
        "graph_community_managers",
        "graph_community_models",
        "graph_community_selection",
        "graph_community_snapshot",
    },
    "graph_community_managers": {"graph_community_models"},
    "graph_community_membership": {
        "graph_community_managers",
        "graph_community_models",
    },
    "graph_community_models": set(),
    "graph_community_selection": {
        "graph_community_managers",
        "graph_community_models",
        "graph_community_snapshot",
    },
    "graph_community_snapshot": {
        "graph_community_managers",
        "graph_community_models",
    },
}

PUBLIC_OWNERS = {
    "CLUSTER_CACHE": graph_community_clusters,
    "CLUSTER_CACHE_TTL": graph_community_clusters,
    "CLUSTER_DETAIL_MAX_EDGES": graph_community_selection,
    "CLUSTER_DETAIL_MAX_NODES": graph_community_selection,
    "CLUSTER_SUMMARY_MEMBER_SAMPLE": graph_community_selection,
    "DETECTION_MAX_ENTITIES": graph_community_selection,
    "DETECTION_MAX_RELATIONSHIPS": graph_community_selection,
    "GRAPH_LOD_CACHE": graph_community_hierarchy,
    "GRAPH_LOD_CACHE_TTL": graph_community_hierarchy,
    "GRAPH_RESOLUTION_DETAIL": graph_community_models,
    "GRAPH_RESOLUTION_OVERVIEW": graph_community_models,
    "GRAPH_SNAPSHOT_CACHE": graph_community_snapshot,
    "GRAPH_SNAPSHOT_CACHE_TTL": graph_community_snapshot,
    "GRAPH_SNAPSHOT_LOADS": graph_community_snapshot,
    "HIERARCHICAL_CACHE": graph_community_hierarchy,
    "HIERARCHICAL_CACHE_TTL": graph_community_hierarchy,
    "OVERVIEW_MAX_CLUSTERS": graph_community_selection,
    "OVERVIEW_NODE_THRESHOLD": graph_community_selection,
    "ClusterSummary": graph_community_models,
    "CommunityConfig": graph_community_models,
    "DetectedCommunity": graph_community_models,
    "GraphSnapshot": graph_community_models,
    "HierarchicalGraphData": graph_community_models,
    "detect_communities": graph_community_detection,
    "detect_communities_leiden": graph_community_detection,
    "detect_communities_louvain": graph_community_detection,
    "export_to_networkx": graph_community_detection,
    "get_cluster_nodes": graph_community_clusters,
    "get_clusters_for_visualization": graph_community_clusters,
    "get_community_members": graph_community_membership,
    "get_entity_communities": graph_community_membership,
    "get_hierarchical_graph": graph_community_hierarchy,
    "link_hierarchy": graph_community_detection,
    "partition_to_communities": graph_community_detection,
    "store_communities": graph_community_membership,
}


def _source_tree(module: ModuleType) -> ast.Module:
    source_path = inspect.getsourcefile(module)
    assert source_path is not None
    return ast.parse(Path(source_path).read_text())


def _community_imports(module: ModuleType) -> set[str]:
    imported: set[str] = set()
    for node in ast.walk(_source_tree(module)):
        if not isinstance(node, ast.ImportFrom) or node.module is None:
            continue
        leaf = node.module.rsplit(".", maxsplit=1)[-1]
        if leaf.startswith("graph_community_"):
            imported.add(leaf)
    return imported


def test_public_facade_has_an_explicit_ordered_contract() -> None:
    assert community_api.__all__ == PUBLIC_EXPORTS
    assert all(not name.startswith("_") for name in community_api.__all__)

    for name, owner in PUBLIC_OWNERS.items():
        assert getattr(community_api, name) is getattr(owner, name)


def test_private_helpers_are_not_facade_patch_seams() -> None:
    private_owners = {
        "_build_cluster_detail_graph_from_snapshot": graph_community_selection,
        "_create_type_based_clusters": graph_community_clusters,
        "_entity_manager_factory": graph_community_managers,
        "_get_graph_snapshot": graph_community_snapshot,
        "_list_all_entities": graph_community_managers,
        "_load_graph_snapshot": graph_community_snapshot,
    }

    for name, owner in private_owners.items():
        assert not hasattr(community_api, name)
        assert hasattr(owner, name)


def test_community_module_dependencies_are_explicit_and_acyclic() -> None:
    dependency_graph = {name: _community_imports(module) for name, module in MODULES.items()}
    for name in MODULES:
        assert dependency_graph[name] == ALLOWED_IMPORTS[name]
        assert "graph_communities" not in dependency_graph[name]

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(name: str) -> None:
        assert name not in visiting, f"community import cycle through {name}"
        if name in visited:
            return
        visiting.add(name)
        for dependency in dependency_graph[name]:
            visit(dependency)
        visiting.remove(name)
        visited.add(name)

    for name in dependency_graph:
        visit(name)


def test_community_implementation_modules_stay_cohesive() -> None:
    for module in MODULES.values():
        source_path = inspect.getsourcefile(module)
        assert source_path is not None
        assert len(Path(source_path).read_text().splitlines()) < 1000


def test_facade_imports_without_initializing_optional_algorithms() -> None:
    reloaded = importlib.reload(community_api)

    assert reloaded.CommunityConfig().resolutions == [0.5, 1.0, 2.0]
