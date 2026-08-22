"""Entity persistence, metadata healing, and write serialization."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from sibyl_core.backends.surreal.connection import _is_transient_connection_error
from sibyl_core.backends.surreal.schema import EMBEDDING_DIM
from sibyl_core.embeddings.providers import entity_embedding_text
from sibyl_core.memory_pipeline.retrieval_keys import coerce_retrieval_keys
from sibyl_core.models.entities import Entity, EntityType
from sibyl_core.services.graph_client import (
    SurrealGraphClient,
    mark_graph_schema_dirty,
    prepare_graph_schema,
)
from sibyl_core.services.graph_common import (
    SurrealRecord,
)
from sibyl_core.services.graph_common import (
    normalize_graph_records as normalize_records,
)
from sibyl_core.services.graph_common import (
    select_one as _select_one,
)
from sibyl_core.services.graph_records import (
    _entity_metadata,
    _first_content,
    _jsonable,
    _metadata_datetime,
    _metadata_int,
    _metadata_optional_int,
    _metadata_str,
    _metadata_str_list,
    _snapshot_without_owned_keys,
)

# Work-item models invert the usual relationship between the two text columns:
# their ``set_entity_fields`` validators seed ``content`` FROM ``description``
# at construction, so ``description`` is the authored text and ``content`` is
# its mirror. Everywhere else ``content`` is authored and ``description`` is a
# derived blurb, which is why the mirror is type-scoped rather than universal.
_CONTENT_MIRRORS_DESCRIPTION_TYPES = (
    EntityType.TASK.value,
    EntityType.PROJECT.value,
    EntityType.EPIC.value,
    EntityType.TEAM.value,
    EntityType.MILESTONE.value,
)

# Mirrors MAX_CONTENT_LENGTH in tools/helpers.py and EntityCreate.content in the
# API schema. Declared in three places and, until now, enforced in two.
MAX_ENTITY_CONTENT_CHARS = 50_000
CLEAR_MEMORY_SCOPE = "__clear_memory_scope__"
# Every value that means "remove this key" rather than "set it to this". A plain
# None is the usual spelling; a promoted column cannot use it, because Surreal
# reads an absent key and a null one as the same NONE and the upsert's
# preserve-on-absence would keep the old value, so those carry a sentinel the
# statement converts. Anything that teaches the write path a new way to say
# "clear" belongs here, or the snapshot fold stops recognizing it as a removal.
CLEAR_SENTINEL_VALUES = frozenset({CLEAR_MEMORY_SCOPE})
# Bounded because contention on one row is the signal, not something to wait out:
# a row this contended is being rewritten anyway, and looping forever inside a
# write path would hold the caller's own write open indefinitely.
_MAX_SNAPSHOT_HEAL_ATTEMPTS = 5

_ENTITY_BULK_UPSERT_QUERY = f"""
INSERT INTO entity $rows ON DUPLICATE KEY UPDATE
    uuid = $input.uuid,
    name = $input.name,
    entity_type = $input.entity_type,
    summary = $input.summary,
    description = $input.description,
    content = $input.content,
    labels = $input.labels,
    -- Absence means "this write does not speak to that key", the same rule
    -- memory_scope and retrieval_keys below already spell out for themselves.
    -- Assigning the input bag wholesale made every write a full replace, so a
    -- reprojection or restore that rebuilt an Entity from partial knowledge
    -- silently dropped every key it had never heard of; three fields were
    -- rescued from that one at a time, after each one lost data in production.
    -- Merging generalizes the rescue to the whole bag, and it is order-free:
    -- two writers touching disjoint keys both land whichever way they race.
    -- Removing a key is the update path's job, where writing it as NONE clears
    -- the slot and no later write puts it back.
    attributes = object::from_entries(array::concat(
        object::entries(attributes ?? {{}}),
        object::entries($input.attributes)
    )),
    attributes.memory_scope = IF $input.memory_scope = '{CLEAR_MEMORY_SCOPE}' {{ NONE }}
        ELSE {{ $input.memory_scope ?? memory_scope }},
    attributes.last_recalled_at = $input.last_recalled_at ?? last_recalled_at,
    attributes.last_used_at = $input.last_used_at ?? last_used_at,
    attributes.retrieval_count = $input.retrieval_count ?? retrieval_count ?? 0,
    attributes.citation_count = $input.citation_count ?? citation_count ?? 0,
    attributes.misled_count = $input.misled_count ?? misled_count ?? 0,
    group_id = $input.group_id,
    created_at = $input.created_at,
    updated_at = $input.updated_at,
    created_by = created_by ?? $input.created_by,
    modified_by = $input.modified_by ?? modified_by,
    revision = (revision ?? 0) + 1,
    last_recalled_at = $input.last_recalled_at ?? last_recalled_at,
    last_used_at = $input.last_used_at ?? last_used_at,
    retrieval_count = $input.retrieval_count ?? retrieval_count ?? 0,
    citation_count = $input.citation_count ?? citation_count ?? 0,
    misled_count = $input.misled_count ?? misled_count ?? 0,
    project_id = $input.project_id,
    -- Absent means "this write does not speak to scope", which is the common
    -- case: a write is a full replace and most callers rebuild an Entity
    -- without carrying the scope forward. Overwriting on absence let any
    -- reprojection or restore silently unscope a row into the read path's
    -- fail-open. A caller that means "no scope" sends CLEAR_MEMORY_SCOPE.
    memory_scope = IF $input.memory_scope = '{CLEAR_MEMORY_SCOPE}' {{ NONE }}
        ELSE {{ $input.memory_scope ?? memory_scope }},
    -- Same absence rule as memory_scope, for the same reason: a write is a full
    -- replace, and a reprojection or restore that rebuilds an Entity without
    -- the keys must not strip the writer's exact-match declaration off the row.
    retrieval_keys = $input.retrieval_keys ?? retrieval_keys,
    retrieval_keys_normalized = $input.retrieval_keys_normalized ?? retrieval_keys_normalized,
    epic_id = $input.epic_id,
    parent_task_id = $input.parent_task_id,
    task_id = $input.task_id,
    status = $input.status,
    priority = $input.priority,
    complexity = $input.complexity,
    feature = $input.feature,
    tags = $input.tags,
    name_embedding = $input.name_embedding;
