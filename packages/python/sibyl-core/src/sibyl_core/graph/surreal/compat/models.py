"""Sibyl-owned models for the Surreal Graphiti-compatibility island."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class GraphitiCompatError(Exception):
    """Base exception for Sibyl's Graphiti-compatible Surreal surface."""


class EdgeNotFoundError(GraphitiCompatError):
    def __init__(self, uuid: str) -> None:
        self.message = f"edge {uuid} not found"
        super().__init__(self.message)


class NodeNotFoundError(GraphitiCompatError):
    def __init__(self, uuid: str) -> None:
        self.message = f"node {uuid} not found"
        super().__init__(self.message)


class EpisodeType(StrEnum):
    message = "message"
    json = "json"
    text = "text"
    fact_triple = "fact_triple"

    @staticmethod
    def from_str(episode_type: str) -> EpisodeType:
        try:
            return EpisodeType(episode_type)
        except ValueError as exc:
            raise NotImplementedError from exc


class CompatModel(BaseModel):
    model_config = ConfigDict(validate_assignment=True)


class Node(CompatModel):
    uuid: str = Field(default_factory=lambda: str(uuid4()))
    name: str
    group_id: str
    labels: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    async def delete(self, driver: Any) -> None:
        await driver.graph_operations_interface.node_delete(self, driver)


class EpisodicNode(Node):
    source: EpisodeType
    source_description: str
    content: str
    valid_at: datetime
    entity_edges: list[str] = Field(default_factory=list)
    episode_metadata: dict[str, Any] | None = None

    async def save(self, driver: Any) -> None:
        await driver.graph_operations_interface.episodic_node_save(self, driver)

    @classmethod
    async def get_by_uuid(cls, driver: Any, uuid: str) -> EpisodicNode:
        return await driver.graph_operations_interface.episodic_node_get_by_uuid(cls, driver, uuid)

    @classmethod
    async def get_by_uuids(cls, driver: Any, uuids: list[str]) -> list[EpisodicNode]:
        return await driver.graph_operations_interface.episodic_node_get_by_uuids(
            cls, driver, uuids
        )

    @classmethod
    async def get_by_group_ids(
        cls,
        driver: Any,
        group_ids: list[str],
        limit: int | None = None,
        uuid_cursor: str | None = None,
    ) -> list[EpisodicNode]:
        return await driver.graph_operations_interface.episodic_node_get_by_group_ids(
            cls, driver, group_ids, limit, uuid_cursor
        )

    @classmethod
    async def get_by_entity_node_uuid(
        cls, driver: Any, entity_node_uuid: str
    ) -> list[EpisodicNode]:
        return await driver.graph_operations_interface.episodic_node_get_by_entity_node_uuid(
            cls, driver, entity_node_uuid
        )


class EntityNode(Node):
    name_embedding: list[float] | None = None
    summary: str = ""
    attributes: dict[str, Any] = Field(default_factory=dict)

    async def save(self, driver: Any) -> None:
        await driver.graph_operations_interface.node_save(self, driver)

    async def generate_name_embedding(self, embedder: Any) -> list[float]:
        text = self.name.replace("\n", " ")
        self.name_embedding = await embedder.create(input_data=[text])
        return self.name_embedding

    async def load_name_embedding(self, driver: Any) -> None:
        await driver.graph_operations_interface.node_load_embeddings(self, driver)

    @classmethod
    async def get_by_uuid(cls, driver: Any, uuid: str) -> EntityNode:
        return await driver.graph_operations_interface.node_get_by_uuid(cls, driver, uuid)

    @classmethod
    async def get_by_uuids(
        cls,
        driver: Any,
        uuids: list[str],
        group_id: str | None = None,
    ) -> list[EntityNode]:
        return await driver.graph_operations_interface.node_get_by_uuids(
            cls, driver, uuids, group_id
        )

    @classmethod
    async def get_by_group_ids(
        cls,
        driver: Any,
        group_ids: list[str],
        limit: int | None = None,
        uuid_cursor: str | None = None,
        with_embeddings: bool = False,
    ) -> list[EntityNode]:
        del with_embeddings
        return await driver.graph_operations_interface.node_get_by_group_ids(
            cls, driver, group_ids, limit, uuid_cursor
        )


