"""Document source adapters for raw-memory imports."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from hashlib import sha256
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse
from uuid import UUID

from sibyl.crawler.local import LocalFileCrawler
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
from sibyl_core.network import (
    SAFE_FETCH_MAX_BYTES,
    decode_safe_fetch_body,
    normalize_safe_url,
    safe_fetch,
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
_DOCUMENT_URL_MAX_BYTES = SAFE_FETCH_MAX_BYTES

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
        self._fetcher = fetcher

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
    fetcher: DocumentFetcher | None,
) -> tuple[SourceRecord, ...]:
    allow_private_network = _bool_option(manifest.options.get("allow_private_network"))
    url = _normalize_document_url(
        str(manifest.source_uri or manifest.source_identity),
        allow_private_network=allow_private_network,
    )
    if fetcher is not None:
        document = await fetcher(url)
    else:
        document = await _fetch_url_document(
            url,
            allow_private_network=allow_private_network,
        )
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


async def _fetch_url_document(
    url: str,
    *,
    allow_private_network: bool = False,
) -> CrawledDocumentRecord:
    page = await safe_fetch(
        url,
        allow_private_network=allow_private_network,
        max_bytes=_DOCUMENT_URL_MAX_BYTES,
        user_agent="Sibyl document importer",
        accept="text/html,text/markdown,text/plain;q=0.9,*/*;q=0.1",
    )
    raw_content = _decode_document_body(page.body, page.headers)
    content, title, headings, links = _document_content_from_response(
        raw_content,
        page.url,
        page.headers,
    )
    content_hash = sha256(content.encode()).hexdigest()
    parsed = urlparse(page.url)
    depth = len([part for part in parsed.path.split("/") if part])
    word_count = len(content.split()) if content else 0
    return CrawledDocumentRecord(
        source_id=sha256(page.url.encode()).hexdigest(),
        organization_id=_DOCUMENT_ORGANIZATION_ID,
        url=page.url,
        title=title,
        raw_content=raw_content[:100000],
        content=content,
        content_hash=content_hash,
        depth=depth,
        word_count=word_count,
        token_count=word_count * 4 // 3,
        has_code=False,
        is_index=_is_index_document_url(page.url, content),
        headings=headings[:50],
        links=links[:200],
        code_languages=[],
        http_status=page.status_code,
    )


def _decode_document_body(body: bytes, headers: Mapping[str, str]) -> str:
    return decode_safe_fetch_body(body, headers, max_bytes=_DOCUMENT_URL_MAX_BYTES)


def _document_content_from_response(
    raw_content: str,
    url: str,
    headers: Mapping[str, str],
) -> tuple[str, str, list[str], list[str]]:
    content_type = headers.get("content-type", "").lower()
    if "html" not in content_type and not raw_content.lstrip().startswith("<"):
        title = _title_from_document_url(url)
        return raw_content, title, [], []

    extractor = _DocumentHtmlExtractor(base_url=url)
    extractor.feed(raw_content)
    content = extractor.content()
    title = extractor.title or _title_from_document_url(url)
    return content, title[:512], extractor.headings, extractor.links


def _title_from_document_url(url: str) -> str:
    parsed = urlparse(url)
    path_parts = [part for part in parsed.path.split("/") if part]
    if path_parts:
        return path_parts[-1].replace("-", " ").replace("_", " ").title()[:512]
    return parsed.hostname or "Untitled"


def _is_index_document_url(url: str, content: str) -> bool:
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    if path.endswith(("/index", "/readme", "")) or path in ("/docs", "/documentation"):
        return True
    word_count = len(content.split())
    return bool(word_count and content.count("http") / word_count > 0.1)


class _DocumentHtmlExtractor(HTMLParser):
    _SKIP_TAGS = {"script", "style", "nav", "footer", "aside", "header", "form"}
    _BLOCK_TAGS = {"article", "br", "div", "li", "main", "p", "section", "tr"}
    _HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self, *, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self._base_url = base_url
        self._skip_depth = 0
        self._in_title = False
        self._heading_parts: list[str] | None = None
        self._title_parts: list[str] = []
        self._text_parts: list[str] = []
        self.headings: list[str] = []
        self.links: list[str] = []

    @property
    def title(self) -> str:
        return " ".join(part for part in self._title_parts if part).strip()

    def content(self) -> str:
        lines = []
        for line in " ".join(self._text_parts).splitlines():
            normalized = " ".join(line.split())
            if normalized:
                lines.append(normalized)
        return "\n".join(lines)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "title":
            self._in_title = True
            return
        if tag in self._HEADING_TAGS:
            self._heading_parts = []
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(urljoin(self._base_url, href))
        if tag in self._BLOCK_TAGS:
            self._text_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self._SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if tag == "title":
            self._in_title = False
            return
        if tag in self._HEADING_TAGS and self._heading_parts is not None:
            heading = " ".join(self._heading_parts).strip()
            if heading:
                self.headings.append(heading[:200])
                self._text_parts.append(f"\n# {heading}\n")
            self._heading_parts = None

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = " ".join(data.split())
        if not text:
            return
        if self._in_title:
            self._title_parts.append(text)
            return
        if self._heading_parts is not None:
            self._heading_parts.append(text)
            return
        self._text_parts.append(text)


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
    return normalize_safe_url(source_uri, allow_private_network=allow_private_network)


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
