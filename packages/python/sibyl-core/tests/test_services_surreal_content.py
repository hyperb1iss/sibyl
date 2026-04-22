"""Tests for Surreal-backed core content helpers."""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from sibyl_core.services.surreal_content import (
    get_or_create_source,
    list_unlinked_document_chunks,
)


def _query_result(records: list[dict[str, object]]) -> list[dict[str, object]]:
    return [{"status": "OK", "result": records}]


class FakeClient:
    def __init__(self, responses: list[object]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def execute_query(self, query: str, params: dict[str, object] | None = None, **kwargs: object) -> object:
        merged = dict(params or {})
        merged.update(kwargs)
        self.calls.append((query, merged))
        return self._responses.pop(0)

    async def close(self) -> None:
        return None


class TestSurrealContentHelpers:
    @pytest.mark.asyncio
    async def test_get_or_create_source_returns_existing_record(self) -> None:
        fake_client = FakeClient(
            [
                _query_result(
                    [
                        {
                            "uuid": "src-1",
                            "organization_id": "org-1",
                            "name": "Docs",
                            "url": "https://docs.example.com",
                            "source_type": "website",
                            "crawl_status": "completed",
                        }
                    ]
                )
            ]
        )

        @asynccontextmanager
        async def fake_session():
            yield fake_client

        from sibyl_core.services import surreal_content as content_service

        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(content_service, "surreal_content_client", fake_session)
            source, created = await get_or_create_source(
                "https://docs.example.com",
                2,
                {},
                organization_id="org-1",
            )

        assert created is False
        assert source.id == "src-1"
        assert source.organization_id == "org-1"
        assert fake_client.calls[0][1] == {
            "organization_id": "org-1",
            "url": "https://docs.example.com",
        }

    @pytest.mark.asyncio
    async def test_list_unlinked_document_chunks_filters_linked_rows(self) -> None:
        fake_client = FakeClient(
            [
                _query_result(
                    [
                        {
                            "uuid": "src-1",
                            "organization_id": "org-1",
                            "name": "Docs",
                            "url": "https://docs.example.com",
                        }
                    ]
                ),
                _query_result(
                    [
                        {
                            "uuid": "doc-1",
                            "source_id": "src-1",
                            "url": "https://docs.example.com/guide",
                            "title": "Guide",
                        }
                    ]
                ),
                _query_result(
                    [
                        {
                            "uuid": "chunk-1",
                            "document_id": "doc-1",
                            "chunk_index": 0,
                            "chunk_type": "text",
                            "content": "unlinked chunk",
                            "has_entities": False,
                        },
                        {
                            "uuid": "chunk-2",
                            "document_id": "doc-1",
                            "chunk_index": 1,
                            "chunk_type": "text",
                            "content": "linked chunk",
                            "has_entities": True,
                        },
                    ]
                ),
            ]
        )

        @asynccontextmanager
        async def fake_session():
            yield fake_client

        from sibyl_core.services import surreal_content as content_service

        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(content_service, "surreal_content_client", fake_session)
            chunks = await list_unlinked_document_chunks(
                organization_id="org-1",
                source_id="src-1",
                limit=10,
            )

        assert [chunk.id for chunk in chunks] == ["chunk-1"]