"""


def _enforce_entity_content_limit(entities: Sequence[Entity]) -> None:
    """Refuse to store a body past the cap every other layer already declares.

    EntityCreate.content and the add tool both cap content at 50,000
    characters, but anything constructing an Entity directly reached the table
    without passing either, so the limit was advisory. The live graph holds a
    520KB pasted terminal transcript as a result.

    Rejecting rather than truncating is the point: a silently shortened body is
    indistinguishable from a short one, and the cost of an outsized row is not
    just storage. Its embedding is computed over text the embedder may itself
    truncate without saying so, and BM25 length normalization is skewed by
    outliers, which the A1 work identified as the mechanism behind selection
    dilution -- one half-megabyte row can distort ranking corpus-wide.

    Deliberately not enforced in ``_entity_record``: migrations reshape rows
    that already exist, including legacy oversized ones, and must stay able to
    move them.
    """
    oversized = [
        f"{entity.id} ({len(entity.content or ''):,} chars)"
        for entity in entities
        if len(entity.content or "") > MAX_ENTITY_CONTENT_CHARS
    ]
    if oversized:
        msg = (
            f"entity content exceeds the {MAX_ENTITY_CONTENT_CHARS:,} character "
            f"limit: {', '.join(oversized)}. Split the body or store it as a "
            f"document rather than a single entity."
        )
        raise ValueError(msg)


async def _replace_entity(
    client: SurrealGraphClient,
    entity: Entity,
    *,
    group_id: str,
) -> SurrealRecord:
    _enforce_entity_content_limit([entity])
    record = _entity_record(entity, group_id=group_id)
    await heal_entity_metadata_snapshots(client, [record], group_id=group_id)
    try:
        result = await _execute_replace_entities_with_schema_retry(client, [record])
    except Exception as exc:
        if not _is_transient_connection_error(exc):
            raise
        mark_graph_schema_dirty(client.group_id)
        await prepare_graph_schema(client)
        result = await _execute_replace_entities_with_schema_retry(client, [record])
    rows = normalize_records(result)
    if rows:
        return rows[0]
    stored = await _select_one(
        client, "SELECT * FROM entity WHERE uuid = $uuid LIMIT 1;", uuid=entity.id
    )
    if stored is None:
        raise RuntimeError(f"failed to persist entity {entity.id}")
    return stored


async def _replace_entities_bulk(
    client: SurrealGraphClient,
    entities: Sequence[Entity],
    *,
    group_id: str,
) -> list[SurrealRecord]:
    _enforce_entity_content_limit(entities)
    records = [_entity_record(entity, group_id=group_id) for entity in entities]
    if not records:
        return []
    await heal_entity_metadata_snapshots(client, records, group_id=group_id)
    try:
        result = await _execute_replace_entities_with_schema_retry(client, records)
    except Exception as exc:
        if not _is_transient_connection_error(exc):
            raise
        mark_graph_schema_dirty(client.group_id)
        await prepare_graph_schema(client)
        result = await _execute_replace_entities_with_schema_retry(client, records)
    return normalize_records(result)


async def heal_entity_metadata_snapshots(
    client: SurrealGraphClient,
    records: Sequence[SurrealRecord],
    *,
    group_id: str,
) -> None:
    """Normalize any pre-flattening row these records are about to overwrite.

    Every path that writes entity rows calls this first, including the two
    migrations that drive the canonical upsert themselves. Those are the writers
    that clear keys on rollback, so they are the ones that most need it.
    """
    await _heal_metadata_snapshots_for_write(
        client,
        {
            uuid: attributes
            for record in records
            if (uuid := str(record.get("uuid") or ""))
            and isinstance(attributes := record.get("attributes"), Mapping)
        },
        group_id=group_id,
    )


async def _heal_metadata_snapshots_for_write(
    client: SurrealGraphClient,
    payloads: Mapping[str, Mapping[str, object]],
    *,
    group_id: str,
) -> None:
    """Fold a pre-flattening row's snapshot into its flattened bag, before the write.

    A row written before the flattened bag existed carries its metadata only as
    the JSON snapshot, so the read merges one where it finds one. That merge is
    also how a removal gets undone: Surreal drops a field written as NONE, and
    the snapshot then answers for the empty slot, so a write that clears a key
    on such a row reads back unchanged.

    Clearing the snapshot alone would delete whatever lives only inside it, and
    SurrealQL cannot parse JSON (``type::object``, ``parse::json``, and the
    object cast all reject a string), so the fold cannot be expressed inside the
    caller's statement.

    It runs as its own fenced write beforehand instead. That ordering is what
    makes it safe: folding the snapshot down and dropping it changes nothing a
    reader can observe, since those values are exactly what the read was already
    merging, so no window between this and the caller's write can show anything
    new. What the caller's write then finds is an ordinary flattened row, where
    a removal is just a removal.

    Only a write that clears something can be undone by a snapshot, so only
    those rows are probed, and ordinary writes cost nothing.
    """
    uuids = [uuid for uuid, payload in payloads.items() if uuid and _clears_a_key(payload)]
    if not uuids:
        return
    carriers = await _rows_with_metadata_snapshots(client, uuids, group_id=group_id)
    for uuid in carriers:
        await _heal_one_metadata_snapshot(client, uuid, group_id=group_id)


def _clears_a_key(payload: Mapping[str, object]) -> bool:
    """Whether this payload removes a key rather than only setting keys.

    Both spellings count. Most removals are a plain None, but a column promoted
    out of the bag cannot say it that way: an absent key and a null one are the
    same value to Surreal, and both mean "this write does not speak to it", so
    those keys carry a sentinel the upsert turns into NONE. A probe that only
    knew about None saw the sentinel as an ordinary string and skipped the row.
    """
    return any(
        value is None or (isinstance(value, str) and value in CLEAR_SENTINEL_VALUES)
        for value in payload.values()
    )


async def _heal_one_metadata_snapshot(
    client: SurrealGraphClient,
    uuid: str,
    *,
    group_id: str,
) -> None:
    """Fold one row's snapshot into its flattened bag under an optimistic fence.

    Read, fold, write is not atomic, and the values being written are by
    definition stale: they came off a snapshot of the row's pre-flattening past.
    Without a fence a writer landing between the read and the write had its
    newer value overwritten by that stale one. Every real writer bumps
    ``revision``, so the write refuses to land unless the row is still the one
    that was read, and a refusal means re-reading and folding again, by which
    point the newer value sits in the flattened bag and is no longer folded at
    all.
    """
    for _ in range(_MAX_SNAPSHOT_HEAL_ATTEMPTS):
        row = await _select_one(
            client,
            """
            SELECT uuid, revision, attributes
            FROM entity
            WHERE group_id = $group_id AND uuid = $uuid
            LIMIT 1;
            """,
            group_id=group_id,
            uuid=uuid,
        )
        if row is None:
            return
        attributes = row.get("attributes")
        if not isinstance(attributes, Mapping):
            return
        snapshot = _parsed_metadata_snapshot(attributes.get("metadata"))
        if snapshot is None:
            return
        patch: dict[str, object] = {
            key: value
            for key, value in _snapshot_without_owned_keys(snapshot).items()
            if key not in attributes
        }
        # None drops the snapshot the same way any other removal drops a key.
        patch["metadata"] = None
        applied = normalize_records(
            await client.execute_query(
                """
                UPDATE entity MERGE { attributes: $patch }
                WHERE group_id = $group_id
                    AND uuid = $uuid
                    AND revision = $seen_revision
                RETURN AFTER;
                """,
                group_id=group_id,
                uuid=uuid,
                patch=patch,
                seen_revision=row.get("revision"),
            )
        )
        if applied:
            return
    raise RuntimeError(
        f"metadata snapshot for {uuid} could not be folded under contention "
        f"after {_MAX_SNAPSHOT_HEAL_ATTEMPTS} attempts; the write was not attempted"
    )


async def _rows_with_metadata_snapshots(
    client: SurrealGraphClient,
    uuids: Sequence[str],
    *,
    group_id: str,
) -> dict[str, tuple[Mapping[str, object], dict[str, object]]]:
    """The rows among ``uuids`` still carrying a JSON metadata snapshot.

    Probed in two passes because the first one is the one that always runs: it
    projects the snapshot field alone, so the common answer (nobody has one)
    costs a handful of indexed lookups returning almost nothing. Only a row that
    really is pre-flattening pays for its full attributes bag, and only until it
    is healed.
    """
    probed = normalize_records(
        await client.execute_query(
            # `uuid IN $list` is never index-served, so each uuid gets its own
            # indexed lookup. The closure body may reference nothing but its own
            # argument: any other binding silently evaluates to nothing on at
            # least one engine, so the group guard is applied to the returned
            # rows instead (uuid is unique table-wide).
            """
            RETURN $uuids.map(|$u|
                (SELECT uuid, group_id, attributes.metadata AS snapshot FROM entity
                 WHERE uuid = $u LIMIT 1)[0]
            );
            """,
            uuids=list(dict.fromkeys(uuids)),
        )
    )
    carriers = [
        uuid
        for row in probed
        if str(row.get("group_id") or "") == group_id
        and _parsed_metadata_snapshot(row.get("snapshot")) is not None
        and (uuid := str(row.get("uuid") or ""))
    ]
    if not carriers:
        return {}
    hydrated = normalize_records(
        await client.execute_query(
            """
            RETURN $uuids.map(|$u|
                (SELECT uuid, group_id, attributes FROM entity
                 WHERE uuid = $u LIMIT 1)[0]
            );
            """,
            uuids=carriers,
        )
    )
    resolved: dict[str, tuple[Mapping[str, object], dict[str, object]]] = {}
    for row in hydrated:
        uuid = str(row.get("uuid") or "")
        attributes = row.get("attributes")
        if not uuid or str(row.get("group_id") or "") != group_id:
            continue
        if not isinstance(attributes, Mapping):
            continue
        snapshot = _parsed_metadata_snapshot(attributes.get("metadata"))
        if snapshot is None:
            continue
        resolved[uuid] = (attributes, snapshot)
    return resolved


def _parsed_metadata_snapshot(raw: object) -> dict[str, object] | None:
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    if isinstance(raw, Mapping):
        return {str(key): value for key, value in raw.items()}
    return None


_ENTITY_EMBEDDING_BACKFILL_QUERY = f"""
UPDATE (
    SELECT VALUE id
    FROM entity
    WHERE group_id = $group_id AND uuid IN $uuids
) SET
    name_embedding = <array<float, {EMBEDDING_DIM}>>$rows_by_uuid[uuid].name_embedding,
    attributes.embedding_metadata = $rows_by_uuid[uuid].embedding_metadata,
    attributes.updated_at = $rows_by_uuid[uuid].updated_at,
    updated_at = $rows_by_uuid[uuid].updated_at,
    revision = (revision ?? 0) + 1
