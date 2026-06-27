"""Aggregated operational adapters, re-exported from the auth, backup, and setup surfaces."""

from __future__ import annotations

from sibyl.persistence.auth_runtime import (
    confirm_password_reset,
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
from sibyl.persistence.surreal.setup import (
    get_setup_status,
    is_setup_mode,
    require_global_admin,
    require_settings_admin,
    require_settings_owner,
    require_setup_mode_or_admin,
    require_setup_mode_or_auth,
)

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
    "list_backups",
    "request_password_reset",
    "require_global_admin",
    "require_settings_admin",
    "require_settings_owner",
    "require_setup_mode_or_admin",
    "require_setup_mode_or_auth",
    "update_backup_settings",
]
