from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SetupStatus:
    has_users: bool
    has_orgs: bool


LegacySetupStatus = SetupStatus

__all__ = ["SetupStatus", "LegacySetupStatus"]
