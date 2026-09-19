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
        org: {"checked": 2, "recovered": 2, "pending": 0, "failed": 0}
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
