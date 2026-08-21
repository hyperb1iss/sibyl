"""Canonical contracts ownership for entity routes."""

from enum import StrEnum

import structlog

from sibyl_core.auth import OrganizationRole

log = structlog.get_logger()

READ_ROLES = (
    OrganizationRole.OWNER,
    OrganizationRole.ADMIN,
    OrganizationRole.MEMBER,
    OrganizationRole.VIEWER,
)
WRITE_ROLES = (
    OrganizationRole.OWNER,
    OrganizationRole.ADMIN,
    OrganizationRole.MEMBER,
)


class SortField(StrEnum):
    """Fields available for sorting entities."""

    NAME = "name"
    CREATED_AT = "created_at"
    UPDATED_AT = "updated_at"
    ENTITY_TYPE = "entity_type"


class SortOrder(StrEnum):
    """Sort order direction."""

    ASC = "asc"
    DESC = "desc"
