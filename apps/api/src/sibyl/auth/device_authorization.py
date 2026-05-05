"""Compatibility wrapper for legacy SQL device authorization."""

from __future__ import annotations

from sibyl.auth.primitives import (
    DeviceTokenError,
    generate_device_code,
    generate_user_code,
    normalize_user_code,
)
from sibyl.persistence.legacy.auth_managers.device_authorization import (
    DeviceAuthorizationManager,
)

__all__ = [
    "DeviceAuthorizationManager",
    "DeviceTokenError",
    "generate_device_code",
    "generate_user_code",
    "normalize_user_code",
]
