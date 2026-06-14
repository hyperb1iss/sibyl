from __future__ import annotations

from sibyl.persistence import operations_runtime
from sibyl.persistence.surreal import setup as surreal_setup


def test_operations_runtime_reexports_surreal_setup_symbols() -> None:
    assert operations_runtime.get_setup_status is surreal_setup.get_setup_status
    assert operations_runtime.require_settings_admin is surreal_setup.require_settings_admin
    assert operations_runtime.require_settings_owner is surreal_setup.require_settings_owner


def test_operations_runtime_exports_neutral_runtime_surface() -> None:
    assert set(operations_runtime.__all__) == {
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
        "list_oauth_connections",
        "remove_oauth_connection",
        "request_password_reset",
        "require_global_admin",
        "require_settings_admin",
        "require_settings_owner",
        "require_setup_mode_or_admin",
        "require_setup_mode_or_auth",
        "update_backup_settings",
    }
