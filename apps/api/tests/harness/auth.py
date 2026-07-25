"""Shared auth-context stubs for route tests.

Route tests used to build contexts as ad-hoc ``SimpleNamespace`` objects
carrying only the attributes the test happened to need. That is why the
production code read the API-key memory grant through ``getattr`` with a
``None`` fallback: direct attribute access would have raised against those
stubs. But ``None`` is not neutral downstream — the read rule treats it as an
unrestricted session and grants private scope — so the defensive read turned a
future contract drift into a silent full-session read.

These build the real dataclasses instead. A field added to ``AuthContext`` or
``McpContext`` appears here for free, and a stub cannot omit one, which is what
lets the production sites use direct attribute access and fail loudly.
"""

from __future__ import annotations

from uuid import UUID, uuid4

from sibyl.auth.context import AuthContext
from sibyl.server import McpContext
from sibyl_core.auth import AuthOrganization, AuthUser, OrganizationRole

DEFAULT_ORG_ID = UUID("00000000-0000-0000-0000-000000000111")
DEFAULT_USER_ID = UUID("00000000-0000-0000-0000-000000000222")


def stub_auth_context(
    *,
    user_id: UUID | str = DEFAULT_USER_ID,
    organization_id: UUID | str | None = DEFAULT_ORG_ID,
    org_role: OrganizationRole = OrganizationRole.MEMBER,
    scopes: frozenset[str] = frozenset(),
    api_key_id: str | None = None,
    api_key_project_ids: frozenset[str] | None = None,
    api_key_memory_space_ids: frozenset[str] | None = None,
    api_key_memory_scope_keys: frozenset[str] | set[str] | list[str] | None = None,
) -> AuthContext:
    """A real AuthContext for a route test.

    Defaults describe an ordinary member session with no API-key narrowing,
    which is the common case; a narrowed key is requested explicitly.
    """
    organization = (
        AuthOrganization(id=_as_uuid(organization_id), name="Sibyl", slug="sibyl")
        if organization_id is not None
        else None
    )
    return AuthContext(
        user=AuthUser(id=_as_uuid(user_id), email="stub@example.test"),
        organization=organization,
        org_role=org_role,
        scopes=scopes,
        api_key_id=api_key_id,
        api_key_project_ids=api_key_project_ids,
        api_key_memory_space_ids=api_key_memory_space_ids,
        api_key_memory_scope_keys=(
            frozenset(api_key_memory_scope_keys) if api_key_memory_scope_keys is not None else None
        ),
    )


def stub_mcp_context(
    *,
    org_id: str | None = None,
    user_id: str | None = None,
    scopes: list[str] | None = None,
    api_key_project_ids: list[str] | None = None,
    api_key_memory_space_ids: list[str] | None = None,
    api_key_memory_scope_keys: list[str] | set[str] | None = None,
) -> McpContext:
    """A real McpContext for an MCP tool test."""
    return McpContext(
        org_id=org_id if org_id is not None else str(uuid4()),
        user_id=user_id if user_id is not None else str(uuid4()),
        scopes=scopes if scopes is not None else ["mcp"],
        api_key_project_ids=api_key_project_ids,
        api_key_memory_space_ids=api_key_memory_space_ids,
        api_key_memory_scope_keys=(
            list(api_key_memory_scope_keys) if api_key_memory_scope_keys is not None else None
        ),
    )


def _as_uuid(value: UUID | str) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))


__all__ = [
    "DEFAULT_ORG_ID",
    "DEFAULT_USER_ID",
    "stub_auth_context",
    "stub_mcp_context",
]