class CommunityNode(Node):
    name_embedding: list[float] | None = None
    summary: str = ""

    async def save(self, driver: Any) -> None:
        await driver.graph_operations_interface.community_node_save(self, driver)

    async def generate_name_embedding(self, embedder: Any) -> list[float]:
        text = self.name.replace("\n", " ")
        self.name_embedding = await embedder.create(input_data=[text])
        return self.name_embedding

    async def load_name_embedding(self, driver: Any) -> None:
        await driver.graph_operations_interface.community_node_load_name_embedding(self, driver)

    @classmethod
    async def get_by_uuid(cls, driver: Any, uuid: str) -> CommunityNode:
        return await driver.graph_operations_interface.community_node_get_by_uuid(cls, driver, uuid)

    @classmethod
    async def get_by_uuids(cls, driver: Any, uuids: list[str]) -> list[CommunityNode]:
        return await driver.graph_operations_interface.community_node_get_by_uuids(
            cls, driver, uuids
        )

    @classmethod
    async def get_by_group_ids(
        cls,
        driver: Any,
        group_ids: list[str],
        limit: int | None = None,
        uuid_cursor: str | None = None,
    ) -> list[CommunityNode]:
        return await driver.graph_operations_interface.community_node_get_by_group_ids(
            cls, driver, group_ids, limit, uuid_cursor
        )


class SagaNode(Node):
    summary: str = ""
    first_episode_uuid: str | None = None
    last_episode_uuid: str | None = None
    last_summarized_at: datetime | None = None

    async def save(self, driver: Any) -> None:
        await driver.graph_operations_interface.saga_node_save(self, driver)

    async def delete(self, driver: Any) -> None:
        await driver.graph_operations_interface.saga_node_delete(self, driver)

    @classmethod
    async def get_by_uuid(cls, driver: Any, uuid: str) -> SagaNode:
        return await driver.graph_operations_interface.saga_node_get_by_uuid(cls, driver, uuid)

    @classmethod
    async def get_by_uuids(cls, driver: Any, uuids: list[str]) -> list[SagaNode]:
        return await driver.graph_operations_interface.saga_node_get_by_uuids(cls, driver, uuids)

    @classmethod
    async def get_by_group_ids(
        cls,
        driver: Any,
        group_ids: list[str],
        limit: int | None = None,
        uuid_cursor: str | None = None,
    ) -> list[SagaNode]:
        return await driver.graph_operations_interface.saga_node_get_by_group_ids(
            cls, driver, group_ids, limit, uuid_cursor
        )


class Edge(CompatModel):
    uuid: str = Field(default_factory=lambda: str(uuid4()))
    group_id: str
    source_node_uuid: str
    target_node_uuid: str
    created_at: datetime

    async def delete(self, driver: Any) -> None:
        await driver.graph_operations_interface.edge_delete(self, driver)


class EpisodicEdge(Edge):
    async def save(self, driver: Any) -> None:
        await driver.graph_operations_interface.episodic_edge_save(self, driver)

    @classmethod
    async def get_by_uuid(cls, driver: Any, uuid: str) -> EpisodicEdge:
        return await driver.graph_operations_interface.episodic_edge_get_by_uuid(cls, driver, uuid)

    @classmethod
    async def get_by_uuids(cls, driver: Any, uuids: list[str]) -> list[EpisodicEdge]:
        return await driver.graph_operations_interface.episodic_edge_get_by_uuids(
            cls, driver, uuids
        )

    @classmethod
    async def get_by_group_ids(
        cls,
        driver: Any,
        group_ids: list[str],
        limit: int | None = None,
        uuid_cursor: str | None = None,
    ) -> list[EpisodicEdge]:
        return await driver.graph_operations_interface.episodic_edge_get_by_group_ids(
            cls, driver, group_ids, limit, uuid_cursor
        )


class EntityEdge(Edge):
    name: str
    fact: str
    fact_embedding: list[float] | None = None
    episodes: list[str] = Field(default_factory=list)
    expired_at: datetime | None = None
    valid_at: datetime | None = None
    invalid_at: datetime | None = None
    reference_time: datetime | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)

    async def save(self, driver: Any) -> None:
        await driver.graph_operations_interface.edge_save(self, driver)

    async def generate_embedding(self, embedder: Any) -> list[float]:
        text = self.fact.replace("\n", " ")
        self.fact_embedding = await embedder.create(input_data=[text])
        return self.fact_embedding

    async def load_fact_embedding(self, driver: Any) -> None:
        await driver.graph_operations_interface.edge_load_embeddings(self, driver)

    @classmethod
    async def get_by_uuid(cls, driver: Any, uuid: str) -> EntityEdge:
        return await driver.graph_operations_interface.edge_get_by_uuid(cls, driver, uuid)

    @classmethod
    async def get_by_uuids(cls, driver: Any, uuids: list[str]) -> list[EntityEdge]:
        return await driver.graph_operations_interface.edge_get_by_uuids(cls, driver, uuids)

    @classmethod
    async def get_between_nodes(
        cls, driver: Any, source_node_uuid: str, target_node_uuid: str
    ) -> list[EntityEdge]:
        return await driver.graph_operations_interface.edge_get_between_nodes(
            cls, driver, source_node_uuid, target_node_uuid
        )

    @classmethod
    async def get_by_group_ids(
        cls,
        driver: Any,
        group_ids: list[str],
        limit: int | None = None,
        uuid_cursor: str | None = None,
        with_embeddings: bool = False,
    ) -> list[EntityEdge]:
        del with_embeddings
        return await driver.graph_operations_interface.edge_get_by_group_ids(
            cls, driver, group_ids, limit, uuid_cursor
        )

    @classmethod
    async def get_by_node_uuid(cls, driver: Any, node_uuid: str) -> list[EntityEdge]:
        return await driver.graph_operations_interface.edge_get_by_node_uuid(cls, driver, node_uuid)


