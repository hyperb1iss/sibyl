"""Compatibility wrapper for legacy SQL password resets."""

from __future__ import annotations

from sibyl.persistence.legacy.auth_managers.password_reset import (
    PasswordResetError,
    PasswordResetManager,
    ResetTokenResult,
)

__all__ = ["PasswordResetError", "PasswordResetManager", "ResetTokenResult"]
