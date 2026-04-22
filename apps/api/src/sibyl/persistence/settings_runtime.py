"""Active system-setting adapters for the current persistence runtime."""

from __future__ import annotations

from contextlib import asynccontextmanager
from importlib import import_module
from typing import TYPE_CHECKING, Any

from sibyl.config import settings
from sibyl.db.connection import get_session

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sqlalchemy.ext.asyncio import AsyncSession

_BACKEND_MODULES = {
    "legacy": "sibyl.persistence.legacy.system_settings",
    "surreal": "sibyl.persistence.surreal.system_settings",
}

_RUNTIME_EXPORTS = [
    "delete_system_setting",
    "get_settings_session",
    "get_system_setting",
    "list_system_settings",
    "save_system_setting",
]

__all__ = list(_RUNTIME_EXPORTS)


def _backend_module() -> Any:
    return import_module(_BACKEND_MODULES[settings.store])


@asynccontextmanager
async def get_settings_session() -> AsyncGenerator[AsyncSession | None]:
    if settings.store == "surreal":
        yield None
        return
    async with get_session() as session:
        yield session


def _make_runtime_proxy(name: str) -> Any:
    async def _proxy(*args: object, **kwargs: object) -> object:
        export = getattr(_backend_module(), name)
        return await export(*args, **kwargs)

    _proxy.__name__ = name
    return _proxy


for _export_name in _RUNTIME_EXPORTS:
    if _export_name not in globals():
        globals()[_export_name] = _make_runtime_proxy(_export_name)
