"""Every API process renders the same clusters for the same graph.

Replicas behind a round-robin balancer each start with their own string hash
seed, so anything that walks a set of ids in iteration order can rank, label
or truncate clusters differently from one request to the next. Two
interpreters with different PYTHONHASHSEED values must detect and render
identical clusters, overview and drill-in alike.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

_RENDER = r"""
import json
import logging

import structlog

structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.ERROR))

from sibyl_core.models.entities import Entity, EntityType, Relationship, RelationshipType
from sibyl_core.services.graph_community_hierarchy import _detect_clusters, _render_levels
from sibyl_core.services.graph_community_selection import OVERVIEW_MAX_CLUSTERS

# Equal cliques give equal cluster sizes and equal member degrees, so every
# ranking in the render meets ties; one bridge per neighbouring pair keeps
# the graph connected without merging them.
CLIQUES, SIZE = 48, 4
TYPES = (EntityType.TOPIC, EntityType.TASK, EntityType.NOTE, EntityType.PATTERN)
entities = [
    Entity(
        id=f"node-{clique:02d}-{member}",
        name=f"Node {clique} {member}",
        entity_type=TYPES[(clique + member) % len(TYPES)],
        organization_id="org-cluster-determinism",
    )
    for clique in range(CLIQUES)
    for member in range(SIZE)
]
edges = [
    (f"node-{clique:02d}-{a}", f"node-{clique:02d}-{b}")
    for clique in range(CLIQUES)
    for a in range(SIZE)
    for b in range(a + 1, SIZE)
]
edges += [(f"node-{clique:02d}-0", f"node-{clique + 1:02d}-1") for clique in range(CLIQUES - 1)]
relationships = [
    Relationship(
        id=f"edge-{index:04d}",
        source_id=source,
        target_id=target,
        relationship_type=RelationshipType.RELATED_TO,
    )
    for index, (source, target) in enumerate(edges)
]

detected = _detect_clusters(entities, relationships)
node_to_cluster = {member: community.id for community in detected for member in community.member_ids}
clusters_meta = [
    {"id": community.id, "member_count": community.member_count, "level": community.level}
    for community in detected
]


def render(resolution, cluster_id=None):
    return _render_levels(
        entities,
        relationships,
        node_to_cluster,
        clusters_meta,
        resolution=resolution,
        cluster_id=cluster_id,
        project_ids=None,
        entity_types=None,
        max_nodes=1000,
        max_edges=5000,
    ).__dict__


assert len(detected) > OVERVIEW_MAX_CLUSTERS, len(detected)
print(
    "RESULT "
    + json.dumps(
        {
            "communities": [[c.id, c.member_ids] for c in detected],
            "detail": render("detail"),
            "drill_in": render("detail", detected[0].id),
        },
        default=lambda value: value.__dict__,
    )
)
"""


def _render_in_process(hash_seed: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-c", _RENDER],
        env={**os.environ, "PYTHONHASHSEED": hash_seed},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _result(process: subprocess.Popen[str]) -> dict:
    stdout, stderr = process.communicate(timeout=120)
    assert process.returncode == 0, stderr
    (line,) = [line for line in stdout.splitlines() if line.startswith("RESULT ")]
    return json.loads(line.removeprefix("RESULT "))


def test_processes_with_different_hash_seeds_render_identical_clusters() -> None:
    first, second = (_render_in_process(seed) for seed in ("1", "2"))
    one, two = _result(first), _result(second)

    assert one["communities"] == two["communities"]
    assert one["detail"]["overview"] == two["detail"]["overview"]
    assert one["detail"] == two["detail"]
    assert one["drill_in"] == two["drill_in"]
    assert len(one["detail"]["overview"]["nodes"]) == 18
