"""Active auth runtime adapters for the configured auth backend."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sibyl.config import settings
from sibyl.persistence.auth_common import InvalidAuthClaimsError, UserNotFoundError

if TYPE_CHECKING:
    from sibyl.persistence.legacy.auth import (
        LegacyAuthContextResolver,
        LegacyOrganizationMembershipRepository,
        LegacyOrganizationRepository,
        LegacySessionRepository,
        LegacyUserRepository,
        approve_legacy_device_authorization,
        authenticate_legacy_api_key,
        authenticate_legacy_local_user,
        create_legacy_api_key_for_user,
        create_legacy_session_record,
        deny_legacy_device_authorization,
        ensure_legacy_personal_organization,
        exchange_legacy_device_code,
        get_legacy_device_request_by_user_code,
        get_legacy_user_by_id,
        has_legacy_owner_membership,
        list_legacy_accessible_project_graph_ids,
        list_legacy_api_keys_for_user,
        list_legacy_user_organizations,
        load_legacy_refresh_session_record,
        log_legacy_audit_event,
        login_legacy_device_browser_user,
        login_legacy_github_identity,
        login_legacy_local_user,
        resolve_legacy_accessible_project_graph_ids,
        resolve_legacy_auth_context,
        resolve_legacy_request_claims,
        resolve_legacy_request_user,
        resolve_surreal_auth_context,
        revoke_legacy_access_session,
        revoke_legacy_api_key_for_user,
        revoke_legacy_refresh_session_record,
        rotate_legacy_refresh_exchange,
        rotate_legacy_refresh_session_record,
        signup_legacy_local_user,
        start_legacy_device_authorization,
        update_legacy_auth_user,
        verify_legacy_entity_project_access,
    )

_BACKEND_MODULES = {
    "postgres": "sibyl.persistence.legacy.auth",
    "surreal": "sibyl.persistence.surreal.auth_runtime",
}

_BACKEND_NAME_OVERRIDES = {
    "surreal": {
        "LegacyAuthContextResolver": "SurrealAuthContextResolver",
        "LegacyOrganizationMembershipRepository": "SurrealOrganizationMembershipRepository",
        "LegacyOrganizationRepository": "SurrealOrganizationRepository",
        "LegacySessionRepository": "SurrealSessionRepository",
        "LegacyUserRepository": "SurrealUserRepository",
    },
}

__all__ = [
    "InvalidAuthClaimsError",
    "LegacyAuthContextResolver",
    "LegacyOrganizationMembershipRepository",
    "LegacyOrganizationRepository",
    "LegacySessionRepository",
    "LegacyUserRepository",
    "UserNotFoundError",
    "approve_legacy_device_authorization",
    "authenticate_legacy_api_key",
    "authenticate_legacy_local_user",
    "create_legacy_api_key_for_user",
    "create_legacy_project_record",
    "create_legacy_session_record",
    "delete_legacy_project_record",
    "deny_legacy_device_authorization",
    "ensure_legacy_personal_organization",
    "exchange_legacy_device_code",
    "get_legacy_device_request_by_user_code",
    "get_legacy_project_record_by_graph_id",
    "get_legacy_project_record_by_id",
    "get_legacy_user_by_id",
    "has_legacy_owner_membership",
    "list_legacy_accessible_project_graph_ids",
    "list_legacy_api_keys_for_user",
    "list_legacy_oauth_connections",
    "list_legacy_user_sessions",
    "list_legacy_user_organizations",
    "load_legacy_refresh_session_record",
    "log_legacy_audit_event",
    "login_legacy_device_browser_user",
    "login_legacy_github_identity",
    "login_legacy_local_user",
    "resolve_legacy_accessible_project_graph_ids",
    "resolve_surreal_auth_context",
    "resolve_legacy_auth_context",
    "resolve_legacy_request_claims",
    "resolve_legacy_request_user",
    "revoke_legacy_access_session",
    "revoke_legacy_api_key_for_user",
    "revoke_legacy_user_session",
    "revoke_all_legacy_user_sessions",
    "revoke_legacy_refresh_session_record",
    "rotate_legacy_refresh_exchange",
    "rotate_legacy_refresh_session_record",
    "signup_legacy_local_user",
    "start_legacy_device_authorization",
    "patch_legacy_auth_user",
    "request_legacy_password_reset",
    "confirm_legacy_password_reset",
    "remove_legacy_oauth_connection",
    "update_legacy_auth_user",
    "update_legacy_project_record",
    "verify_legacy_entity_project_access",
]


def _active_backend_name() -> str:
    return settings.auth_store


def _resolve_backend_export(name: str) -> Any:
    backend = _active_backend_name()
    module = import_module(_BACKEND_MODULES[backend])
    export_name = _BACKEND_NAME_OVERRIDES.get(backend, {}).get(name, name)
    if hasattr(module, export_name):
        return getattr(module, export_name)
    return _unsupported_export(name=name, backend=backend)


def _unsupported_export(*, name: str, backend: str) -> Any:
    message = (
        f"{name} is not implemented for SIBYL_AUTH_STORE={backend!r}. "
        "Add the backend adapter or route the export through the runtime helper "
        "before using it on this path."
    )
    if name.endswith("Resolver"):

        class _UnsupportedResolver:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                raise NotImplementedError(message)

            @classmethod
            def from_session(cls, *_args: object, **_kwargs: object):
                raise NotImplementedError(message)

            @classmethod
            def from_client(cls, *_args: object, **_kwargs: object):
                raise NotImplementedError(message)

            async def resolve(self, *_args: object, **_kwargs: object) -> object:
                raise NotImplementedError(message)

        _UnsupportedResolver.__name__ = name
        return _UnsupportedResolver

    async def _unsupported(*_args: object, **_kwargs: object) -> object:
        raise NotImplementedError(message)

    _unsupported.__name__ = name
    return _unsupported


def __getattr__(name: str) -> Any:
    if name == "InvalidAuthClaimsError":
        return InvalidAuthClaimsError
    if name == "UserNotFoundError":
        return UserNotFoundError
    if name in __all__:
        return _resolve_backend_export(name)
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))


def _runtime_helper_module() -> Any:
    return import_module(
        {
            "postgres": "sibyl.persistence.legacy.auth_runtime",
            "surreal": "sibyl.persistence.surreal.auth_runtime",
        }[settings.auth_store]
    )


async def _call_runtime_helper(export_name: str, **kwargs: object) -> Any:
    export = getattr(_runtime_helper_module(), export_name)
    return await export(**kwargs)


async def _call_backend_export(export_name: str, *args: object, **kwargs: object) -> Any:
    export = _resolve_backend_export(export_name)
    return await export(*args, **kwargs)


async def patch_legacy_auth_user(
    *,
    user_id: UUID,
    updates: dict[str, Any],
    organization_id: UUID | None,
    request: Any,
):
    return await _call_runtime_helper(
        "patch_legacy_auth_user",
        user_id=user_id,
        updates=updates,
        organization_id=organization_id,
        request=request,
    )


async def create_legacy_project_record(
    *,
    organization_id: UUID,
    owner_user_id: UUID,
    graph_project_id: str,
    name: str,
    description: str | None = None,
) -> Any:
    return await _call_runtime_helper(
        "create_legacy_project_record",
        organization_id=organization_id,
        owner_user_id=owner_user_id,
        graph_project_id=graph_project_id,
        name=name,
        description=description,
    )


async def update_legacy_project_record(
    *,
    organization_id: UUID,
    graph_project_id: str,
    name: str | None = None,
    description: str | None = None,
) -> bool:
    return await _call_runtime_helper(
        "update_legacy_project_record",
        organization_id=organization_id,
        graph_project_id=graph_project_id,
        name=name,
        description=description,
    )


async def delete_legacy_project_record(
    *,
    organization_id: UUID,
    graph_project_id: str,
) -> bool:
    return await _call_runtime_helper(
        "delete_legacy_project_record",
        organization_id=organization_id,
        graph_project_id=graph_project_id,
    )


async def get_legacy_project_record_by_graph_id(
    *,
    organization_id: UUID,
    graph_project_id: str,
) -> Any:
    return await _call_runtime_helper(
        "get_legacy_project_record_by_graph_id",
        organization_id=organization_id,
        graph_project_id=graph_project_id,
    )


async def get_legacy_project_record_by_id(
    *,
    organization_id: UUID,
    project_id: UUID,
) -> Any:
    return await _call_runtime_helper(
        "get_legacy_project_record_by_id",
        organization_id=organization_id,
        project_id=project_id,
    )


async def resolve_legacy_auth_context(
    *,
    claims: dict[str, Any],
    session: Any | None = None,
) -> Any:
    return await _call_runtime_helper(
        "resolve_legacy_auth_context",
        claims=claims,
        session=session,
    )


async def list_legacy_accessible_project_graph_ids(ctx: Any) -> set[str] | None:
    return await _call_runtime_helper(
        "list_legacy_accessible_project_graph_ids",
        ctx=ctx,
    )


async def resolve_legacy_accessible_project_graph_ids(
    *,
    user_id: str,
    org_id: str,
    scopes: Any | None = None,
    api_key_project_ids: Any | None = None,
) -> set[str] | None:
    return await _call_runtime_helper(
        "resolve_legacy_accessible_project_graph_ids",
        user_id=user_id,
        org_id=org_id,
        scopes=scopes,
        api_key_project_ids=api_key_project_ids,
    )


async def verify_legacy_entity_project_access(
    *,
    ctx: Any,
    entity_project_id: str | None,
    required_role: Any,
) -> Any:
    return await _call_runtime_helper(
        "verify_legacy_entity_project_access",
        ctx=ctx,
        entity_project_id=entity_project_id,
        required_role=required_role,
    )


async def list_legacy_user_sessions(
    *,
    user_id: UUID,
    include_expired: bool = False,
):
    return await _call_runtime_helper(
        "list_legacy_user_sessions",
        user_id=user_id,
        include_expired=include_expired,
    )


async def revoke_all_legacy_user_sessions(
    *,
    user_id: UUID,
    exclude_token_hash: str | None = None,
) -> int:
    return await _call_runtime_helper(
        "revoke_all_legacy_user_sessions",
        user_id=user_id,
        exclude_token_hash=exclude_token_hash,
    )


async def revoke_legacy_user_session(
    *,
    user_id: UUID,
    session_id: UUID,
) -> bool:
    return await _call_runtime_helper(
        "revoke_legacy_user_session",
        user_id=user_id,
        session_id=session_id,
    )


async def request_legacy_password_reset(email: str) -> None:
    await _call_runtime_helper("request_legacy_password_reset", email=email)


async def confirm_legacy_password_reset(token: str, new_password: str) -> None:
    await _call_runtime_helper(
        "confirm_legacy_password_reset",
        token=token,
        new_password=new_password,
    )


async def list_legacy_oauth_connections(*, user_id: UUID):
    return await _call_runtime_helper(
        "list_legacy_oauth_connections",
        user_id=user_id,
    )


async def remove_legacy_oauth_connection(
    *,
    user_id: UUID,
    connection_id: UUID,
):
    return await _call_runtime_helper(
        "remove_legacy_oauth_connection",
        user_id=user_id,
        connection_id=connection_id,
    )


async def authenticate_api_key(raw_key: str):
    return await _call_backend_export("authenticate_legacy_api_key", raw_key)


async def authenticate_local_user(*, email: str, password: str):
    return await _call_backend_export(
        "authenticate_legacy_local_user",
        email=email,
        password=password,
    )


async def login_github_identity(**kwargs: object):
    return await _call_backend_export("login_legacy_github_identity", **kwargs)


async def signup_local_user(**kwargs: object):
    return await _call_backend_export("signup_legacy_local_user", **kwargs)


async def login_local_user(**kwargs: object):
    return await _call_backend_export("login_legacy_local_user", **kwargs)


async def start_device_authorization(**kwargs: object):
    return await _call_backend_export("start_legacy_device_authorization", **kwargs)


async def exchange_device_code(**kwargs: object):
    return await _call_backend_export("exchange_legacy_device_code", **kwargs)


async def get_device_request_by_user_code(user_code: str):
    return await _call_backend_export("get_legacy_device_request_by_user_code", user_code)


async def resolve_request_claims(request: Any) -> dict[str, Any] | None:
    return await _call_backend_export("resolve_legacy_request_claims", request)


async def resolve_request_user(request: Any):
    return await _call_backend_export("resolve_legacy_request_user", request)


async def login_device_browser_user(**kwargs: object):
    return await _call_backend_export("login_legacy_device_browser_user", **kwargs)


async def deny_device_authorization(**kwargs: object):
    return await _call_backend_export("deny_legacy_device_authorization", **kwargs)


async def approve_device_authorization(**kwargs: object):
    return await _call_backend_export("approve_legacy_device_authorization", **kwargs)


async def rotate_refresh_exchange(**kwargs: object):
    return await _call_backend_export("rotate_legacy_refresh_exchange", **kwargs)


async def revoke_access_session(token: str) -> None:
    await _call_backend_export("revoke_legacy_access_session", token)


async def log_audit_event(**kwargs: object) -> None:
    await _call_backend_export("log_legacy_audit_event", **kwargs)


async def list_api_keys_for_user(**kwargs: object):
    return await _call_backend_export("list_legacy_api_keys_for_user", **kwargs)


async def create_api_key_for_user(**kwargs: object):
    return await _call_backend_export("create_legacy_api_key_for_user", **kwargs)


async def revoke_api_key_for_user(**kwargs: object):
    return await _call_backend_export("revoke_legacy_api_key_for_user", **kwargs)


async def create_session_record(**kwargs: object):
    return await _call_backend_export("create_legacy_session_record", **kwargs)


async def load_refresh_session_record(refresh_token: str):
    return await _call_backend_export("load_legacy_refresh_session_record", refresh_token)


async def rotate_refresh_session_record(refresh_token: str, **kwargs: object):
    return await _call_backend_export(
        "rotate_legacy_refresh_session_record",
        refresh_token,
        **kwargs,
    )


async def revoke_refresh_session_record(refresh_token: str) -> None:
    await _call_backend_export("revoke_legacy_refresh_session_record", refresh_token)


async def ensure_personal_organization(*, user_id: UUID):
    return await _call_backend_export("ensure_legacy_personal_organization", user_id=user_id)


async def get_user_by_id(user_id: UUID):
    return await _call_backend_export("get_legacy_user_by_id", user_id)


async def resolve_auth_context(
    *,
    claims: dict[str, Any],
    session: Any | None = None,
) -> Any:
    return await resolve_legacy_auth_context(claims=claims, session=session)


async def list_user_organizations(*, user_id: UUID):
    return await _call_backend_export("list_legacy_user_organizations", user_id=user_id)


async def patch_auth_user(
    *,
    user_id: UUID,
    updates: dict[str, Any],
    organization_id: UUID | None,
    request: Any,
):
    return await patch_legacy_auth_user(
        user_id=user_id,
        updates=updates,
        organization_id=organization_id,
        request=request,
    )


async def update_auth_user(**kwargs: object):
    return await _call_backend_export("update_legacy_auth_user", **kwargs)


async def get_project_record_by_graph_id(
    *,
    organization_id: UUID,
    graph_project_id: str,
) -> Any:
    return await get_legacy_project_record_by_graph_id(
        organization_id=organization_id,
        graph_project_id=graph_project_id,
    )


async def create_project_record(
    *,
    organization_id: UUID,
    owner_user_id: UUID,
    graph_project_id: str,
    name: str,
    description: str | None = None,
) -> Any:
    return await create_legacy_project_record(
        organization_id=organization_id,
        owner_user_id=owner_user_id,
        graph_project_id=graph_project_id,
        name=name,
        description=description,
    )


async def update_project_record(
    *,
    organization_id: UUID,
    graph_project_id: str,
    name: str | None = None,
    description: str | None = None,
) -> bool:
    return await update_legacy_project_record(
        organization_id=organization_id,
        graph_project_id=graph_project_id,
        name=name,
        description=description,
    )


async def delete_project_record(
    *,
    organization_id: UUID,
    graph_project_id: str,
) -> bool:
    return await delete_legacy_project_record(
        organization_id=organization_id,
        graph_project_id=graph_project_id,
    )


async def get_project_record_by_id(
    *,
    organization_id: UUID,
    project_id: UUID,
) -> Any:
    return await get_legacy_project_record_by_id(
        organization_id=organization_id,
        project_id=project_id,
    )


async def list_accessible_project_graph_ids(ctx: Any) -> set[str] | None:
    return await list_legacy_accessible_project_graph_ids(ctx)


async def resolve_accessible_project_graph_ids(
    *,
    user_id: str,
    org_id: str,
    scopes: Any | None = None,
    api_key_project_ids: Any | None = None,
) -> set[str] | None:
    return await resolve_legacy_accessible_project_graph_ids(
        user_id=user_id,
        org_id=org_id,
        scopes=scopes,
        api_key_project_ids=api_key_project_ids,
    )


async def verify_entity_project_access(
    *,
    ctx: Any,
    entity_project_id: str | None,
    required_role: Any,
) -> Any:
    return await verify_legacy_entity_project_access(
        ctx=ctx,
        entity_project_id=entity_project_id,
        required_role=required_role,
    )


async def has_owner_membership(*, org_id: str, user_id: str | None) -> bool:
    return await _call_backend_export("has_legacy_owner_membership", org_id=org_id, user_id=user_id)


async def list_user_sessions(
    *,
    user_id: UUID,
    include_expired: bool = False,
):
    return await list_legacy_user_sessions(user_id=user_id, include_expired=include_expired)


async def revoke_all_user_sessions(
    *,
    user_id: UUID,
    exclude_token_hash: str | None = None,
) -> int:
    return await revoke_all_legacy_user_sessions(
        user_id=user_id,
        exclude_token_hash=exclude_token_hash,
    )


async def revoke_user_session(
    *,
    user_id: UUID,
    session_id: UUID,
) -> bool:
    return await revoke_legacy_user_session(user_id=user_id, session_id=session_id)


async def request_password_reset(email: str) -> None:
    await request_legacy_password_reset(email)


async def confirm_password_reset(token: str, new_password: str) -> None:
    await confirm_legacy_password_reset(token, new_password)


async def list_oauth_connections(*, user_id: UUID):
    return await list_legacy_oauth_connections(user_id=user_id)


async def remove_oauth_connection(
    *,
    user_id: UUID,
    connection_id: UUID,
):
    return await remove_legacy_oauth_connection(user_id=user_id, connection_id=connection_id)
