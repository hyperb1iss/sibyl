"""Bootstrap commands for production installations."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID, uuid4

import typer

from sibyl.cli.common import info, run_async, success

app = typer.Typer(
    name="bootstrap",
    help="Bootstrap tenant records for production installs",
    no_args_is_help=True,
)


@dataclass(slots=True)
class BootstrapOrgResult:
    organization_id: UUID
    organization_slug: str
    organization_created: bool
    owner_user_id: UUID | None
    owner_created: bool
    memory_space_id: UUID | None
    memory_space_created: bool


def _password_from_env(env_name: str) -> str | None:
    value = os.environ.get(env_name, "").strip()
    return value or None


async def _ensure_owner_user(
    client: object,
    *,
    email: str,
    password: str | None,
    name: str,
) -> tuple[object, bool]:
    from sibyl.persistence.surreal.auth import SurrealUserRepository

    users = SurrealUserRepository.from_client(client)  # type: ignore[arg-type]
    existing = await users.get_by_email(email)
    if existing is not None:
        if not existing.is_admin:
            await client.execute_query(  # type: ignore[attr-defined]
                "UPDATE users SET is_admin = true, updated_at = time::now() WHERE uuid = $uuid;",
                uuid=str(existing.id),
            )
        return existing, False

    if not password:
        msg = "owner password is required when the bootstrap owner does not exist"
        raise typer.BadParameter(msg)
    created = await users.create_local_user(
        email=email,
        password=password,
        name=name,
        is_admin=True,
    )
    return created, True


async def _ensure_organization(
    client: object,
    *,
    name: str,
    slug: str | None,
) -> tuple[object, bool]:
    from sibyl.auth.primitives import slugify
    from sibyl.persistence.surreal.auth import SurrealOrganizationRepository

    organizations = SurrealOrganizationRepository.from_client(client)  # type: ignore[arg-type]
    resolved_slug = slugify(slug or name)
    existing = await organizations.get_by_slug(resolved_slug)
    if existing is not None:
        return existing, False

    created = await organizations.create(name=name, slug=resolved_slug, is_personal=False)
    return created, True


async def _ensure_owner_membership(
    client: object,
    *,
    organization_id: UUID,
    user_id: UUID,
) -> None:
    from sibyl.persistence.surreal.auth import SurrealOrganizationMembershipRepository
    from sibyl_core.auth import OrganizationRole

    memberships = SurrealOrganizationMembershipRepository.from_client(client)  # type: ignore[arg-type]
    await memberships.add_member(
        organization_id=organization_id,
        user_id=user_id,
        role=OrganizationRole.OWNER,
    )


async def _ensure_memory_space(
    client: object,
    *,
    organization_id: UUID,
    owner_user_id: UUID,
    memory_scope: str,
    scope_key: str | None,
    name: str,
) -> tuple[UUID, bool]:
    from sibyl.persistence.surreal.auth_runtime import _memory_space_scope_key, _memory_space_state

    resolved_scope_key = _memory_space_scope_key(
        memory_scope=memory_scope,
        scope_key=scope_key,
        created_by_user_id=owner_user_id,
    )
    state, disabled_reason = _memory_space_state(memory_scope)
    now = datetime.now(UTC).replace(tzinfo=None)

    existing = await client.execute_query(  # type: ignore[attr-defined]
        """
        SELECT * FROM memory_spaces
        WHERE organization_id = $organization_id
            AND memory_scope = $memory_scope
            AND scope_key = $scope_key
        LIMIT 1;
        """,
        organization_id=str(organization_id),
        memory_scope=memory_scope,
        scope_key=resolved_scope_key,
    )
    if isinstance(existing, list) and existing and isinstance(existing[0], dict):
        return UUID(str(existing[0]["uuid"])), False

    space_id = uuid4()
    await client.execute_query(  # type: ignore[attr-defined]
        "CREATE memory_spaces CONTENT $record;",
        record={
            "uuid": str(space_id),
            "organization_id": str(organization_id),
            "memory_scope": memory_scope,
            "scope_key": resolved_scope_key,
            "name": name[:200],
            "description": "Default bootstrap memory space",
            "state": state,
            "disabled_reason": disabled_reason,
            "metadata": {"source": "bootstrap"},
            "created_by_user_id": str(owner_user_id),
            "created_at": now,
            "updated_at": now,
        },
    )
    return space_id, True


async def bootstrap_org_records(
    *,
    organization_name: str,
    organization_slug: str | None,
    owner_email: str | None,
    owner_name: str,
    owner_password: str | None,
    memory_space_enabled: bool,
    memory_space_name: str,
    memory_space_scope: str,
    memory_space_scope_key: str | None,
) -> BootstrapOrgResult:
    from sibyl.persistence.surreal.auth import build_surreal_auth_client
    from sibyl.persistence.surreal.organization_runtime import ensure_graph_indexes
    from sibyl.surreal_runtime_startup import bootstrap_surreal_content_schema
    from sibyl_core.backends.surreal import bootstrap_auth_schema

    await bootstrap_surreal_content_schema()

    client = build_surreal_auth_client()
    try:
        await bootstrap_auth_schema(client)
        organization, organization_created = await _ensure_organization(
            client,
            name=organization_name,
            slug=organization_slug,
        )

        owner = None
        owner_created = False
        if owner_email:
            owner, owner_created = await _ensure_owner_user(
                client,
                email=owner_email,
                password=owner_password,
                name=owner_name,
            )
            await _ensure_owner_membership(
                client,
                organization_id=organization.id,
                user_id=owner.id,
            )

        memory_space_id = None
        memory_space_created = False
        if memory_space_enabled:
            if owner is None:
                msg = "memory-space bootstrap requires --owner-email"
                raise typer.BadParameter(msg)
            memory_space_id, memory_space_created = await _ensure_memory_space(
                client,
                organization_id=organization.id,
                owner_user_id=owner.id,
                memory_scope=memory_space_scope,
                scope_key=memory_space_scope_key,
                name=memory_space_name,
            )

        await ensure_graph_indexes(str(organization.id))

        return BootstrapOrgResult(
            organization_id=organization.id,
            organization_slug=organization.slug,
            organization_created=organization_created,
            owner_user_id=owner.id if owner is not None else None,
            owner_created=owner_created,
            memory_space_id=memory_space_id,
            memory_space_created=memory_space_created,
        )
    finally:
        await client.close()


@app.command("org")
def bootstrap_org(
    organization_name: Annotated[str, typer.Option("--name", help="Organization name")],
    organization_slug: Annotated[
        str | None,
        typer.Option("--slug", help="Optional organization slug"),
    ] = None,
    owner_email: Annotated[
        str | None,
        typer.Option(
            "--owner-email",
            envvar="SIBYL_BOOTSTRAP_OWNER_EMAIL",
            help="Break-glass owner email",
        ),
    ] = None,
    owner_name: Annotated[
        str,
        typer.Option(
            "--owner-name",
            envvar="SIBYL_BOOTSTRAP_OWNER_NAME",
            help="Break-glass owner display name",
        ),
    ] = "Sibyl Break Glass",
    owner_secret_env: Annotated[
        str,
        typer.Option("--owner-password-env", help="Environment variable containing owner password"),
    ] = "SIBYL_BOOTSTRAP_OWNER_PASSWORD",  # noqa: S107
    skip_memory_space: Annotated[
        bool,
        typer.Option("--skip-memory-space", help="Skip default memory-space creation"),
    ] = False,
    memory_space_name: Annotated[
        str,
        typer.Option("--memory-space-name", help="Default memory-space name"),
    ] = "Default memory",
    memory_space_scope: Annotated[
        str,
        typer.Option("--memory-space-scope", help="Default memory-space scope"),
    ] = "private",
    memory_space_scope_key: Annotated[
        str | None,
        typer.Option("--memory-space-scope-key", help="Scope key for non-private memory spaces"),
    ] = None,
) -> None:
    """Seed an Organization, optional break-glass owner, and default MemorySpace."""

    @run_async
    async def _bootstrap() -> None:
        result = await bootstrap_org_records(
            organization_name=organization_name,
            organization_slug=organization_slug,
            owner_email=owner_email.strip().lower() if owner_email else None,
            owner_name=owner_name,
            owner_password=_password_from_env(owner_secret_env),
            memory_space_enabled=not skip_memory_space,
            memory_space_name=memory_space_name,
            memory_space_scope=memory_space_scope,
            memory_space_scope_key=memory_space_scope_key,
        )
        status = "created" if result.organization_created else "already exists"
        success(f"Organization {result.organization_slug} {status}")
        if result.owner_user_id is not None:
            owner_status = "created" if result.owner_created else "already exists"
            info(f"Owner user {result.owner_user_id} {owner_status}")
        if result.memory_space_id is not None:
            space_status = "created" if result.memory_space_created else "already exists"
            info(f"Memory space {result.memory_space_id} {space_status}")

    _bootstrap()
