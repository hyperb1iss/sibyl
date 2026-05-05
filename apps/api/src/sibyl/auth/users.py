"""Compatibility wrapper for legacy SQL users."""

from __future__ import annotations

from sibyl.persistence.legacy.auth_managers.users import (
    GitHubUserIdentity,
    PasswordChange,
    UserManager,
)

__all__ = ["GitHubUserIdentity", "PasswordChange", "UserManager"]
