"""Compatibility wrapper for legacy SQL audit logging."""

from __future__ import annotations

from sibyl.persistence.legacy.auth_managers.audit import AuditLogger

__all__ = ["AuditLogger"]
