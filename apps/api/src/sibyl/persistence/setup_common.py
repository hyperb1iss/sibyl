from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LegacySetupStatus:
    has_users: bool
    has_orgs: bool