WHERE group_id = $group_id
  AND entity_type = $rows_by_uuid[uuid].entity_type
  AND name = $rows_by_uuid[uuid].name
  AND description = $rows_by_uuid[uuid].description
  AND content = $rows_by_uuid[uuid].content
  AND (attributes.summary ?? '') = $rows_by_uuid[uuid].summary
RETURN AFTER;
"""


async def _update_entity_embeddings_if_current(
    client: SurrealGraphClient,
    entities: Sequence[Entity],
    *,
    group_id: str,
) -> set[str]:
    rows = [
        {
            "uuid": entity.id,
            "entity_type": entity.entity_type.value,
            "name": entity.name,
            "description": entity.description or "",
            "content": entity.content or "",
            "summary": str(entity.metadata.get("summary") or ""),
            "name_embedding": entity.embedding,
            "embedding_metadata": entity.metadata.get("embedding_metadata"),
            "updated_at": datetime.now(UTC),
        }
        for entity in entities
        if entity.embedding
    ]
    if not rows:
        return set()
    rows_by_uuid = {str(row["uuid"]): row for row in rows}
    rows = normalize_records(
        await client.execute_query(
            # Each row is still addressed through idx_entity_uuid and fenced
            # atomically against its current text. Sending the batch as one
            # query keeps HNSW writes on one database request instead of
            # flooding every pool socket with an independent indexed UPDATE.
            _ENTITY_EMBEDDING_BACKFILL_QUERY,
            group_id=group_id,
            uuids=list(rows_by_uuid),
            rows_by_uuid=rows_by_uuid,
        )
    )
    return {str(row["uuid"]) for row in rows if str(row.get("uuid") or "")}


def _persisted_entity_embedding_text(entity: Entity) -> str:
    # Mirrors entity_from_surreal_row's read-side fallbacks; any divergence
    # makes the currency fence refuse rows that are byte-identical on disk.
    summary = entity.description[:500] if entity.description else entity.name
    persisted = entity.model_copy(
        update={
            "name": entity.name.strip(),
            "description": (entity.description or summary).strip(),
            "content": _first_content(entity.content, entity.metadata.get("content"), summary),
        }
    )
    return entity_embedding_text(persisted)


async def _execute_replace_entity_query(
    client: SurrealGraphClient,
    record: SurrealRecord,
) -> object:
    return await _execute_replace_entities_bulk_query(client, [record])


async def _execute_replace_entities_bulk_query(
    client: SurrealGraphClient,
    records: Sequence[SurrealRecord],
) -> object:
    return await client.execute_query(_ENTITY_BULK_UPSERT_QUERY, rows=list(records))


async def _execute_replace_entities_with_schema_retry(
    client: SurrealGraphClient,
    records: Sequence[SurrealRecord],
) -> object:
    try:
        return await _execute_replace_entities_bulk_query(client, records)
    except Exception as exc:
        if not _is_legacy_updated_at_string_schema_error(exc):
            raise
        legacy_records = _records_with_legacy_updated_at_strings(records)
        return await _execute_replace_entities_bulk_query(client, legacy_records)


def _is_legacy_updated_at_string_schema_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    return "coerce value for field `updated_at`" in message and (
        "expected `none | string`" in message or "expected none | string" in message
    )


def _records_with_legacy_updated_at_strings(
    records: Sequence[SurrealRecord],
) -> list[SurrealRecord]:
    converted: list[SurrealRecord] = []
    for record in records:
        patched = dict(record)
        patched["updated_at"] = _legacy_updated_at_value(patched.get("updated_at"))
        attributes = patched.get("attributes")
        if isinstance(attributes, dict):
            patched_attributes = dict(attributes)
            patched_attributes["updated_at"] = _legacy_updated_at_value(
                patched_attributes.get("updated_at")
            )
            patched["attributes"] = patched_attributes
        converted.append(patched)
    return converted


def _legacy_updated_at_value(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _entity_record(
    entity: Entity,
    *,
    group_id: str,
    canonicalize_parent_task_id: bool = True,
) -> SurrealRecord:
    metadata = _entity_metadata(entity)
    now = datetime.now(UTC)
    updated_at = _metadata_datetime(metadata.get("updated_at")) or entity.updated_at or now
    created_at = entity.created_at or now
    project_id = _metadata_str(metadata, "project_id")
    # Promoted from metadata like the other denormalized columns. The scope is
    # already stamped into attributes at capture; the column is what lets a
    # query filter on it and what makes its absence a fact rather than a guess.
    #
    # The upsert preserves the stored scope when this is None, because a write
    # is a full replace and most callers rebuild an Entity without carrying the
    # scope forward -- a reprojection or restore would otherwise silently
    # unscope a row and hand it to the read path's fail-open. A caller that
    # genuinely means "no scope" says so with CLEAR_MEMORY_SCOPE, which reaches
    # the query as a value and overwrites rather than being skipped.
    memory_scope = _metadata_str(metadata, "memory_scope")
    # The writer's exact-match declaration, promoted for the same reason the
    # scope is: only a column can carry the index the exact-match arm reads.
    # Coerced rather than validated here because this is the storage edge and a
    # single malformed key must not fail an otherwise valid write; the surfaces
    # that accept keys from a caller validate strictly instead.
    retrieval_keys = coerce_retrieval_keys(metadata.get("retrieval_keys"))
    # A key written as None means "remove it", the same as anywhere else in the
    # bag. Promotion would otherwise swallow that: the coercion returns None for
    # a removal and for a write that never mentioned keys alike, the columns get
    # skipped, and the upsert's preserve-on-absence keeps the retired key
    # exact-matching. Empty lists say the removal out loud in column form.
    clears_retrieval_keys = retrieval_keys is None and metadata.get("retrieval_keys", ...) is None
    epic_id = _metadata_str(metadata, "epic_id")
    parent_task_id = _metadata_str(metadata, "parent_task_id")
    if canonicalize_parent_task_id and not parent_task_id and entity.entity_type == EntityType.TASK:
        parent_task_id = epic_id
    task_id = _metadata_str(metadata, "task_id")
    status = _metadata_str(metadata, "status")
    priority = _metadata_str(metadata, "priority")
    complexity = _metadata_str(metadata, "complexity")
    feature = _metadata_str(metadata, "feature")
    tags = _metadata_str_list(metadata.get("tags"))
    last_recalled_at = _metadata_datetime(metadata.get("last_recalled_at"))
    last_used_at = _metadata_datetime(metadata.get("last_used_at"))
    retrieval_count = _metadata_optional_int(metadata.get("retrieval_count"))
    citation_count = _metadata_optional_int(metadata.get("citation_count"))
    misled_count = _metadata_optional_int(metadata.get("misled_count"))
    # No ``metadata`` snapshot beside the flattened bag. Both copies came from
    # this same dict, so the snapshot never held anything the flattened keys did
    # not, while an update merges into the flattened copy alone and cannot reach
    # a JSON string. That left the snapshot a frozen picture of pre-update state
    # whose only observable effect was resurrecting keys an update removed:
    # Surreal drops a field written as NONE, so removal empties the flattened
    # slot and the read then filled it back in from the stale picture.
    attributes: dict[str, object] = {
        **metadata,
        "description": entity.description or "",
        "source_file": entity.source_file or "",
        "updated_at": updated_at,
        "_direct_insert": True,
        "entity_type": entity.entity_type.value,
    }
    record: SurrealRecord = {
        "uuid": entity.id,
        "name": entity.name,
        "entity_type": entity.entity_type.value,
        "summary": entity.description[:500] if entity.description else entity.name,
        "description": entity.description or "",
        "content": entity.content or "",
        "labels": [entity.entity_type.value, "Entity"],
        "attributes": attributes,
        "group_id": group_id,
        "created_at": created_at,
        "updated_at": updated_at,
        "created_by": entity.created_by,
        "modified_by": entity.modified_by,
        "revision": entity.revision,
        "project_id": project_id,
        "memory_scope": memory_scope,
        "epic_id": epic_id,
        "parent_task_id": parent_task_id,
        "task_id": task_id,
        "status": status,
        "priority": priority,
        "complexity": complexity,
        "feature": feature,
        "tags": tags,
        "name_embedding": entity.embedding,
    }
    # Absent rather than NONE when the entity declares no keys, so a namespace
    # that has not reached graph schema v18 still accepts an ordinary write.
    if retrieval_keys is not None:
        record["retrieval_keys"] = retrieval_keys[0]
        record["retrieval_keys_normalized"] = retrieval_keys[1]
    elif clears_retrieval_keys:
        record["retrieval_keys"] = []
        record["retrieval_keys_normalized"] = []
    if last_recalled_at is not None:
        record["last_recalled_at"] = last_recalled_at
    if last_used_at is not None:
        record["last_used_at"] = last_used_at
    if retrieval_count is not None:
        record["retrieval_count"] = retrieval_count
    if citation_count is not None:
        record["citation_count"] = citation_count
    if misled_count is not None:
        record["misled_count"] = misled_count
    return record


def _entity_update_patch(updates: Mapping[str, Any], *, updated_at: datetime) -> SurrealRecord:
    metadata_patch = _entity_update_metadata_patch(updates)
    attributes_patch: dict[str, object] = {
        **metadata_patch,
        "updated_at": updated_at,
        "_direct_insert": True,
    }
    patch: SurrealRecord = {
        "updated_at": updated_at,
        "attributes": attributes_patch,
    }

    name = updates.get("name") or updates.get("title")
    if name:
        patch["name"] = str(name)
    if "description" in updates:
        description = str(updates.get("description") or "")
        patch["description"] = description
        attributes_patch["description"] = description
    if "content" in updates:
        patch["content"] = str(updates.get("content") or "")
    if source_file := updates.get("source_file"):
        source_file_text = str(source_file)
        patch["source_file"] = source_file_text
        attributes_patch["source_file"] = source_file_text
    elif "source_file" in updates:
        patch["source_file"] = None
        attributes_patch["source_file"] = ""
    if modified_by := updates.get("modified_by"):
        patch["modified_by"] = str(modified_by)
    if "embedding" in updates:
        embedding = updates.get("embedding")
        patch["name_embedding"] = embedding if isinstance(embedding, list) else None

    for key in (
        "project_id",
        "memory_scope",
        "epic_id",
        "parent_task_id",
        "task_id",
        "status",
        "priority",
        "complexity",
        "feature",
        "last_recalled_at",
        "last_used_at",
        "retrieval_count",
        "citation_count",
        "misled_count",
    ):
        if key in metadata_patch and key in {"last_recalled_at", "last_used_at"}:
            value = _metadata_datetime(metadata_patch.get(key))
            patch[key] = value
            attributes_patch[key] = value
        elif key in metadata_patch and key in {
            "retrieval_count",
            "citation_count",
            "misled_count",
        }:
            value = _metadata_int(metadata_patch.get(key))
            patch[key] = value
            attributes_patch[key] = value
        elif key in metadata_patch:
            value = _metadata_str(metadata_patch, key)
            patch[key] = value
            attributes_patch[key] = value
    if "tags" in metadata_patch:
        tags = _metadata_str_list(metadata_patch.get("tags")) or []
        patch["tags"] = tags
        attributes_patch["tags"] = tags
    if "retrieval_keys" in metadata_patch:
        # Naming the field in an update IS the explicit statement the bulk
        # upsert's absence rule refuses to infer, so an empty list clears.
        display, match = coerce_retrieval_keys(metadata_patch.get("retrieval_keys")) or ([], [])
        patch["retrieval_keys"] = display
        patch["retrieval_keys_normalized"] = match
        attributes_patch["retrieval_keys"] = display
    return patch


def _entity_update_metadata_patch(updates: Mapping[str, Any]) -> dict[str, object]:
    metadata: dict[str, object] = {}
    update_metadata = updates.get("metadata")
    if isinstance(update_metadata, Mapping):
        metadata.update({str(key): _jsonable(value) for key, value in update_metadata.items()})

    excluded_keys = {
        "content",
        "description",
        "embedding",
        "metadata",
        "name",
        "source_file",
        "title",
    }
    metadata.update(
        {str(key): _jsonable(value) for key, value in updates.items() if key not in excluded_keys}
    )
    return metadata


__all__ = ["CLEAR_MEMORY_SCOPE", "MAX_ENTITY_CONTENT_CHARS", "heal_entity_metadata_snapshots"]
