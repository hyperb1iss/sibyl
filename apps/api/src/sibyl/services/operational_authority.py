"""Current write authority for retained operational captures and their jobs."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from sibyl.auth.authorization import verify_entity_project_access
from sibyl.auth.context import AuthContext
from sibyl.auth.dependencies import _api_key_allows_rest, _api_key_claims
from sibyl.persistence.auth_common import InvalidAuthClaimsError, UserNotFoundError
from sibyl.persistence.auth_runtime import resolve_api_key_authority, resolve_auth_context
from sibyl_core.auth import OrganizationRole, ProjectRole
from sibyl_core.models.experience import OperationalExperience
from sibyl_core.services.memory_source_validation import SourceReadAuthority
from sibyl_core.services.operational_projection import OperationalProjectionSource
from sibyl_core.services.source_observations import SourceUnavailableError
from sibyl_core.services.source_state_store import RawSourceSnapshot


class OperationalWriteAuthority(BaseModel):
    """Trusted intake identity and ceilings, serialized only by the service."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    version: Literal[1] = 1
    organization_id: str = Field(min_length=1)
    actor_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    api_key_id: str | None
    scopes: tuple[str, ...]
    project_ids: tuple[str, ...] | None
    memory_scope_keys: tuple[str, ...] | None

    @classmethod
    def from_context(cls, context: AuthContext, project_id: str) -> OperationalWriteAuthority:
        if context.user_id is None or context.organization_id is None:
            raise SourceUnavailableError
        return cls(
            organization_id=context.organization_id,
            actor_id=context.user_id,
            project_id=project_id,
            api_key_id=context.api_key_id,
            scopes=tuple(sorted(context.scopes)),
            project_ids=(
                tuple(sorted(context.api_key_project_ids))
                if context.api_key_project_ids is not None
                else None
            ),
            memory_scope_keys=(
                tuple(sorted(context.api_key_memory_scope_keys))
                if context.api_key_memory_scope_keys is not None
                else None
            ),
        )

    async def current(self, *, creator_id: str) -> AuthContext:
        from sibyl.api.routes.memory_auth import api_key_memory_scope_allowed

        claims: dict[str, object] = {"sub": self.actor_id, "org": self.organization_id}
        if self.api_key_id is not None:
            key = await resolve_api_key_authority(
                api_key_id=UUID(self.api_key_id),
                organization_id=UUID(self.organization_id),
                user_id=UUID(self.actor_id),
            )
            if key is None:
                raise SourceUnavailableError
            scopes = sorted(set(key.scopes).intersection(self.scopes))
            if not _api_key_allows_rest(scopes=scopes, method="POST"):
                raise SourceUnavailableError
            claims = _api_key_claims(key, scopes=scopes)
        try:
            context = await resolve_auth_context(claims=claims)
        except (InvalidAuthClaimsError, UserNotFoundError) as exc:
            raise SourceUnavailableError from exc
        if (
            context.user_id != self.actor_id
            or context.organization_id != self.organization_id
            or context.org_role
            not in (OrganizationRole.OWNER, OrganizationRole.ADMIN, OrganizationRole.MEMBER)
        ):
            raise SourceUnavailableError
        context = replace(
            context,
            api_key_project_ids=_intersection(context.api_key_project_ids, self.project_ids),
            api_key_memory_scope_keys=_intersection(
                context.api_key_memory_scope_keys, self.memory_scope_keys
            ),
        )
        if (
            context.api_key_project_ids is not None
            and self.project_id not in context.api_key_project_ids
        ) or not api_key_memory_scope_allowed(
            context, memory_scope="project", scope_key=self.project_id
        ):
            raise SourceUnavailableError
        try:
            await verify_entity_project_access(
                None,
                context,
                self.project_id,
                required_role=(
                    ProjectRole.CONTRIBUTOR
                    if creator_id == self.actor_id
                    else ProjectRole.MAINTAINER
                ),
                require_existing_project=True,
            )
        except HTTPException as exc:
            raise SourceUnavailableError from exc
        return context

    def read_ceiling(self) -> SourceReadAuthority:
        return SourceReadAuthority(
            principal_id=self.actor_id,
            projects=frozenset({self.project_id}),
            scope_keys=(
                frozenset(self.memory_scope_keys) if self.memory_scope_keys is not None else None
            ),
        )


def _intersection(
    current: frozenset[str] | None, ceiling: tuple[str, ...] | None
) -> frozenset[str] | None:
    if ceiling is None:
        return current
    retained = frozenset(ceiling)
    return retained if current is None else current.intersection(retained)


@dataclass(frozen=True, slots=True)
class OperationalPublicationSource(OperationalProjectionSource):
    """Apply current write authority at the shared graph owner's source fences."""

    write_authority: OperationalWriteAuthority

    async def current(self) -> tuple[RawSourceSnapshot, OperationalExperience]:
        if (
            self.write_authority.organization_id != self.observation.source.organization_id
            or self.write_authority.actor_id != self.authority.principal_id
            or self.write_authority.project_id != self.project_id
            or self.authority != self.write_authority.read_ceiling()
        ):
            raise SourceUnavailableError
        await self.write_authority.current(creator_id=self.creator_id)
        return await OperationalProjectionSource.current(self)
