"""Graphiti graph operations adapter for the SurrealDB driver."""

from __future__ import annotations

from typing import Any

from graphiti_core.driver.graph_operations.graph_operations import GraphOperationsInterface
from graphiti_core.driver.record_parsers import episodic_node_from_record
from graphiti_core.edges import (
    CommunityEdge,
    EntityEdge,
    EpisodicEdge,
    HasEpisodeEdge,
    NextEpisodeEdge,
)
from graphiti_core.nodes import CommunityNode, EntityNode, EpisodeType, EpisodicNode, SagaNode

from sibyl_core.graph.surreal.ops._common import normalize_records

_ENTITY_NODE_FIELDS = {
    "uuid",
    "name",
    "group_id",
    "labels",
    "created_at",
    "summary",
    "name_embedding",
    "attributes",
}
_ENTITY_EDGE_FIELDS = {
    "uuid",
    "group_id",
    "source_node_uuid",
    "target_node_uuid",
    "created_at",
    "name",
    "fact",
    "fact_embedding",
    "episodes",
    "expired_at",
    "valid_at",
    "invalid_at",
    "attributes",
}


def _episodic_node_from_bulk_payload(item: EpisodicNode | dict[str, Any]) -> EpisodicNode:
    if isinstance(item, EpisodicNode):
        return item
    payload = dict(item)
    if isinstance(payload.get("source"), str):
        payload["source"] = EpisodeType.from_str(payload["source"])
    payload.setdefault("labels", [])
    payload.setdefault("entity_edges", [])
    return EpisodicNode.model_validate(payload)


def _entity_node_from_bulk_payload(item: EntityNode | dict[str, Any]) -> EntityNode:
    if isinstance(item, EntityNode):
        return item
    payload = dict(item)
    attributes = dict(payload.get("attributes") or {})
    attributes.update(
        {
            key: value
            for key, value in payload.items()
            if key not in _ENTITY_NODE_FIELDS and value is not None
        }
    )
    payload["attributes"] = attributes
    return EntityNode.model_validate(
        {key: value for key, value in payload.items() if key in _ENTITY_NODE_FIELDS}
    )


def _episodic_edge_from_bulk_payload(item: EpisodicEdge | dict[str, Any]) -> EpisodicEdge:
    if isinstance(item, EpisodicEdge):
        return item
    return EpisodicEdge.model_validate(item)


def _entity_edge_from_bulk_payload(item: EntityEdge | dict[str, Any]) -> EntityEdge:
    if isinstance(item, EntityEdge):
        return item
    payload = dict(item)
    attributes = dict(payload.get("attributes") or {})
    attributes.update(
        {
            key: value
            for key, value in payload.items()
            if key not in _ENTITY_EDGE_FIELDS and value is not None
        }
    )
    payload["attributes"] = attributes
    return EntityEdge.model_validate(
        {key: value for key, value in payload.items() if key in _ENTITY_EDGE_FIELDS}
    )


