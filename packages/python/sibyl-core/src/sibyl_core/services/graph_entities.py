"""Entity write manager for the native graph."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from sibyl_core.embeddings.providers import entity_embedding_text
from sibyl_core.errors import RevisionConflictError
from sibyl_core.models.entities import Entity
from sibyl_core.services.graph_common import (
    execute_graph_transaction as _execute_graph_transaction,
)
from sibyl_core.services.graph_common import select_one as _select_one
from sibyl_core.services.graph_embeddings import (
    _entities_with_native_embeddings,
    _entity_with_native_embedding,
)
from sibyl_core.services.graph_entity_store import (
    _CONTENT_MIRRORS_DESCRIPTION_TYPES,
    _entity_update_patch,
    _heal_metadata_snapshots_for_write,
    _insert_entity_if_absent,
    _persisted_entity_embedding_text,
    _replace_entities_bulk,
    _replace_entity,
    _update_entity_embeddings_if_current,
)
from sibyl_core.services.graph_entity_work_items import _EntityWorkItemManager
from sibyl_core.services.graph_records import _entity_from_row, entity_from_surreal_row


class EntityManager(_EntityWorkItemManager):
    supports_bounded_entity_list = True
    supports_lightweight_entity_list = True

    async def create_direct_if_absent(self, entity: Entity) -> tuple[Entity, bool]:
        """Insert once, returning the stored row and whether this call created it."""
        row, created = await _insert_entity_if_absent(
            self._client,
            entity,
            group_id=self._group_id,
        )
        return _entity_from_row(row), created

    async def create_direct(self, entity: Entity, *, generate_embedding: bool = False) -> str:
        if generate_embedding:
            entity = await _entity_with_native_embedding(entity, self._embedding_provider)
        await _replace_entity(self._client, entity, group_id=self._group_id)
        return entity.id

    async def create_direct_bulk(
        self,
        entities: Sequence[Entity],
        *,
        generate_embeddings: bool = False,
        embedding_batch_size: int = 64,
        write_batch_size: int = 128,
    ) -> list[str]:
        prepared_entities = await self.prepare_entities_for_write(
            entities,
            generate_embeddings=generate_embeddings,
            embedding_batch_size=embedding_batch_size,
        )
        if not prepared_entities:
            return []

        created_ids: list[str] = []
        batch_size = max(int(write_batch_size), 1)
        for index in range(0, len(prepared_entities), batch_size):
            batch = prepared_entities[index : index + batch_size]
            await _replace_entities_bulk(self._client, batch, group_id=self._group_id)
            created_ids.extend(entity.id for entity in batch)
        return created_ids

    async def prepare_entities_for_write(
        self,
        entities: Sequence[Entity],
        *,
        generate_embeddings: bool = False,
        embedding_batch_size: int = 64,
    ) -> list[Entity]:
        prepared_entities = list(entities)
        if generate_embeddings:
            prepared_entities = await _entities_with_native_embeddings(
                prepared_entities,
                self._embedding_provider,
                batch_size=embedding_batch_size,
            )
        return prepared_entities

    async def backfill_embeddings_if_current(
        self,
        entities: Sequence[Entity],
        *,
        embedding_batch_size: int = 64,
    ) -> list[str]:
        async def load_matching_entity(expected: Entity) -> Entity | None:
            row = await _select_one(
                self._client,
                """
                SELECT * FROM entity
                WHERE group_id = $group_id AND uuid = $uuid
                LIMIT 1;
                """,
                group_id=self._group_id,
                uuid=expected.id,
            )
            if row is None:
                return None
            hydrated = entity_from_surreal_row(row)
            hydrated_text = entity_embedding_text(hydrated)
            if hydrated_text not in {
                entity_embedding_text(expected),
                _persisted_entity_embedding_text(expected),
            }:
                return None
            return hydrated.model_copy(
                update={
                    "name": str(row.get("name") or ""),
                    "description": str(row.get("description") or ""),
                    "content": str(row.get("content") or ""),
                }
            )

        loaded = await asyncio.gather(*(load_matching_entity(entity) for entity in entities))
        current_entities = [entity for entity in loaded if entity is not None]
        provider_metadata = (
            self._embedding_provider.metadata.to_dict() if self._embedding_provider else None
        )
        ready_ids = {
            entity.id
            for entity in current_entities
            if entity.embedding
            and (
                provider_metadata is None
                or entity.metadata.get("embedding_metadata") == provider_metadata
            )
        }
        pending_entities = [
            entity.model_copy(update={"embedding": None})
            for entity in current_entities
            if entity.id not in ready_ids
        ]
        prepared = await self.prepare_entities_for_write(
            pending_entities,
            generate_embeddings=True,
            embedding_batch_size=embedding_batch_size,
        )
        written_ids: set[str] = set()
        batch_size = max(int(embedding_batch_size), 1)
        for index in range(0, len(prepared), batch_size):
            written_ids.update(
                await _update_entity_embeddings_if_current(
                    self._client,
                    prepared[index : index + batch_size],
                    group_id=self._group_id,
                )
            )
        ready_ids.update(written_ids)
        return [entity.id for entity in current_entities if entity.id in ready_ids]

    async def create(self, entity: Entity) -> str:
        return await self.create_direct(entity, generate_embedding=True)

    async def delete(self, entity_id: str) -> bool:
        rows = await _execute_graph_transaction(
            self._client,
            """
            BEGIN TRANSACTION;
            DELETE FROM relates_to
            WHERE group_id = $group_id
              AND (source_id = $uuid OR target_id = $uuid)
            RETURN BEFORE;
            DELETE FROM mentions
            WHERE group_id = $group_id
              AND (source_id = $uuid OR target_id = $uuid)
            RETURN BEFORE;
            DELETE FROM entity
            WHERE group_id = $group_id AND uuid = $uuid
            RETURN BEFORE;
            COMMIT TRANSACTION;
            """,
            group_id=self._group_id,
            uuid=entity_id,
        )
        return any(row.get("uuid") == entity_id for row in rows)

    async def update(
        self,
        entity_id: str,
        updates: dict[str, Any],
        *,
        expected_revision: int | None = None,
        replace_metadata_keys: Sequence[str] = (),
    ) -> Entity | None:
        if not updates:
            return await self.get(entity_id)
        if expected_revision is not None and expected_revision < 1:
            raise ValueError("expected_revision must be at least 1")

        patch = _entity_update_patch(updates, updated_at=datetime.now(UTC))
        # A patch removes a key by writing it as NONE, which Surreal drops, so on
        # a pre-flattening row the snapshot would answer for the empty slot and
        # undo the removal. Healed before this write rather than after it.
        patch_attributes = patch.get("attributes")
        if isinstance(patch_attributes, Mapping):
            await _heal_metadata_snapshots_for_write(
                self._client,
                {entity_id: patch_attributes},
                group_id=self._group_id,
            )
        mirror_content = "description" in updates and "content" not in updates
        rows = await _execute_graph_transaction(
            self._client,
            """
                BEGIN TRANSACTION;
                LET $updated = (
                    UPDATE entity MERGE $patch
                    WHERE group_id = $group_id
                        AND uuid = $uuid
                        AND ($expected_revision = NONE OR revision = $expected_revision)
                    RETURN AFTER
                );
                UPDATE $updated SET
                    attributes = IF $metadata_replacements = {} THEN
                        attributes
                    ELSE
                        object::from_entries(array::concat(
                            object::entries(attributes ?? {}),
                            object::entries($metadata_replacements)
                        ))
                    END,
                    summary = IF description != NONE AND description != '' THEN
                        string::slice(description, 0, 500)
                    ELSE
                        name
                    END,
                    content = IF $mirror_content
                        AND entity_type IN $content_mirror_types THEN
                        description
                    ELSE
                        content
                    END,
                    revision += 1
                RETURN AFTER;
                COMMIT TRANSACTION;
            """,
            group_id=self._group_id,
            uuid=entity_id,
            patch=patch,
            expected_revision=expected_revision,
            mirror_content=mirror_content,
            content_mirror_types=list(_CONTENT_MIRRORS_DESCRIPTION_TYPES),
            metadata_replacements={
                key: patch_attributes[key]
                for key in replace_metadata_keys
                if isinstance(patch_attributes, Mapping) and key in patch_attributes
            },
        )
        if not rows and expected_revision is not None:
            try:
                current = await self.get(entity_id)
            except KeyError:
                return None
            raise RevisionConflictError(
                entity_id,
                expected_revision,
                current.revision,
            )
        if not rows:
            return None
        return _entity_from_row(rows[0])


__all__ = ["EntityManager"]
