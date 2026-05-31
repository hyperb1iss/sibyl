"""Document source adapters for raw-memory imports."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from hashlib import sha256
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import urlparse, urlunsplit
from uuid import UUID

from sibyl.crawler.local import LocalFileCrawler
from sibyl.crawler.service import CrawlerService
from sibyl.ingestion.parser import MarkdownParser
from sibyl.persistence.content_common import CrawledDocumentRecord, CrawlSourceRecord
from sibyl_core.models.sources import (
    SourceAdapterCapability,
    SourceAdapterDescriptor,
    SourceImportCheckpoint,
    SourceImportManifest,
    SourcePrivacyClass,
    SourceRecord,
    SourceRecordBatch,
    SourceTransformBehavior,
    SourceType,
)
from sibyl_core.services.source_adapters import (
    build_source_content_hash,
    build_source_dedupe_key,
    build_source_record_id,
    register_source_adapter,
    source_adapter_registry,
)

DOCUMENT_FILE_ADAPTER_NAME = "document_file"
DOCUMENT_FOLDER_ADAPTER_NAME = "document_folder"
DOCUMENT_URL_ADAPTER_NAME = "document_url"
DOCUMENT_TEXT_ADAPTER_NAME = "document_text"
DOCUMENT_ADAPTER_VERSION = "1.0"
DOCUMENT_DEFAULT_SCOPE = "project"
DOCUMENT_METADATA_SCHEMA = {
    "collection": "string",
    "document_kind": "string",
    "document_url": "string",
    "heading_count": "number",
    "source_path": "string",
}
_DOCUMENT_ORGANIZATION_ID = UUID("00000000-0000-0000-0000-000000000000")
_MARKDOWN_SUFFIXES = {".md", ".markdown", ".mdx", ".template"}
_LOCAL_CRAWLER_SUFFIXES = {".md", ".template"}

type DocumentFetcher = Callable[[str], Awaitable[CrawledDocumentRecord]]


class DocumentFileAdapter:
    descriptor = SourceAdapterDescriptor(
        name=DOCUMENT_FILE_ADAPTER_NAME,
        version=DOCUMENT_ADAPTER_VERSION,
        source_type="document",
        display_name="Document file",
        capabilities=[
            SourceAdapterCapability.CHECKPOINTS,
            SourceAdapterCapability.SKIPPED_RECORDS,
        ],
        default_privacy_class=SourcePrivacyClass.PROJECT,
        transform_behavior=SourceTransformBehavior.NORMALIZED,
        metadata_schema=DOCUMENT_METADATA_SCHEMA,
        supports_incremental=True,
    )

    async def prepare_manifest(
        self,
        *,
        source_uri: str,
        options: Mapping[str, object] | None = None,
    ) -> SourceImportManifest:
        path = _resolve_document_path(source_uri, allow_file=True, allow_dir=False)
        option_values = dict(options or {})
        return _manifest_for_document_source(
            adapter=self.descriptor,
            source_identity=str(option_values.get("source_identity") or path),
            source_uri=str(path),
            source_version=_files_version((path,), root=path.parent),
            options=option_values,
            metadata={"source_path": str(path), "document_kind": "file"},
        )

    def iter_records(
        self,
        manifest: SourceImportManifest,
        *,
        checkpoint: SourceImportCheckpoint | None = None,
        batch_size: int = 100,
    ) -> AsyncIterator[SourceRecordBatch]:
        return _iter_records(
            manifest,
            checkpoint=checkpoint,
            batch_size=batch_size,
            loader=_load_file_records,
        )


class DocumentFolderAdapter:
    descriptor = SourceAdapterDescriptor(
        name=DOCUMENT_FOLDER_ADAPTER_NAME,
        version=DOCUMENT_ADAPTER_VERSION,
        source_type="document",
        display_name="Document folder",
        capabilities=[
            SourceAdapterCapability.CHECKPOINTS,
            SourceAdapterCapability.SKIPPED_RECORDS,
        ],
        default_privacy_class=SourcePrivacyClass.PROJECT,
        transform_behavior=SourceTransformBehavior.NORMALIZED,
        metadata_schema=DOCUMENT_METADATA_SCHEMA,
        supports_incremental=True,
    )

    async def prepare_manifest(
        self,
        *,
        source_uri: str,
        options: Mapping[str, object] | None = None,
    ) -> SourceImportManifest:
        path = _resolve_document_path(source_uri, allow_file=False, allow_dir=True)
        files = _folder_files(path)
        option_values = dict(options or {})
        return _manifest_for_document_source(
            adapter=self.descriptor,
            source_identity=str(option_values.get("source_identity") or path),
            source_uri=str(path),
            source_version=_files_version(files, root=path),
            options=option_values,
            metadata={
                "document_kind": "folder",
                "file_count": len(files),
                "source_path": str(path),
            },
        )

    def iter_records(
        self,
        manifest: SourceImportManifest,
        *,
        checkpoint: SourceImportCheckpoint | None = None,
        batch_size: int = 100,
    ) -> AsyncIterator[SourceRecordBatch]:
        return _iter_records(
            manifest,
            checkpoint=checkpoint,
            batch_size=batch_size,
            loader=_load_folder_records,
        )


class DocumentUrlAdapter:
    descriptor = SourceAdapterDescriptor(
        name=DOCUMENT_URL_ADAPTER_NAME,
        version=DOCUMENT_ADAPTER_VERSION,
        source_type="document",
        display_name="Document URL",
        capabilities=[SourceAdapterCapability.CHECKPOINTS],
        default_privacy_class=SourcePrivacyClass.PROJECT,
        transform_behavior=SourceTransformBehavior.NORMALIZED,
        metadata_schema=DOCUMENT_METADATA_SCHEMA,
        supports_incremental=False,
    )

    def __init__(self, fetcher: DocumentFetcher | None = None) -> None:
        self._fetcher = fetcher or _fetch_url_document

    async def prepare_manifest(
        self,
        *,
        source_uri: str,
        options: Mapping[str, object] | None = None,
    ) -> SourceImportManifest:
        option_values = dict(options or {})
        url = _normalize_document_url(
            source_uri,
            allow_private_network=_bool_option(option_values.get("allow_private_network")),
        )
        return _manifest_for_document_source(
            adapter=self.descriptor,
            source_identity=str(option_values.get("source_identity") or url),
            source_uri=url,
            source_version=f"url:{sha256(url.encode()).hexdigest()}",
            options=option_values,
            metadata={"document_kind": "url", "source_url": url},
        )

    def iter_records(
        self,
        manifest: SourceImportManifest,
        *,
        checkpoint: SourceImportCheckpoint | None = None,
        batch_size: int = 100,
    ) -> AsyncIterator[SourceRecordBatch]:
        return _iter_records(
            manifest,
            checkpoint=checkpoint,
            batch_size=batch_size,
            loader=lambda current_manifest: _load_url_records(current_manifest, self._fetcher),
        )


class DocumentTextAdapter:
    descriptor = SourceAdapterDescriptor(
        name=DOCUMENT_TEXT_ADAPTER_NAME,
        version=DOCUMENT_ADAPTER_VERSION,
        source_type="document",
        display_name="Document text",
        capabilities=[SourceAdapterCapability.CHECKPOINTS],
        default_privacy_class=SourcePrivacyClass.PROJECT,
        transform_behavior=SourceTransformBehavior.NORMALIZED,
        metadata_schema=DOCUMENT_METADATA_SCHEMA,
        supports_incremental=False,
    )

    async def prepare_manifest(
        self,
        *,
        source_uri: str,
        options: Mapping[str, object] | None = None,
    ) -> SourceImportManifest:
        option_values = dict(options or {})
        text = _required_text_option(option_values)
        text_hash = sha256(text.encode()).hexdigest()
        source_identity = str(option_values.get("source_identity") or f"text:{text_hash}")
        return _manifest_for_document_source(
            adapter=self.descriptor,
            source_identity=source_identity,
            source_uri=source_uri or source_identity,
            source_version=f"text:sha256:{text_hash}",
            options=option_values,
            metadata={"document_kind": "text", "text_hash": text_hash},
        )

    def iter_records(
        self,
        manifest: SourceImportManifest,
        *,
        checkpoint: SourceImportCheckpoint | None = None,
        batch_size: int = 100,
    ) -> AsyncIterator[SourceRecordBatch]:
        return _iter_records(
            manifest,
            checkpoint=checkpoint,
            batch_size=batch_size,
            loader=_load_text_records,
        )


def ensure_document_adapters_registered() -> None:
    if not source_adapter_registry.has(DOCUMENT_FILE_ADAPTER_NAME):
        register_source_adapter(DocumentFileAdapter())
    if not source_adapter_registry.has(DOCUMENT_FOLDER_ADAPTER_NAME):
        register_source_adapter(DocumentFolderAdapter())
    if not source_adapter_registry.has(DOCUMENT_URL_ADAPTER_NAME):
        register_source_adapter(DocumentUrlAdapter())
    if not source_adapter_registry.has(DOCUMENT_TEXT_ADAPTER_NAME):
        register_source_adapter(DocumentTextAdapter())


async def _iter_records(
    manifest: SourceImportManifest,
    *,
    checkpoint: SourceImportCheckpoint | None,
    batch_size: int,
    loader: Callable[[SourceImportManifest], Awaitable[tuple[SourceRecord, ...]]],
) -> AsyncIterator[SourceRecordBatch]:
    if (
        checkpoint
        and checkpoint.source_version
        and checkpoint.source_version != manifest.source_version
    ):
        msg = "source_import_checkpoint_source_version_mismatch"
        raise ValueError(msg)
    records = await loader(manifest)
    start = int(checkpoint.cursor) if checkpoint and checkpoint.cursor else 0
    batch_records = list(records[start : start + batch_size])
    cursor = start + len(batch_records)
    done = cursor >= len(records)
    if batch_records or start < len(records):
        yield SourceRecordBatch(
            records=batch_records,
            checkpoint=SourceImportCheckpoint(
                cursor=str(cursor) if not done else None,
                source_version=manifest.source_version,
                records_seen=cursor,
                records_imported=len(batch_records),
                done=done,
                metadata={"source_uri": manifest.source_uri},
            ),
        )


async def _load_file_records(manifest: SourceImportManifest) -> tuple[SourceRecord, ...]:
    path = _resolve_document_path(str(manifest.source_uri), allow_file=True, allow_dir=False)
    document = _document_from_file(path)
    return (_record_from_document(manifest, document, adapter_record_id=path.name),)


async def _load_folder_records(manifest: SourceImportManifest) -> tuple[SourceRecord, ...]:
    root = _resolve_document_path(str(manifest.source_uri), allow_file=False, allow_dir=True)
    source = _document_crawl_source(root, manifest)
    crawler = LocalFileCrawler()
    records: list[SourceRecord] = []
    async for document in crawler.crawl_source(source, max_pages=_max_pages(manifest.options)):
        document_path = _path_from_file_url(document.url)
        adapter_record_id = _relative_file_key(document_path, root)
        records.append(
            _record_from_document(manifest, document, adapter_record_id=adapter_record_id)
        )
    return tuple(records)


async def _load_url_records(
    manifest: SourceImportManifest,
    fetcher: DocumentFetcher,
) -> tuple[SourceRecord, ...]:
    url = _normalize_document_url(
        str(manifest.source_uri or manifest.source_identity),
        allow_private_network=_bool_option(manifest.options.get("allow_private_network")),
    )
    document = await fetcher(url)
    return (_record_from_document(manifest, document, adapter_record_id=url),)


async def _load_text_records(manifest: SourceImportManifest) -> tuple[SourceRecord, ...]:
    text = _required_text_option(manifest.options)
    title = str(manifest.options.get("title") or "Pasted document")
    text_hash = sha256(text.encode()).hexdigest()
    document = CrawledDocumentRecord(
        source_id=build_source_record_id(manifest=manifest, adapter_record_id=text_hash),
        organization_id=_DOCUMENT_ORGANIZATION_ID,
        url=str(manifest.source_uri or manifest.source_identity),
        title=title,
        raw_content=text,
        content=text,
        content_hash=text_hash,
        word_count=len(text.split()),
    )
    return (_record_from_document(manifest, document, adapter_record_id=text_hash),)


def _manifest_for_document_source(
    *,
    adapter: SourceAdapterDescriptor,
    source_identity: str,
    source_uri: str,
    source_version: str,
    options: Mapping[str, object],
    metadata: Mapping[str, object],
) -> SourceImportManifest:
    return SourceImportManifest(
        adapter_name=adapter.name,
        adapter_version=adapter.version,
        source_identity=_bounded_identifier(source_identity),
        source_uri=source_uri,
        source_version=source_version,
        target_memory_scope=str(options.get("target_memory_scope") or DOCUMENT_DEFAULT_SCOPE),
        target_scope_key=_optional_str(options.get("target_scope_key")),
        privacy_class=adapter.default_privacy_class,
        transform_behavior=adapter.transform_behavior,
        metadata_schema=dict(adapter.metadata_schema),
        metadata={**dict(metadata), "collection": _optional_str(options.get("collection"))},
        options=dict(options),
    )


def _document_from_file(path: Path) -> CrawledDocumentRecord:
    if path.suffix.lower() in _MARKDOWN_SUFFIXES:
        parsed = MarkdownParser().parse_file(path)
        content = parsed.raw_content
        title = parsed.title or path.stem
        headings = [section.title for section in parsed.all_sections_flat if section.title]
        code_languages = [
            block.language
            for section in parsed.all_sections_flat
            for block in section.code_blocks
            if block.language
        ]
        word_count = parsed.word_count
    else:
        content = path.read_text(encoding="utf-8")
        title = path.stem
        headings = []
        code_languages = []
        word_count = len(content.split())
    return CrawledDocumentRecord(
        source_id=sha256(str(path).encode()).hexdigest(),
        organization_id=_DOCUMENT_ORGANIZATION_ID,
        url=f"file://{path}",
        title=title,
        raw_content=content,
        content=content,
        content_hash=sha256(content.encode()).hexdigest(),
        section_path=list(path.parent.parts),
        word_count=word_count,
        headings=headings,
        code_languages=sorted(set(code_languages)),
        has_code=bool(code_languages),
    )


def _record_from_document(
    manifest: SourceImportManifest,
    document: CrawledDocumentRecord,
    *,
    adapter_record_id: str,
) -> SourceRecord:
    adapter_record_id = _bounded_identifier(adapter_record_id)
    body = document.content or document.raw_content
    content_hash = document.content_hash or build_source_content_hash(body)
    dedupe_key = build_source_dedupe_key(
        manifest=manifest,
        adapter_record_id=adapter_record_id,
        content_hash=content_hash,
    )
    collection = _optional_str(manifest.options.get("collection"))
    labels = ["document"]
    if collection:
        labels.append(f"collection:{collection}")
    metadata = {
        "collection": collection,
        "content_hash": content_hash,
        "document_url": document.url,
        "heading_count": len(document.headings),
        "headings": list(document.headings[:50]),
        "has_code": document.has_code,
        "source_path": _source_path_metadata(document.url),
        "token_count": document.token_count,
        "word_count": document.word_count,
    }
    if document.code_languages:
        metadata["code_languages"] = list(document.code_languages)
    if document.parent_url:
        metadata["parent_url"] = document.parent_url
    return SourceRecord(
        adapter_record_id=adapter_record_id,
        source_id=build_source_record_id(
            manifest=manifest,
            adapter_record_id=adapter_record_id,
        ),
        source_type="document",
        source_uri=document.url,
        source_version=manifest.source_version,
        title=document.title,
        body=body,
        content_hash=content_hash,
        dedupe_key=dedupe_key.value,
        privacy_class=SourcePrivacyClass.PROJECT,
        transform_behavior=SourceTransformBehavior.NORMALIZED,
        transform_version=manifest.adapter_version,
        labels=labels,
        metadata=metadata,
    )


async def _fetch_url_document(url: str) -> CrawledDocumentRecord:
    source = _document_crawl_source(url, None)
    async with CrawlerService() as crawler:
        result = await crawler.crawl_page(url)
        return crawler.result_to_document(result, source)


def _document_crawl_source(
    source: Path | str,
    manifest: SourceImportManifest | None,
) -> CrawlSourceRecord:
    options = manifest.options if manifest is not None else {}
    return CrawlSourceRecord(
        organization_id=_DOCUMENT_ORGANIZATION_ID,
        name=str(options.get("collection") or source),
        url=str(source),
        source_type=SourceType.LOCAL if isinstance(source, Path) else SourceType.WEBSITE,
        include_patterns=_string_list(options.get("include_patterns")),
        exclude_patterns=_string_list(options.get("exclude_patterns")),
    )


def _resolve_document_path(source_uri: str, *, allow_file: bool, allow_dir: bool) -> Path:
    raw_path = source_uri
    if source_uri.startswith("file://"):
        parsed = urlparse(source_uri)
        raw_path = parsed.path
    unresolved = Path(raw_path).expanduser()
    if unresolved.is_symlink():
        msg = f"Document source cannot be a symlink: {unresolved}"
        raise ValueError(msg)
    path = unresolved.resolve()
    if not path.exists():
        msg = f"Document source does not exist: {path}"
        raise FileNotFoundError(msg)
    if path.is_file() and not allow_file:
        msg = f"Document source must be a directory: {path}"
        raise ValueError(msg)
    if path.is_dir() and not allow_dir:
        msg = f"Document source must be a file: {path}"
        raise ValueError(msg)
    if not path.is_file() and not path.is_dir():
        msg = f"Document source is not a file or directory: {path}"
        raise ValueError(msg)
    return path


def _folder_files(path: Path) -> tuple[Path, ...]:
    for child in path.rglob("*"):
        if child.is_symlink():
            msg = f"Document source cannot include symlinked entries: {child}"
            raise ValueError(msg)
    files = [
        child.resolve()
        for child in sorted(path.rglob("*"))
        if child.is_file() and child.suffix.lower() in _LOCAL_CRAWLER_SUFFIXES
    ]
    if not files:
        msg = f"Document directory contains no supported files: {path}"
        raise ValueError(msg)
    return tuple(files)


def _files_version(files: Sequence[Path], *, root: Path) -> str:
    hasher = sha256()
    for file in sorted(files):
        file_key = _relative_file_key(file, root)
        stat = file.stat()
        for value in (file_key, str(stat.st_size), str(stat.st_mtime_ns)):
            hasher.update(value.encode())
            hasher.update(b"\0")
        with file.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                hasher.update(chunk)
        hasher.update(b"\0")
    return f"files:{len(files)}:sha256:{hasher.hexdigest()}"


def _relative_file_key(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.name


def _bounded_identifier(raw_value: str) -> str:
    if len(raw_value) <= 500:
        return raw_value
    digest = sha256(raw_value.encode()).hexdigest()
    readable = Path(raw_value).name[-96:] or raw_value[-96:]
    return f"{readable}:sha256:{digest}"


def _path_from_file_url(value: str) -> Path:
    return Path(value.removeprefix("file://")).resolve()


def _source_path_metadata(url: str) -> str | None:
    if not url.startswith("file://"):
        return None
    return str(_path_from_file_url(url))


def _normalize_document_url(source_uri: str, *, allow_private_network: bool = False) -> str:
    parsed = urlparse(source_uri.strip())
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not parsed.netloc:
        msg = f"Document URL must be http(s): {source_uri}"
        raise ValueError(msg)
    host = parsed.hostname
    if not host:
        msg = f"Document URL must include a host: {source_uri}"
        raise ValueError(msg)
    host = host.lower()
    try:
        port = parsed.port
    except ValueError as exc:
        msg = f"Document URL port is invalid: {source_uri}"
        raise ValueError(msg) from exc
    if not allow_private_network:
        _reject_private_document_host(host)
    netloc_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc_host = f"{netloc_host}:{port}"
    path = parsed.path or ""
    if path != "/":
        path = path.rstrip("/")
    return urlunsplit((scheme, netloc_host, path, parsed.query, ""))


def _reject_private_document_host(host: str) -> None:
    blocked_hostnames = {"localhost"}
    if host in blocked_hostnames or host.endswith(".localhost") or host.endswith(".local"):
        msg = f"Document URL host is private: {host}"
        raise ValueError(msg)
    try:
        address = ip_address(host.strip("[]"))
    except ValueError:
        return
    if (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_unspecified
        or address.is_reserved
    ):
        msg = f"Document URL host is private: {host}"
        raise ValueError(msg)


def _bool_option(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _required_text_option(options: Mapping[str, object]) -> str:
    text = str(options.get("text") or "")
    if not text.strip():
        msg = "Document text import requires non-empty text"
        raise ValueError(msg)
    return text


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _max_pages(options: Mapping[str, object]) -> int:
    value = options.get("max_pages")
    if value is None:
        return 100
    try:
        return max(1, int(str(value)))
    except (TypeError, ValueError):
        return 100


__all__ = [
    "DOCUMENT_ADAPTER_VERSION",
    "DOCUMENT_FILE_ADAPTER_NAME",
    "DOCUMENT_FOLDER_ADAPTER_NAME",
    "DOCUMENT_TEXT_ADAPTER_NAME",
    "DOCUMENT_URL_ADAPTER_NAME",
    "DocumentFileAdapter",
    "DocumentFolderAdapter",
    "DocumentTextAdapter",
    "DocumentUrlAdapter",
    "ensure_document_adapters_registered",
]
