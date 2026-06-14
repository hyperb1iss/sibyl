"""Password hashing compatibility exports for the API package."""

from sibyl import config as _config_module
from sibyl_core.auth.passwords import (
    PasswordError,
    PasswordHash,
    hash_password,
    install_settings_provider,
    verify_password,
    verify_password_timing_floor,
)

install_settings_provider(lambda: _config_module.settings)

__all__ = [
    "PasswordError",
    "PasswordHash",
    "hash_password",
    "verify_password",
    "verify_password_timing_floor",
]
