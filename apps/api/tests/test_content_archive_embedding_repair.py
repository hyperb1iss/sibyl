"""A successful capture restore repairs the restored organizations' raw embeddings."""

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from sibyl.persistence import content_archive
from sibyl.persistence.content_archive import restore_content_archive_payload
from sibyl_core.backends.surreal import SurrealContentClient
from sibyl_core.backends.surreal.content_schema import EMBEDDING_DIM
from sibyl_core.embeddings.providers import DeterministicEmbeddingProvider, EmbeddingMetadata
from sibyl_core.services import content_client
from sibyl_core.services.content_models import raw_memory_embedding_metadata
from tests.test_surreal_content_persistence import (
    _normalize_records,
    surreal_content_client as surreal_content_client,  # noqa: PLC0414
)


def _payload(org: str, *, rows: int = 1) -> dict[str, object]:
    captures = [
        {
            "id": str(uuid4()),
            "organization_id": org,
            "source_id": f"source-{index}",
            "principal_id": "user-restored",
            "title": f"restored {index}",
            "raw_content": f"restored {index}",
        }
        for index in range(rows)
    ]
    return {
        "version": "1.0",
        "created_at": "2026-06-01T00:00:00+00:00",
        "organization_id": org,
        "tables": {"raw_captures": captures, "system_settings": []},
        "row_counts": {"raw_captures": len(captures), "system_settings": 0},
        "total_rows": len(captures),
    }


async def _restore(client: SurrealContentClient, payload: dict[str, object]):
    with (
        patch.object(client, "close", AsyncMock()),
        patch(
            "sibyl.persistence.content_archive.build_surreal_content_client",
            return_value=client,
        ),
    ):
        return await restore_content_archive_payload(payload, clean=True)


@pytest.mark.asyncio
async def test_restore_embeds_the_restored_captures_on_its_own_client(
    surreal_content_client: SurrealContentClient, monkeypatch
) -> None:
    """The repair runs through the restore's client, not a shared one pointed elsewhere."""
    org = str(uuid4())
    provider = DeterministicEmbeddingProvider(
        EmbeddingMetadata(
            provider="deterministic",
            model="restore-repair",
            dimensions=EMBEDDING_DIM,
            cache_namespace="restore-repair",
            tokenizer_estimate_method="utf8-byte-length",
        )
    )
    monkeypatch.setattr(
        "sibyl_core.services.content_models.configured_raw_memory_embedding_provider",
        lambda: provider,
    )
    elsewhere = AsyncMock(side_effect=AssertionError("shared client must not be used"))
    monkeypatch.setattr(content_client, "surreal_content_client", elsewhere)
    payload = _payload(org, rows=2)

    result = await _restore(surreal_content_client, payload)

    assert result.success is True
    assert result.rows_restored == 2
    assert result.embedding_repair == {
        org: {
            "checked": 2,
            "recovered": 2,
            "pending": 0,
            "failed": 0,
            "status": "completed",
            "provider_dimensions": None,
            "schema_dimensions": None,
            "refused": 0,
            "deferred": 0,
            "cursor": "",
        }
    }
    elsewhere.assert_not_called()
    rows = _normalize_records(
        await surreal_content_client.execute_query(
            "SELECT uuid, revision, embedding, metadata.embedding_metadata AS embedding_metadata "
            "FROM raw_captures WHERE organization_id = $organization_id;",
            organization_id=org,
        )
    )
    assert len(rows) == 2
    for row in rows:
        assert len(row["embedding"]) == EMBEDDING_DIM
        assert row["embedding_metadata"] == raw_memory_embedding_metadata(provider.metadata)
        assert row["revision"] == 1


@pytest.mark.asyncio
async def test_restore_survives_a_failed_embedding_repair(
    surreal_content_client: SurrealContentClient, monkeypatch
) -> None:
    org = str(uuid4())
    monkeypatch.setattr(
        "sibyl_core.services.content_raw_embedding_repair.repair_raw_capture_embeddings",
        AsyncMock(side_effect=RuntimeError("embedding provider unavailable")),
    )

    result = await _restore(surreal_content_client, _payload(org))

    assert result.success is True
    assert result.errors == []
    assert result.embedding_repair == {org: {"error": "RuntimeError"}}


