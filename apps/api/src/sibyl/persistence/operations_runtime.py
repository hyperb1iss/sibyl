"""Active operational adapters for the current runtime."""

from __future__ import annotations

from importlib import import_module
from typing import Any

from sibyl.config import settings
from sibyl.persistence.auth_runtime import (
    confirm_legacy_password_reset,
    list_legacy_oauth_connections,
    remove_legacy_oauth_connection,
    request_legacy_password_reset,
)
from sibyl.persistence.backups_runtime import (
    attach_backup_job as attach_legacy_backup_job,
    create_backup_record as create_legacy_backup_record,
    delete_backup_record as delete_legacy_backup_record,
    get_backup as get_legacy_backup,
    get_backup_retention as get_legacy_backup_retention,
    get_backup_settings as get_legacy_backup_settings,
    list_backups as list_legacy_backups,
    update_backup_settings as update_legacy_backup_settings,
)

_AUTH_BACKEND_MODULES = {
    "postgres": "sibyl.persistence.legacy.setup",
    "surreal": "sibyl.persistence.surreal.setup",
}


def _auth_backend_module() -> Any:
    return import_module(_AUTH_BACKEND_MODULES[settings.auth_store])


def _make_auth_runtime_proxy(name: str) -> Any:
    async def _proxy(*args: object, **kwargs: object) -> object:
        export = getattr(_auth_backend_module(), name)
        return await export(*args, **kwargs)

    _proxy.__name__ = name
    return _proxy


get_legacy_setup_status = _make_auth_runtime_proxy("get_legacy_setup_status")
is_legacy_setup_mode = _make_auth_runtime_proxy("is_legacy_setup_mode")
require_legacy_settings_admin = _make_auth_runtime_proxy("require_legacy_settings_admin")
require_legacy_setup_mode_or_admin = _make_auth_runtime_proxy(
    "require_legacy_setup_mode_or_admin"
)
require_legacy_setup_mode_or_auth = _make_auth_runtime_proxy("require_legacy_setup_mode_or_auth")

__all__ = [
    "attach_legacy_backup_job",
    "confirm_legacy_password_reset",
    "create_legacy_backup_record",
    "delete_legacy_backup_record",
    "get_legacy_backup",
    "get_legacy_backup_retention",
    "get_legacy_backup_settings",
    "get_legacy_setup_status",
    "is_legacy_setup_mode",
    "list_legacy_backups",
    "list_legacy_oauth_connections",
    "remove_legacy_oauth_connection",
    "request_legacy_password_reset",
    "require_legacy_settings_admin",
    "require_legacy_setup_mode_or_admin",
    "require_legacy_setup_mode_or_auth",
    "update_legacy_backup_settings",
]
