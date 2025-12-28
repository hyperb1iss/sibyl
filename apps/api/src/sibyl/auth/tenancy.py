"""Tenant/group helpers for graph operations.

Graphiti uses `group_id` to scope nodes/edges and searches. We treat the JWT `org`
claim as the canonical group id for graph operations.
"""

from __future__ import annotations


class MissingOrganizationError(Exception):
    """Raised when org context is required but not provided."""


def resolve_group_id(claims: dict | None) -> str:
    """Resolve the graph group_id for a request.

    Args:
        claims: JWT claims dict containing 'org' key.

    Returns:
        The organization ID as string.

    Raises:
        MissingOrganizationError: If no org claim is present.
    """
    if claims and claims.get("org"):
        return str(claims["org"])
    raise MissingOrganizationError(
        "Organization context required - cannot access graph without org claim in JWT"
    )
