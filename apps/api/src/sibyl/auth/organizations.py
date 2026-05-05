"""Compatibility wrapper for legacy SQL organizations."""

from __future__ import annotations

from sibyl.auth.primitives import slugify
from sibyl.persistence.legacy.auth_managers.organizations import OrganizationManager

__all__ = ["OrganizationManager", "slugify"]
