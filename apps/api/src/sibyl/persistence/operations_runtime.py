"""Active operational adapters for the current runtime."""

from __future__ import annotations

from collections.abc import Awaitable
from importlib import import_module
from types import ModuleType
from typing import TYPE_CHECKING, Protocol, cast

from sibyl.persistence.auth_runtime import (
    confirm_password_reset,
    list_oauth_connections,
    remove_oauth_connection,
    request_password_reset,
)
from sibyl.persistence.backups_runtime import (
    attach_backup_job,
    create_backup_record,
    delete_backup_record,
    get_backup,
    get_backup_retention,
    get_backup_settings,
    list_backups,
    update_backup_settings,
)


class RuntimeExport(Protocol):
    def __call__(self, *args: object, **kwargs: object) -> Awaitable[object]: ...


if TYPE_CHECKING:
    from starlette.requests import Request

    from sibyl.persistence.setup_common import SetupStatus

    class GetSetupStatus(Protocol):
        def __call__(self) -> Awaitable[SetupStatus]: ...

    class IsSetupMode(Protocol):
        def __call__(self) -> Awaitable[bool]: ...

    class RequireSettingsAdmin(Protocol):
        def __call__(self, request: Request) -> Awaitable[None]: ...

    class RequireSetupModeOrAdmin(Protocol):
        def __call__(self, request: Request) -> Awaitable[object | None]: ...

    class RequireSetupModeOrAuth(Protocol):
        def __call__(self, request: Request) -> Awaitable[None]: ...

    get_setup_status: GetSetupStatus
    is_setup_mode: IsSetupMode
    require_settings_admin: RequireSettingsAdmin
    require_setup_mode_or_admin: RequireSetupModeOrAdmin
    require_setup_mode_or_auth: RequireSetupModeOrAuth

_AUTH_BACKEND_MODULE = "sibyl.persistence.surreal.setup"

_AUTH_RUNTIME_EXPORTS = [
    "get_setup_status",
    "is_setup_mode",
    "require_settings_admin",
    "require_setup_mode_or_admin",
    "require_setup_mode_or_auth",
]

__all__ = [
    "attach_backup_job",
    "confirm_password_reset",
    "create_backup_record",
    "delete_backup_record",
    "get_backup",
    "get_backup_retention",
    "get_backup_settings",
    "get_setup_status",
    "is_setup_mode",
    "require_settings_admin",
    "require_setup_mode_or_admin",
    "require_setup_mode_or_auth",
    "list_backups",
    "list_oauth_connections",
    "remove_oauth_connection",
    "request_password_reset",
    "update_backup_settings",
]


def _auth_backend_module() -> ModuleType:
    return import_module(_AUTH_BACKEND_MODULE)


def _make_auth_runtime_proxy(name: str) -> RuntimeExport:
    async def _proxy(*args: object, **kwargs: object) -> object:
        export = cast("RuntimeExport", getattr(_auth_backend_module(), name))
        return await export(*args, **kwargs)

    _proxy.__name__ = name
    return cast("RuntimeExport", _proxy)


for _export_name in _AUTH_RUNTIME_EXPORTS:
    globals()[_export_name] = _make_auth_runtime_proxy(_export_name)


del _export_name
