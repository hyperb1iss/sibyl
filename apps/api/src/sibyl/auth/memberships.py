"""Compatibility wrapper for legacy SQL organization memberships."""

from __future__ import annotations

from sibyl.persistence.legacy.auth_managers.memberships import (
    OrganizationMembershipManager,
)

__all__ = ["OrganizationMembershipManager"]
