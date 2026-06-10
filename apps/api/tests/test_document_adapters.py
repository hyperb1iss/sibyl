from __future__ import annotations

import gzip
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from sibyl.persistence.content_common import CrawledDocumentRecord
from sibyl.services import document_adapters
from sibyl.services.document_adapters import (
    DOCUMENT_FILE_ADAPTER_NAME,
    DOCUMENT_FOLDER_ADAPTER_NAME,
    DOCUMENT_TEXT_ADAPTER_NAME,
    DOCUMENT_URL_ADAPTER_NAME,
    DocumentFileAdapter,
    DocumentFolderAdapter,
    DocumentTextAdapter,
    DocumentUrlAdapter,
    ensure_document_adapters_registered,
)
from sibyl_core.models.sources import SourcePrivacyClass, SourceTransformBehavior
from sibyl_core.services.source_adapters import clear_source_adapters, get_source_adapter


@pytest.fixture(autouse=True)
def _clear_registry() -> Iterator[None]:
    clear_source_adapters()
    yield
    clear_source_adapters()


@pytest.mark.asyncio
async def test_document_file_adapter_emits_project_normalized_record(tmp_path: Path) -> None:
    source = tmp_path / "guide.md"
    source.write_text("# Guide\n\nUse the good path.\n", encoding="utf-8")
    adapter = DocumentFileAdapter()

    manifest = await adapter.prepare_manifest(
        source_uri=str(source),
        options={"target_scope_key": "project_123", "collection": "docs"},
    )
    batch = await anext(adapter.iter_records(manifest, batch_size=10))

    assert manifest.adapter_name == DOCUMENT_FILE_ADAPTER_NAME
    assert manifest.target_memory_scope == "project"
    assert manifest.target_scope_key == "project_123"
    assert manifest.privacy_class is SourcePrivacyClass.PROJECT
    record = batch.records[0]
    assert record.source_type == "document"
    assert record.transform_behavior is SourceTransformBehavior.NORMALIZED
    assert record.privacy_class is SourcePrivacyClass.PROJECT
    assert record.title == "Guide"
    assert "Use the good path" in record.body
    assert record.metadata["collection"] == "docs"
    assert "collection:docs" in record.labels


@pytest.mark.asyncio
async def test_document_folder_adapter_uses_local_globbing_and_checkpoints(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sibyl.crawler.local.settings.source_import_dir", tmp_path)
    (tmp_path / "a.md").write_text("# A\n\nalpha\n", encoding="utf-8")
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "b.md").write_text("# B\n\nbeta\n", encoding="utf-8")
    adapter = DocumentFolderAdapter()

    manifest = await adapter.prepare_manifest(
        source_uri=str(tmp_path),
        options={"target_scope_key": "project_123"},
    )
    first = await anext(adapter.iter_records(manifest, batch_size=1))
    second = await anext(adapter.iter_records(manifest, checkpoint=first.checkpoint, batch_size=1))

    assert manifest.adapter_name == DOCUMENT_FOLDER_ADAPTER_NAME
    assert first.checkpoint.cursor == "1"
    assert first.checkpoint.done is False
    assert [record.adapter_record_id for record in first.records] == ["a.md"]
    assert second.records[0].adapter_record_id == "nested/b.md"
    assert second.checkpoint.done is True


@pytest.mark.asyncio
async def test_document_text_adapter_uses_text_hash_identity() -> None:
    adapter = DocumentTextAdapter()

    manifest = await adapter.prepare_manifest(
        source_uri="text://paste",
        options={
            "target_scope_key": "project_123",
            "title": "Architecture note",
            "text": "The graph remembers provenance.",
        },
    )
    batch = await anext(adapter.iter_records(manifest, batch_size=10))

    assert manifest.adapter_name == DOCUMENT_TEXT_ADAPTER_NAME
    assert manifest.source_identity.startswith("text:")
    assert manifest.source_version.startswith("text:sha256:")
    assert batch.records[0].title == "Architecture note"
    assert batch.records[0].body == "The graph remembers provenance."


@pytest.mark.asyncio
async def test_document_url_adapter_fetches_single_page() -> None:
    seen: list[str] = []
    long_path = "a" * 520

    async def fetch(url: str) -> CrawledDocumentRecord:
        seen.append(url)
        return CrawledDocumentRecord(
            source_id="url-source",
            url=url,
            title="URL docs",
            content="Fetched markdown",
            raw_content="<main>Fetched markdown</main>",
            content_hash="hash-url",
            word_count=2,
        )

    adapter = DocumentUrlAdapter(fetcher=fetch)

    manifest = await adapter.prepare_manifest(
        source_uri=f"https://docs.example.com/{long_path}",
        options={"target_scope_key": "project_123", "collection": "web"},
    )
    batch = await anext(adapter.iter_records(manifest, batch_size=10))

    assert seen == [f"https://docs.example.com/{long_path}"]
    assert manifest.adapter_name == DOCUMENT_URL_ADAPTER_NAME
    assert batch.records[0].source_uri == f"https://docs.example.com/{long_path}"
    assert len(batch.records[0].adapter_record_id) <= 500
    assert batch.records[0].metadata["collection"] == "web"


