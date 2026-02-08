"""Staged sandbox rollout resolution.

Determines the effective sandbox mode for a given organization based on
global config, explicit allowlists, and percentage-based rollout.
"""

from __future__ import annotations

import hashlib
from uuid import UUID


def resolve_sandbox_mode(
    global_mode: str,
    org_id: UUID,
    rollout_percent: int,
    rollout_orgs: list[str],
    canary_mode: bool,
) -> str:
    """Resolve effective sandbox mode for an org.

    Resolution order:
    1. If global_mode == "off" -> "off" (master kill switch)
    2. If org_id in rollout_orgs -> "shadow" if canary else global_mode
    3. If rollout_percent == 100 -> global_mode
    4. If rollout_percent > 0 -> hash(org_id) % 100 < rollout_percent -> "shadow" if canary else global_mode
    5. Else -> "off"

    Deterministic hash ensures same orgs stay in/out across restarts.
    """
    if global_mode == "off":
        return "off"

    org_str = str(org_id)

    # Explicit allowlist — always in
    if org_str in rollout_orgs:
        return "shadow" if canary_mode else global_mode

    # Full rollout
    if rollout_percent >= 100:
        return global_mode

    # No rollout
    if rollout_percent <= 0:
        return "off"

    # Percentage-based — deterministic hash assignment
    hash_val = int(hashlib.sha256(org_str.encode()).hexdigest()[:8], 16) % 100
    if hash_val < rollout_percent:
        return "shadow" if canary_mode else global_mode

    return "off"
