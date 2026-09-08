"""Wide source frontiers remain fully validated through indexed lookup batches."""

from contextlib import asynccontextmanager
from uuid import uuid4

import pytest

from sibyl_core.backends.surreal import SurrealContentClient
from sibyl_core.backends.surreal.content_schema import bootstrap_content_schema
from sibyl_core.services import content_client, content_raw_persistence
from sibyl_core.services.content_models import RawMemoryWrite, raw_memory_recallable


@pytest.mark.parametrize("missing_tail", [False, True])
async def test_admission_validates_every_source_batch(monkeypatch, missing_tail):
    client = SurrealContentClient(url="memory://")
    try:
        await bootstrap_content_schema(client, reset=True)

        @asynccontextmanager
        async def session():
            yield client

        monkeypatch.setattr(content_client, "surreal_content_client", session)
        org = str(uuid4())
        sources = await content_raw_persistence.remember_raw_memories(
            [
                RawMemoryWrite(
                    organization_id=org,
                    principal_id="owner",
                    source_id=f"source-{index}",
                    raw_content=f"Evidence {index}",
                )
                for index in range(content_client.DEFAULT_BATCH_SIZE + 1)
            ],
            embedding_provider=None,
        )
        source_ids = [source.id for source in sources]
        if missing_tail:
            # This lexical suffix falls in the second batch.
            source_ids.append("zz-missing-source")
        original_select = content_client.select_many
        batches = []

        async def observe(client, query, **params):
            if "source_ids" in params:
                batches.append(list(params["source_ids"]))
                assert len(params["source_ids"]) <= content_client.DEFAULT_BATCH_SIZE
            return await original_select(client, query, **params)

        monkeypatch.setattr(content_client, "select_many", observe)
        child = await content_raw_persistence.remember_raw_memory(
            organization_id=org,
            principal_id="owner",
            source_id="derived",
            raw_content="Derived instructions",
            metadata={"raw_source_ids": source_ids},
            embedding_provider=None,
        )
        assert len(batches) == 2
        assert {value for batch in batches for value in batch} == set(source_ids)
        assert child.metadata["source_validation_pending"] is missing_tail
        assert raw_memory_recallable(child) is not missing_tail
        assert set(child.metadata["correction_blockers"]) == {source.id for source in sources}
    finally:
        await client.close()