def test_restored_organizations_follow_the_archive_scope() -> None:
    tables = {
        "raw_captures": [
            {"organization_id": "a"},
            {"organization_id": "b"},
            {"organization_id": " a "},
            {"organization_id": ""},
            "not a row",
        ]
    }
    assert content_archive._restored_capture_organizations(tables, None) == ["a", "b"]
    assert content_archive._restored_capture_organizations(tables, ["b"]) == ["b"]
    assert content_archive._restored_capture_organizations({}, ["b"]) == []


@pytest.mark.asyncio
async def test_restored_chunk_vectors_keep_their_model_or_arrive_unverified(
    surreal_content_client: SurrealContentClient,
) -> None:
    from sibyl_core.embeddings.provenance import (
        UNVERIFIED_ORIGIN_ARCHIVE,
        document_chunk_embedding_metadata,
        unverified_embedding_metadata,
    )

    org = str(uuid4())
    stamp = document_chunk_embedding_metadata(
        provider="deterministic", model="chunks", dimensions=EMBEDDING_DIM
    )
    vector = [1.0, *([0.0] * (EMBEDDING_DIM - 1))]
    chunks = [
        {"id": "stamped", "embedding": vector, "embedding_metadata": stamp},
        {"id": "legacy", "embedding": vector},
        {"id": "lexical"},
    ]
    payload = _payload(org)
    payload["tables"]["document_chunks"] = [
        {
            **chunk,
            "organization_id": org,
            "source_id": "source",
            "document_id": f"doc-{chunk['id']}",
            "chunk_index": 0,
            "content": f"body {chunk['id']}",
        }
        for chunk in chunks
    ]
    await surreal_content_client.execute_query(
        "CREATE embedding_states:sprior SET organization_id = $org, plane = 'document_chunks', "
        "complete_metadata = $stamp, complete_at = time::now();",
        org=org,
        stamp=stamp,
    )

    result = await _restore(surreal_content_client, payload)

    assert result.success is True, result.errors
    rows = _normalize_records(
        await surreal_content_client.execute_query(
            "SELECT uuid, embedding_metadata FROM document_chunks WHERE organization_id = $org;",
            org=org,
        )
    )
    stamps = {row["uuid"]: row.get("embedding_metadata") for row in rows}
    assert stamps == {
        "stamped": stamp,
        "legacy": unverified_embedding_metadata(UNVERIFIED_ORIGIN_ARCHIVE),
        "lexical": None,
    }
    state = _normalize_records(
        await surreal_content_client.execute_query(
            "SELECT complete_metadata, generation FROM embedding_states:sprior;"
        )
    )
    # The restore sends the sweep back over a plane that had finished.
    assert state[0].get("complete_metadata") is None
    assert state[0]["generation"] == 1


@pytest.mark.asyncio
async def test_restore_repairs_raw_captures_without_settling_any_verdict(
    surreal_content_client: SurrealContentClient, monkeypatch
) -> None:
    """The verdicts weigh stamps photographed at upgrade, so restamping cannot sway them.

    Settling a verdict here would also decide it without the graph plane's
    evidence, which only lifecycle repair gathers.
    """
    from sibyl_core.projection.repair import LifecycleRepairResult
    from sibyl_core.services.document_embedding_sweep import DOCUMENT_CHUNK_EMBEDDING_PLANE
    from sibyl_core.services.embedding_sweep import read_embedding_sweep_state

    org = str(uuid4())
    decide = AsyncMock()
    repair = AsyncMock(return_value=LifecycleRepairResult())
    monkeypatch.setattr(
        "sibyl_core.services.document_embedding_sweep.decide_document_chunk_legacy_vectors",
        decide,
    )
    monkeypatch.setattr(
        "sibyl_core.services.content_raw_embedding_repair.repair_raw_capture_embeddings", repair
    )

    result = await _restore(surreal_content_client, _payload(org))

    assert result.success is True
    decide.assert_not_awaited()
    repair.assert_awaited_once()
    state = await read_embedding_sweep_state(
        DOCUMENT_CHUNK_EMBEDDING_PLANE,
        org,
        lambda query, **params: content_client.select_many(surreal_content_client, query, **params),
    )
    assert not state.get("legacy_decision")
