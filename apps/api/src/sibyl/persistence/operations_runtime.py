"""Active operational adapters for the current runtime."""

from __future__ import annotations

from importlib import import_module
from typing import Any

from sibyl.config import settings
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


get_setup_status = _make_auth_runtime_proxy("get_setup_status")
is_setup_mode = _make_auth_runtime_proxy("is_setup_mode")
require_settings_admin = _make_auth_runtime_proxy("require_settings_admin")
require_setup_mode_or_admin = _make_auth_runtime_proxy("require_setup_mode_or_admin")
require_setup_mode_or_auth = _make_auth_runtime_proxy("require_setup_mode_or_auth")

attach_legacy_backup_job = attach_backup_job
confirm_legacy_password_reset = confirm_password_reset
create_legacy_backup_record = create_backup_record
delete_legacy_backup_record = delete_backup_record
get_legacy_backup = get_backup
get_legacy_backup_retention = get_backup_retention
get_legacy_backup_settings = get_backup_settings
get_legacy_setup_status = get_setup_status
is_legacy_setup_mode = is_setup_mode
list_legacy_backups = list_backups
list_legacy_oauth_connections = list_oauth_connections
remove_legacy_oauth_connection = remove_oauth_connection
request_legacy_password_reset = request_password_reset
require_legacy_settings_admin = require_settings_admin
require_legacy_setup_mode_or_admin = require_setup_mode_or_admin
require_legacy_setup_mode_or_auth = require_setup_mode_or_auth
update_legacy_backup_settings = update_backup_settings

__all__ = [
    "attach_backup_job",
    "attach_legacy_backup_job",
    "confirm_password_reset",
    "confirm_legacy_password_reset",
    "create_backup_record",
    "create_legacy_backup_record",
    "delete_backup_record",
    "delete_legacy_backup_record",
    "get_backup",
    "get_legacy_backup",
    "get_backup_retention",
    "get_legacy_backup_retention",
    "get_backup_settings",
    "get_legacy_backup_settings",
    "get_setup_status",
    "get_legacy_setup_status",
    "is_setup_mode",
    "is_legacy_setup_mode",
    "list_backups",
    "list_legacy_backups",
    "list_oauth_connections",
    "list_legacy_oauth_connections",
    "remove_oauth_connection",
    "remove_legacy_oauth_connection",
    "request_password_reset",
    "request_legacy_password_reset",
    "require_settings_admin",
    "require_legacy_settings_admin",
    "update_legacy_backup_settings",
    "require_setup_mode_or_admin",
    "require_legacy_setup_mode_or_admin",
    "require_setup_mode_or_auth",
    "require_legacy_setup_mode_or_auth",
    "update_backup_settings",
]
