"""JWT helper compatibility exports for the API package."""

from sibyl import config as _config_module
from sibyl_core.auth.jwt import (
    JwtError,
    create_access_token,
    create_refresh_token,
    decode_token_unverified,
    install_settings_provider,
    verify_access_token,
    verify_refresh_token,
)

install_settings_provider(lambda: _config_module.settings)

__all__ = [
    "JwtError",
    "create_access_token",
    "create_refresh_token",
    "decode_token_unverified",
    "verify_access_token",
    "verify_refresh_token",
]