class SurrealGraphOperationsInterface(GraphOperationsInterface):
    async def node_save(self, node: EntityNode, driver: Any) -> None:
        await driver.entity_node_ops.save(driver, node)

    async def node_delete(self, node: EntityNode, driver: Any) -> None:
        await driver.entity_node_ops.delete(driver, node)

    async def node_save_bulk(
        self,
        _cls: Any,
        driver: Any,
        transaction: Any,
        nodes: list[EntityNode] | list[dict[str, Any]],
        batch_size: int = 100,
    ) -> None:
        await driver.entity_node_ops.save_bulk(
            driver,
            [_entity_node_from_bulk_payload(node) for node in nodes],
            tx=transaction,
            batch_size=batch_size,
        )

    async def node_delete_by_group_id(
        self,
        _cls: Any,
        driver: Any,
        group_id: str,
        batch_size: int = 100,
    ) -> None:
        await driver.entity_node_ops.delete_by_group_id(driver, group_id, batch_size=batch_size)

    async def node_delete_by_uuids(
        self,
        _cls: Any,
        driver: Any,
        uuids: list[str],
        group_id: str | None = None,
        batch_size: int = 100,
    ) -> None:
        del group_id
        await driver.entity_node_ops.delete_by_uuids(driver, uuids, batch_size=batch_size)

    async def node_get_by_uuid(self, _cls: Any, driver: Any, uuid: str) -> EntityNode:
        return await driver.entity_node_ops.get_by_uuid(driver, uuid)

    async def node_get_by_uuids(
        self, _cls: Any, driver: Any, uuids: list[str]
    ) -> list[EntityNode]:
        return await driver.entity_node_ops.get_by_uuids(driver, uuids)

    async def node_get_by_group_ids(
        self,
        _cls: Any,
        driver: Any,
        group_ids: list[str],
        limit: int | None = None,
        uuid_cursor: str | None = None,
    ) -> list[EntityNode]:
        return await driver.entity_node_ops.get_by_group_ids(driver, group_ids, limit, uuid_cursor)

    async def node_load_embeddings(self, node: EntityNode, driver: Any) -> None:
        await driver.entity_node_ops.load_embeddings(driver, node)

    async def node_load_embeddings_bulk(
        self,
        driver: Any,
        nodes: list[EntityNode],
        batch_size: int = 100,
    ) -> dict[str, list[float]]:
        await driver.entity_node_ops.load_embeddings_bulk(driver, nodes, batch_size=batch_size)
        return {
            node.uuid: node.name_embedding
            for node in nodes
            if node.name_embedding is not None
        }

    async def episodic_node_save(self, node: EpisodicNode, driver: Any) -> None:
        await driver.episode_node_ops.save(driver, node)

    async def episodic_node_delete(self, node: EpisodicNode, driver: Any) -> None:
        await driver.episode_node_ops.delete(driver, node)

    async def episodic_node_save_bulk(
        self,
        _cls: Any,
        driver: Any,
        transaction: Any,
        nodes: list[EpisodicNode] | list[dict[str, Any]],
        batch_size: int = 100,
    ) -> None:
        await driver.episode_node_ops.save_bulk(
            driver,
            [_episodic_node_from_bulk_payload(node) for node in nodes],
            tx=transaction,
            batch_size=batch_size,
        )

    async def episodic_edge_save_bulk(
        self,
        _cls: Any,
        driver: Any,
        transaction: Any,
        episodic_edges: list[EpisodicEdge] | list[dict[str, Any]],
        batch_size: int = 100,
    ) -> None:
        await driver.episodic_edge_ops.save_bulk(
            driver,
            [_episodic_edge_from_bulk_payload(edge) for edge in episodic_edges],
            tx=transaction,
            batch_size=batch_size,
        )

    async def episodic_node_delete_by_group_id(
        self,
        _cls: Any,
        driver: Any,
        group_id: str,
        batch_size: int = 100,
    ) -> None:
        await driver.episode_node_ops.delete_by_group_id(driver, group_id, batch_size=batch_size)

    async def episodic_node_delete_by_uuids(
        self,
        _cls: Any,
        driver: Any,
        uuids: list[str],
        group_id: str | None = None,
        batch_size: int = 100,
    ) -> None:
        del group_id
        await driver.episode_node_ops.delete_by_uuids(driver, uuids, batch_size=batch_size)

    async def episodic_node_get_by_uuid(self, _cls: Any, driver: Any, uuid: str) -> EpisodicNode:
        return await driver.episode_node_ops.get_by_uuid(driver, uuid)

    async def episodic_node_get_by_uuids(
        self, _cls: Any, driver: Any, uuids: list[str]
    ) -> list[EpisodicNode]:
        return await driver.episode_node_ops.get_by_uuids(driver, uuids)

    async def episodic_node_get_by_group_ids(
        self,
        _cls: Any,
        driver: Any,
        group_ids: list[str],
        limit: int | None = None,
        uuid_cursor: str | None = None,
    ) -> list[EpisodicNode]:
        return await driver.episode_node_ops.get_by_group_ids(driver, group_ids, limit, uuid_cursor)

    async def retrieve_episodes(
        self,
        driver: Any,
        reference_time: Any,
        last_n: int = 3,
        group_ids: list[str] | None = None,
        source: EpisodeType | None = None,
        saga: str | None = None,
    ) -> list[EpisodicNode]:
        clauses = ["valid_at <= $reference_time"]
        if group_ids is not None:
            clauses.append("group_id IN $group_ids")
        if source is not None:
            clauses.append("source = $source")
        if saga is not None:
            clauses.append(
                "id IN (SELECT VALUE out FROM has_episode WHERE in.name = $saga)"
            )
        records = normalize_records(
            await driver.execute_query(
                "SELECT * FROM episode WHERE "
                + " AND ".join(clauses)
                + " ORDER BY valid_at DESC LIMIT $last_n;",
                reference_time=reference_time,
                group_ids=group_ids,
                source=source.value if source is not None else None,
                saga=saga,
                last_n=max(int(last_n), 0),
            )
        )
        return [episodic_node_from_record(r) for r in reversed(records)]

    async def community_node_save(self, node: CommunityNode, driver: Any) -> None:
        await driver.community_node_ops.save(driver, node)

    async def community_node_delete(self, node: CommunityNode, driver: Any) -> None:
        await driver.community_node_ops.delete(driver, node)

    async def community_node_save_bulk(
        self,
        _cls: Any,
        driver: Any,
        transaction: Any,
        nodes: list[CommunityNode],
        batch_size: int = 100,
    ) -> None:
        await driver.community_node_ops.save_bulk(
            driver, nodes, tx=transaction, batch_size=batch_size
        )

    async def community_node_delete_by_group_id(
        self,
        _cls: Any,
        driver: Any,
        group_id: str,
        batch_size: int = 100,
    ) -> None:
        await driver.community_node_ops.delete_by_group_id(driver, group_id, batch_size=batch_size)

    async def community_node_delete_by_uuids(
        self,
        _cls: Any,
        driver: Any,
        uuids: list[str],
        group_id: str | None = None,
        batch_size: int = 100,
    ) -> None:
        del group_id
        await driver.community_node_ops.delete_by_uuids(driver, uuids, batch_size=batch_size)

    async def community_node_get_by_uuid(self, _cls: Any, driver: Any, uuid: str) -> CommunityNode:
        return await driver.community_node_ops.get_by_uuid(driver, uuid)

    async def community_node_get_by_uuids(
        self, _cls: Any, driver: Any, uuids: list[str]
    ) -> list[CommunityNode]:
        return await driver.community_node_ops.get_by_uuids(driver, uuids)

    async def community_node_get_by_group_ids(
        self,
        _cls: Any,
        driver: Any,
        group_ids: list[str],
        limit: int | None = None,
        uuid_cursor: str | None = None,
    ) -> list[CommunityNode]:
        return await driver.community_node_ops.get_by_group_ids(
            driver, group_ids, limit, uuid_cursor
        )

    async def community_node_load_name_embedding(
        self, node: CommunityNode, driver: Any
    ) -> None:
        await driver.community_node_ops.load_name_embedding(driver, node)

    async def saga_node_save(self, node: SagaNode, driver: Any) -> None:
        await driver.saga_node_ops.save(driver, node)

    async def saga_node_delete(self, node: SagaNode, driver: Any) -> None:
        await driver.saga_node_ops.delete(driver, node)

    async def saga_node_save_bulk(
        self,
        _cls: Any,
        driver: Any,
        transaction: Any,
        nodes: list[SagaNode],
        batch_size: int = 100,
    ) -> None:
        await driver.saga_node_ops.save_bulk(driver, nodes, tx=transaction, batch_size=batch_size)

    async def saga_node_delete_by_group_id(
        self,
        _cls: Any,
        driver: Any,
        group_id: str,
        batch_size: int = 100,
    ) -> None:
        await driver.saga_node_ops.delete_by_group_id(driver, group_id, batch_size=batch_size)

    async def saga_node_delete_by_uuids(
        self,
        _cls: Any,
        driver: Any,
        uuids: list[str],
        group_id: str | None = None,
        batch_size: int = 100,
    ) -> None:
        del group_id
        await driver.saga_node_ops.delete_by_uuids(driver, uuids, batch_size=batch_size)

    async def saga_node_get_by_uuid(self, _cls: Any, driver: Any, uuid: str) -> SagaNode:
        return await driver.saga_node_ops.get_by_uuid(driver, uuid)

    async def saga_node_get_by_uuids(
        self, _cls: Any, driver: Any, uuids: list[str]
    ) -> list[SagaNode]:
        return await driver.saga_node_ops.get_by_uuids(driver, uuids)

    async def saga_node_get_by_group_ids(
        self,
        _cls: Any,
        driver: Any,
        group_ids: list[str],
        limit: int | None = None,
        uuid_cursor: str | None = None,
    ) -> list[SagaNode]:
        return await driver.saga_node_ops.get_by_group_ids(driver, group_ids, limit, uuid_cursor)

    async def edge_save(self, edge: EntityEdge, driver: Any) -> None:
        await driver.entity_edge_ops.save(driver, edge)

    async def edge_delete(self, edge: EntityEdge, driver: Any) -> None:
        await driver.entity_edge_ops.delete(driver, edge)

    async def edge_save_bulk(
        self,
        _cls: Any,
        driver: Any,
        transaction: Any,
        edges: list[EntityEdge] | list[dict[str, Any]],
        batch_size: int = 100,
    ) -> None:
        await driver.entity_edge_ops.save_bulk(
            driver,
            [_entity_edge_from_bulk_payload(edge) for edge in edges],
            tx=transaction,
            batch_size=batch_size,
        )

    async def edge_delete_by_uuids(
        self,
        _cls: Any,
        driver: Any,
        uuids: list[str],
        group_id: str | None = None,
    ) -> None:
        del group_id
        await driver.entity_edge_ops.delete_by_uuids(driver, uuids)

    async def edge_get_by_uuid(self, _cls: Any, driver: Any, uuid: str) -> EntityEdge:
        return await driver.entity_edge_ops.get_by_uuid(driver, uuid)

    async def edge_get_by_uuids(
        self, _cls: Any, driver: Any, uuids: list[str]
    ) -> list[EntityEdge]:
        return await driver.entity_edge_ops.get_by_uuids(driver, uuids)

    async def edge_get_by_group_ids(
        self,
        _cls: Any,
        driver: Any,
        group_ids: list[str],
        limit: int | None = None,
        uuid_cursor: str | None = None,
    ) -> list[EntityEdge]:
        return await driver.entity_edge_ops.get_by_group_ids(driver, group_ids, limit, uuid_cursor)

    async def edge_get_between_nodes(
        self,
        _cls: Any,
        driver: Any,
        source_node_uuid: str,
        target_node_uuid: str,
        group_ids: list[str] | None = None,
        limit: int | None = None,
    ) -> list[EntityEdge]:
        return await driver.entity_edge_ops.get_between_nodes(
            driver, source_node_uuid, target_node_uuid, group_ids, limit
        )

    async def edge_get_by_node_uuid(
        self,
        _cls: Any,
        driver: Any,
        node_uuid: str,
        group_ids: list[str] | None = None,
        limit: int | None = None,
    ) -> list[EntityEdge]:
        return await driver.entity_edge_ops.get_by_node_uuid(driver, node_uuid, group_ids, limit)

    async def edge_load_embeddings(self, edge: EntityEdge, driver: Any) -> None:
        await driver.entity_edge_ops.load_embeddings(driver, edge)

    async def edge_load_embeddings_bulk(
        self,
        driver: Any,
        edges: list[EntityEdge],
        batch_size: int = 100,
    ) -> dict[str, list[float]]:
        await driver.entity_edge_ops.load_embeddings_bulk(driver, edges, batch_size=batch_size)
        return {
            edge.uuid: edge.fact_embedding
            for edge in edges
            if edge.fact_embedding is not None
        }

    async def episodic_edge_save(self, edge: EpisodicEdge, driver: Any) -> None:
        await driver.episodic_edge_ops.save(driver, edge)

    async def episodic_edge_delete(self, edge: EpisodicEdge, driver: Any) -> None:
        await driver.episodic_edge_ops.delete(driver, edge)

    async def episodic_edge_delete_by_uuids(
        self,
        _cls: Any,
        driver: Any,
        uuids: list[str],
        group_id: str | None = None,
    ) -> None:
        del group_id
        await driver.episodic_edge_ops.delete_by_uuids(driver, uuids)

    async def episodic_edge_get_by_uuid(self, _cls: Any, driver: Any, uuid: str) -> EpisodicEdge:
        return await driver.episodic_edge_ops.get_by_uuid(driver, uuid)

    async def episodic_edge_get_by_uuids(
        self, _cls: Any, driver: Any, uuids: list[str]
    ) -> list[EpisodicEdge]:
        return await driver.episodic_edge_ops.get_by_uuids(driver, uuids)

    async def episodic_edge_get_by_group_ids(
        self,
        _cls: Any,
        driver: Any,
        group_ids: list[str],
        limit: int | None = None,
        uuid_cursor: str | None = None,
    ) -> list[EpisodicEdge]:
        return await driver.episodic_edge_ops.get_by_group_ids(
            driver, group_ids, limit, uuid_cursor
        )

    async def community_edge_save(self, edge: CommunityEdge, driver: Any) -> None:
        await driver.community_edge_ops.save(driver, edge)

    async def community_edge_delete(self, edge: CommunityEdge, driver: Any) -> None:
        await driver.community_edge_ops.delete(driver, edge)

    async def community_edge_delete_by_uuids(
        self,
        _cls: Any,
        driver: Any,
        uuids: list[str],
        group_id: str | None = None,
    ) -> None:
        del group_id
        await driver.community_edge_ops.delete_by_uuids(driver, uuids)

    async def community_edge_get_by_uuid(self, _cls: Any, driver: Any, uuid: str) -> CommunityEdge:
        return await driver.community_edge_ops.get_by_uuid(driver, uuid)

    async def community_edge_get_by_uuids(
        self, _cls: Any, driver: Any, uuids: list[str]
    ) -> list[CommunityEdge]:
        return await driver.community_edge_ops.get_by_uuids(driver, uuids)

    async def community_edge_get_by_group_ids(
        self,
        _cls: Any,
        driver: Any,
        group_ids: list[str],
        limit: int | None = None,
        uuid_cursor: str | None = None,
    ) -> list[CommunityEdge]:
        return await driver.community_edge_ops.get_by_group_ids(
            driver, group_ids, limit, uuid_cursor
        )

    async def has_episode_edge_save(self, edge: HasEpisodeEdge, driver: Any) -> None:
        await driver.has_episode_edge_ops.save(driver, edge)

    async def has_episode_edge_delete(self, edge: HasEpisodeEdge, driver: Any) -> None:
        await driver.has_episode_edge_ops.delete(driver, edge)

    async def has_episode_edge_save_bulk(
        self,
        _cls: Any,
        driver: Any,
        transaction: Any,
        edges: list[HasEpisodeEdge],
        batch_size: int = 100,
    ) -> None:
        await driver.has_episode_edge_ops.save_bulk(
            driver, edges, tx=transaction, batch_size=batch_size
        )

    async def has_episode_edge_delete_by_uuids(
        self,
        _cls: Any,
        driver: Any,
        uuids: list[str],
        group_id: str | None = None,
    ) -> None:
        del group_id
        await driver.has_episode_edge_ops.delete_by_uuids(driver, uuids)

    async def has_episode_edge_get_by_uuid(
        self, _cls: Any, driver: Any, uuid: str
    ) -> HasEpisodeEdge:
        return await driver.has_episode_edge_ops.get_by_uuid(driver, uuid)

    async def has_episode_edge_get_by_uuids(
        self, _cls: Any, driver: Any, uuids: list[str]
    ) -> list[HasEpisodeEdge]:
        return await driver.has_episode_edge_ops.get_by_uuids(driver, uuids)

    async def has_episode_edge_get_by_group_ids(
        self,
        _cls: Any,
        driver: Any,
        group_ids: list[str],
        limit: int | None = None,
        uuid_cursor: str | None = None,
    ) -> list[HasEpisodeEdge]:
        return await driver.has_episode_edge_ops.get_by_group_ids(
            driver, group_ids, limit, uuid_cursor
        )

    async def next_episode_edge_save(self, edge: NextEpisodeEdge, driver: Any) -> None:
        await driver.next_episode_edge_ops.save(driver, edge)

    async def next_episode_edge_delete(self, edge: NextEpisodeEdge, driver: Any) -> None:
        await driver.next_episode_edge_ops.delete(driver, edge)

    async def next_episode_edge_save_bulk(
        self,
        _cls: Any,
        driver: Any,
        transaction: Any,
        edges: list[NextEpisodeEdge],
        batch_size: int = 100,
    ) -> None:
        await driver.next_episode_edge_ops.save_bulk(
            driver, edges, tx=transaction, batch_size=batch_size
        )

    async def next_episode_edge_delete_by_uuids(
        self,
        _cls: Any,
        driver: Any,
        uuids: list[str],
        group_id: str | None = None,
    ) -> None:
        del group_id
        await driver.next_episode_edge_ops.delete_by_uuids(driver, uuids)

    async def next_episode_edge_get_by_uuid(
        self, _cls: Any, driver: Any, uuid: str
    ) -> NextEpisodeEdge:
        return await driver.next_episode_edge_ops.get_by_uuid(driver, uuid)

    async def next_episode_edge_get_by_uuids(
        self, _cls: Any, driver: Any, uuids: list[str]
    ) -> list[NextEpisodeEdge]:
        return await driver.next_episode_edge_ops.get_by_uuids(driver, uuids)

    async def next_episode_edge_get_by_group_ids(
        self,
        _cls: Any,
        driver: Any,
        group_ids: list[str],
        limit: int | None = None,
        uuid_cursor: str | None = None,
    ) -> list[NextEpisodeEdge]:
        return await driver.next_episode_edge_ops.get_by_group_ids(
            driver, group_ids, limit, uuid_cursor
        )

    async def get_mentioned_nodes(
        self, driver: Any, episodes: list[EpisodicNode]
    ) -> list[EntityNode]:
        return await driver.graph_ops.get_mentioned_nodes(driver, episodes)

    async def get_communities_by_nodes(
        self, driver: Any, nodes: list[EntityNode]
    ) -> list[CommunityNode]:
        return await driver.graph_ops.get_communities_by_nodes(driver, nodes)

    async def clear_data(self, driver: Any, group_ids: list[str] | None = None) -> None:
        await driver.graph_ops.clear_data(driver, group_ids)

    async def get_community_clusters(
        self, driver: Any, group_ids: list[str] | None = None
    ) -> list[list[EntityNode]]:
        return await driver.graph_ops.get_community_clusters(driver, group_ids)

    async def remove_communities(self, driver: Any) -> None:
        await driver.graph_ops.remove_communities(driver)

    async def determine_entity_community(
        self, driver: Any, entity: EntityNode
    ) -> tuple[CommunityNode | None, bool]:
        community = await driver.graph_ops.determine_entity_community(driver, entity)
        return community, False

    async def episodic_node_get_by_entity_node_uuid(
        self, _cls: Any, driver: Any, entity_node_uuid: str
    ) -> list[EpisodicNode]:
        records = normalize_records(
            await driver.execute_query(
                """
                SELECT *
                FROM episode
                WHERE id IN (
                    SELECT VALUE in
                    FROM mentions
                    WHERE out.uuid = $entity_node_uuid
                );
                """,
                entity_node_uuid=entity_node_uuid,
            )
        )
        return [episodic_node_from_record(r) for r in records]


__all__ = ["SurrealGraphOperationsInterface"]
