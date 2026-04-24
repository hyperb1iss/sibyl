"""Active content adapters for the current persistence runtime."""

from __future__ import annotations

from contextlib import asynccontextmanager
from importlib import import_module
from typing import TYPE_CHECKING, Any

from sibyl.config import settings

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sqlalchemy.ext.asyncio import AsyncSession

_BACKEND_MODULES = {
    "legacy": (
        "sibyl.persistence.legacy.crawler",
        "sibyl.persistence.legacy.entities",
        "sibyl.persistence.legacy.rag",
    ),
    "surreal": ("sibyl.persistence.surreal.content",),
}

_BACKEND_EXPORTS = [
    "create_crawl_source_record",
    "count_remaining_unlinked_chunks",
    "delete_crawl_source_record",
    "delete_crawled_document_record",
    "get_crawl_source_by_id",
    "get_crawl_source_by_url",
    "get_crawl_stats_payload",
    "get_crawled_document_for_org",
    "get_document_by_url_for_org",
    "get_link_graph_status_payload",
    "get_raw_capture",
    "get_org_crawl_source",
    "get_source_sync_counts",
    "hybrid_search_chunks",
    "list_crawl_sources_for_org",
    "list_crawl_sources",
    "list_crawled_documents_for_org",
    "list_document_chunks",
    "list_raw_captures",
    "list_rag_source_documents_page",
    "list_source_chunks",
    "list_source_documents",
    "list_source_documents_page",
    "list_sources_for_graph_linking",
    "list_unlinked_source_chunks",
    "resolve_document_entity",
    "save_crawl_source_record",
    "save_crawled_document_record",
    "save_document_chunks",
    "save_raw_capture_record",
    "search_code_example_chunks",
    "search_rag_chunks",
]

_LEGACY_EXPORT_ALIASES = {
    "get_legacy_raw_capture": "get_raw_capture",
    "list_legacy_raw_captures": "list_raw_captures",
    "resolve_legacy_document_entity": "resolve_document_entity",
}

__all__ = [
    "get_content_read_session",
    "get_content_read_session_dependency",
]
__all__.extend(_BACKEND_EXPORTS)
__all__.extend(_LEGACY_EXPORT_ALIASES)


def _active_backend_name() -> str:
    return settings.store


def _resolve_backend_export(name: str) -> Any:
    backend = _active_backend_name()
    for module_name in _BACKEND_MODULES[backend]:
        module = import_module(module_name)
        if hasattr(module, name):
            return getattr(module, name)
    msg = f"{name} is not implemented for SIBYL_STORE={backend!r}"
    raise AttributeError(msg)


@asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession]:
    from sibyl.db.connection import get_session as _get_session

    async with _get_session() as session:
        yield session


@asynccontextmanager
async def get_content_read_session() -> AsyncGenerator[AsyncSession | None]:
    """Yield a relational session only when the active content runtime needs one."""
    if settings.store == "surreal":
        yield None
        return
    async with get_session() as session:
        yield session


async def get_content_read_session_dependency() -> AsyncGenerator[AsyncSession | None]:
    """FastAPI dependency wrapper for content reads across runtimes."""
    async with get_content_read_session() as session:
        yield session


def _make_runtime_proxy(name: str) -> Any:
    async def _proxy(*args: object, **kwargs: object) -> object:
        export = _resolve_backend_export(name)
        return await export(*args, **kwargs)

    _proxy.__name__ = name
    return _proxy


for _export_name in _BACKEND_EXPORTS:
    if _export_name not in globals():
        globals()[_export_name] = _make_runtime_proxy(_export_name)

for _legacy_name, _neutral_name in _LEGACY_EXPORT_ALIASES.items():
    globals()[_legacy_name] = globals()[_neutral_name]

del _export_name
del _legacy_name
del _neutral_name


def __getattr__(name: str) -> Any:
    if name in _BACKEND_EXPORTS or name in _LEGACY_EXPORT_ALIASES:
        return _resolve_backend_export(name)
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_BACKEND_EXPORTS) | set(_LEGACY_EXPORT_ALIASES))
