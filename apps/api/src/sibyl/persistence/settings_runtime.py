"""Active system-setting adapters for the current persistence runtime."""

from __future__ import annotations

from collections.abc import Awaitable
from contextlib import asynccontextmanager
from importlib import import_module
from types import ModuleType
from typing import TYPE_CHECKING, Protocol, cast

from sibyl.config import settings


class RuntimeExport(Protocol):
    def __call__(self, *args: object, **kwargs: object) -> Awaitable[object]: ...


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sibyl.persistence.settings_types import SystemSettingRecord

    class GetSystemSetting(Protocol):
        def __call__(
            self, session: object, *, key: str
        ) -> Awaitable[SystemSettingRecord | None]: ...

    class ListSystemSettings(Protocol):
        def __call__(self, session: object) -> Awaitable[list[SystemSettingRecord]]: ...

    class SaveSystemSetting(Protocol):
        def __call__(
            self, session: object, *, setting: SystemSettingRecord
        ) -> Awaitable[SystemSettingRecord]: ...

    class DeleteSystemSetting(Protocol):
        def __call__(self, session: object, *, key: str) -> Awaitable[bool]: ...

    delete_system_setting: DeleteSystemSetting
    get_system_setting: GetSystemSetting
    list_system_settings: ListSystemSettings
    save_system_setting: SaveSystemSetting

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


def _backend_module() -> ModuleType:
    return import_module(_BACKEND_MODULES[settings.store])


@asynccontextmanager
async def get_session() -> AsyncGenerator[object]:
    from sibyl.db.connection import get_session as _get_session

    async with _get_session() as session:
        yield session


@asynccontextmanager
async def get_settings_session() -> AsyncGenerator[object | None]:
    if settings.store == "surreal":
        yield None
        return
    async with get_session() as session:
        yield session


def _make_runtime_proxy(name: str) -> RuntimeExport:
    async def _proxy(*args: object, **kwargs: object) -> object:
        export = cast("RuntimeExport", getattr(_backend_module(), name))
        return await export(*args, **kwargs)

    _proxy.__name__ = name
    return cast("RuntimeExport", _proxy)


for _export_name in _RUNTIME_EXPORTS:
    if _export_name not in globals():
        globals()[_export_name] = _make_runtime_proxy(_export_name)