class CommunityEdge(Edge):
    async def save(self, driver: Any) -> None:
        await driver.graph_operations_interface.community_edge_save(self, driver)

    @classmethod
    async def get_by_uuid(cls, driver: Any, uuid: str) -> CommunityEdge:
        return await driver.graph_operations_interface.community_edge_get_by_uuid(cls, driver, uuid)

    @classmethod
    async def get_by_uuids(cls, driver: Any, uuids: list[str]) -> list[CommunityEdge]:
        return await driver.graph_operations_interface.community_edge_get_by_uuids(
            cls, driver, uuids
        )

    @classmethod
    async def get_by_group_ids(
        cls,
        driver: Any,
        group_ids: list[str],
        limit: int | None = None,
        uuid_cursor: str | None = None,
    ) -> list[CommunityEdge]:
        return await driver.graph_operations_interface.community_edge_get_by_group_ids(
            cls, driver, group_ids, limit, uuid_cursor
        )


class HasEpisodeEdge(Edge):
    async def save(self, driver: Any) -> None:
        await driver.graph_operations_interface.has_episode_edge_save(self, driver)

    async def delete(self, driver: Any) -> None:
        await driver.graph_operations_interface.has_episode_edge_delete(self, driver)

    @classmethod
    async def get_by_uuid(cls, driver: Any, uuid: str) -> HasEpisodeEdge:
        return await driver.graph_operations_interface.has_episode_edge_get_by_uuid(
            cls, driver, uuid
        )

    @classmethod
    async def get_by_uuids(cls, driver: Any, uuids: list[str]) -> list[HasEpisodeEdge]:
        return await driver.graph_operations_interface.has_episode_edge_get_by_uuids(
            cls, driver, uuids
        )

    @classmethod
    async def get_by_group_ids(
        cls,
        driver: Any,
        group_ids: list[str],
        limit: int | None = None,
        uuid_cursor: str | None = None,
    ) -> list[HasEpisodeEdge]:
        return await driver.graph_operations_interface.has_episode_edge_get_by_group_ids(
            cls, driver, group_ids, limit, uuid_cursor
        )


class NextEpisodeEdge(Edge):
    async def save(self, driver: Any) -> None:
        await driver.graph_operations_interface.next_episode_edge_save(self, driver)

    async def delete(self, driver: Any) -> None:
        await driver.graph_operations_interface.next_episode_edge_delete(self, driver)

    @classmethod
    async def get_by_uuid(cls, driver: Any, uuid: str) -> NextEpisodeEdge:
        return await driver.graph_operations_interface.next_episode_edge_get_by_uuid(
            cls, driver, uuid
        )

    @classmethod
    async def get_by_uuids(cls, driver: Any, uuids: list[str]) -> list[NextEpisodeEdge]:
        return await driver.graph_operations_interface.next_episode_edge_get_by_uuids(
            cls, driver, uuids
        )

    @classmethod
    async def get_by_group_ids(
        cls,
        driver: Any,
        group_ids: list[str],
        limit: int | None = None,
        uuid_cursor: str | None = None,
    ) -> list[NextEpisodeEdge]:
        return await driver.graph_operations_interface.next_episode_edge_get_by_group_ids(
            cls, driver, group_ids, limit, uuid_cursor
        )


__all__ = [
    "CommunityEdge",
    "CommunityNode",
    "EdgeNotFoundError",
    "EntityEdge",
    "EntityNode",
    "EpisodeType",
    "EpisodicEdge",
    "EpisodicNode",
    "GraphitiCompatError",
    "HasEpisodeEdge",
    "NextEpisodeEdge",
    "NodeNotFoundError",
    "SagaNode",
]