@pytest.mark.asyncio
async def test_document_url_adapter_canonicalizes_url_identity() -> None:
    adapter = DocumentUrlAdapter(fetcher=AsyncMock())

    first = await adapter.prepare_manifest(
        source_uri="HTTPS://Docs.Example.COM:443/path/#section",
        options={"target_scope_key": "project_123"},
    )
    second = await adapter.prepare_manifest(
        source_uri="https://docs.example.com/path",
        options={"target_scope_key": "project_123"},
    )

    assert first.source_uri == "https://docs.example.com/path"
    assert first.source_identity == second.source_identity
    assert first.source_version == second.source_version


@pytest.mark.asyncio
async def test_document_url_adapter_rejects_private_hosts_by_default() -> None:
    adapter = DocumentUrlAdapter(fetcher=AsyncMock())

    with pytest.raises(ValueError, match="private"):
        await adapter.prepare_manifest(
            source_uri="http://127.0.0.1:3337/docs",
            options={"target_scope_key": "project_123"},
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source_uri",
    [
        "http://169.254.169.254/latest/meta-data",
        "http://2130706433/docs",
        "http://0x7f000001/docs",
        "http://017700000001/docs",
        "http://[::1]/docs",
    ],
)
async def test_document_url_adapter_rejects_encoded_private_hosts(source_uri: str) -> None:
    adapter = DocumentUrlAdapter(fetcher=AsyncMock())

    with pytest.raises(ValueError, match="private"):
        await adapter.prepare_manifest(
            source_uri=source_uri,
            options={"target_scope_key": "project_123"},
        )


@pytest.mark.asyncio
async def test_document_url_safe_fetch_blocks_redirect_to_private_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_fetch_once(
        url: str,
        *,
        allow_private_network: bool,
    ) -> document_adapters._FetchedDocumentPage:
        assert allow_private_network is False
        return document_adapters._FetchedDocumentPage(
            url=url,
            status_code=302,
            headers={"location": "http://169.254.169.254/latest/meta-data"},
            body=b"",
        )

    monkeypatch.setattr(
        document_adapters,
        "_resolve_document_host_addresses",
        lambda host: ["93.184.216.34"] if host == "docs.example.com" else [],
    )
    monkeypatch.setattr(document_adapters, "_fetch_document_url_once", fake_fetch_once)

    with pytest.raises(ValueError, match="private"):
        await document_adapters._safe_fetch_document_url(
            "https://docs.example.com/start",
            allow_private_network=False,
        )


@pytest.mark.asyncio
async def test_document_url_safe_fetch_pins_validated_public_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, str]] = []

    async def fake_http1_fetch_document_url(
        **kwargs: object,
    ) -> document_adapters._FetchedDocumentPage:
        seen.append((str(kwargs["url"]), str(kwargs["connect_host"])))
        return document_adapters._FetchedDocumentPage(
            url=str(kwargs["url"]),
            status_code=200,
            headers={"content-type": "text/plain"},
            body=b"ok",
        )

    monkeypatch.setattr(
        document_adapters,
        "_resolve_document_host_addresses",
        lambda host: ["93.184.216.34"] if host == "docs.example.com" else [],
    )
    monkeypatch.setattr(
        document_adapters,
        "_http1_fetch_document_url",
        fake_http1_fetch_document_url,
    )

    page = await document_adapters._safe_fetch_document_url(
        "https://docs.example.com/start",
        allow_private_network=False,
    )

    assert page.body == b"ok"
    assert seen == [("https://docs.example.com/start", "93.184.216.34")]


def test_document_url_decode_rejects_oversized_compressed_body() -> None:
    body = gzip.compress(b"x" * (document_adapters._DOCUMENT_URL_MAX_BYTES + 1))

    with pytest.raises(ValueError, match="too large"):
        document_adapters._decode_document_body(body, {"content-encoding": "gzip"})


@pytest.mark.asyncio
async def test_document_url_adapter_allows_private_hosts_with_explicit_option() -> None:
    adapter = DocumentUrlAdapter(fetcher=AsyncMock())

    manifest = await adapter.prepare_manifest(
        source_uri="http://127.0.0.1:3337/docs/",
        options={"target_scope_key": "project_123", "allow_private_network": True},
    )

    assert manifest.source_uri == "http://127.0.0.1:3337/docs"


def test_ensure_document_adapters_registered() -> None:
    ensure_document_adapters_registered()

    assert get_source_adapter(DOCUMENT_FILE_ADAPTER_NAME).descriptor.source_type == "document"
    assert get_source_adapter(DOCUMENT_FOLDER_ADAPTER_NAME).descriptor.source_type == "document"
    assert get_source_adapter(DOCUMENT_URL_ADAPTER_NAME).descriptor.source_type == "document"
    assert get_source_adapter(DOCUMENT_TEXT_ADAPTER_NAME).descriptor.source_type == "document"
