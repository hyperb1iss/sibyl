"""Active backup adapters for the current persistence runtime."""

from __future__ import annotations

from importlib import import_module
from typing import Any

from sibyl.config import settings

_BACKEND_MODULES = {
    "legacy": "sibyl.persistence.legacy.backups",
    "surreal": "sibyl.persistence.surreal.backups",
}

_RUNTIME_EXPORTS = [
    "attach_backup_job",
    "create_backup_record",
    "delete_backup_record",
    "get_backup",
    "get_backup_retention",
    "get_backup_settings",
    "list_backups",
    "list_enabled_backup_settings",
    "update_backup_record",
    "update_backup_settings",
]

__all__ = list(_RUNTIME_EXPORTS)


def _backend_module() -> Any:
    return import_module(_BACKEND_MODULES[settings.store])


def _make_runtime_proxy(name: str) -> Any:
    async def _proxy(*args: object, **kwargs: object) -> object:
        export = getattr(_backend_module(), name)
        return await export(*args, **kwargs)

    _proxy.__name__ = name
    return _proxy


for _export_name in _RUNTIME_EXPORTS:
    if _export_name not in globals():
        globals()[_export_name] = _make_runtime_proxy(_export_name)
