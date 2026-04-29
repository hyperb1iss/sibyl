"""Active auth runtime adapters for the configured auth backend."""

from __future__ import annotations

from importlib import import_module
from typing import Any
from uuid import UUID

from sibyl.config import settings
from sibyl.persistence.auth_common import InvalidAuthClaimsError, UserNotFoundError

_BACKEND_MODULES = {
    "postgres": "sibyl.persistence.legacy.auth_runtime",
    "surreal": "sibyl.persistence.surreal.auth_runtime",
}

_DYNAMIC_EXPORTS = (
    "AuthContextResolver",
    "OrganizationMembershipRepository",
    "OrganizationRepository",
    "SessionRepository",
    "UserRepository",
)

__all__ = list(_DYNAMIC_EXPORTS)
__all__ += [
    "InvalidAuthClaimsError",
    "UserNotFoundError",
    "approve_device_authorization",
    "authenticate_api_key",
    "authenticate_local_user",
    "confirm_password_reset",
    "create_api_key_for_user",
    "create_project_record",
    "create_session_record",
    "delete_project_record",
    "deny_device_authorization",
    "ensure_personal_organization",
    "exchange_device_code",
    "get_device_request_by_user_code",
    "get_project_record_by_graph_id",
    "get_project_record_by_id",
    "get_user_by_id",
    "has_owner_membership",
    "list_accessible_project_graph_ids",
    "list_api_keys_for_user",
    "list_oauth_connections",
    "list_user_organizations",
    "list_user_sessions",
    "load_refresh_session_record",
    "log_audit_event",
    "login_device_browser_user",
    "login_github_identity",
    "login_local_user",
    "patch_auth_user",
    "remove_oauth_connection",
    "request_password_reset",
    "resolve_accessible_project_graph_ids",
    "resolve_auth_context",
    "resolve_request_claims",
    "resolve_request_user",
    "revoke_access_session",
    "revoke_all_user_sessions",
    "revoke_api_key_for_user",
    "revoke_refresh_session_record",
    "revoke_user_session",
    "rotate_refresh_exchange",
    "rotate_refresh_session_record",
    "signup_local_user",
    "start_device_authorization",
    "update_auth_user",
    "update_project_record",
    "verify_entity_project_access",
]


def _active_backend_name() -> str:
    return settings.auth_store


def _resolve_backend_export(name: str) -> Any:
    backend = _active_backend_name()
    module = import_module(_BACKEND_MODULES[backend])
    if hasattr(module, name):
        return getattr(module, name)
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
    if name in _DYNAMIC_EXPORTS:
        return _resolve_backend_export(name)
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))


async def _call_runtime_helper(export_name: str, **kwargs: object) -> Any:
    return await _call_backend_export(export_name, **kwargs)


async def _call_backend_export(export_name: str, *args: object, **kwargs: object) -> Any:
    export = _resolve_backend_export(export_name)
    return await export(*args, **kwargs)


async def authenticate_api_key(raw_key: str):
    return await _call_backend_export("authenticate_api_key", raw_key)


async def authenticate_local_user(*, email: str, password: str):
    return await _call_backend_export(
        "authenticate_local_user",
        email=email,
        password=password,
    )


async def login_github_identity(**kwargs: object):
    return await _call_backend_export("login_github_identity", **kwargs)


async def signup_local_user(**kwargs: object):
    return await _call_backend_export("signup_local_user", **kwargs)


async def login_local_user(**kwargs: object):
    return await _call_backend_export("login_local_user", **kwargs)


async def start_device_authorization(**kwargs: object):
    return await _call_backend_export("start_device_authorization", **kwargs)


async def exchange_device_code(**kwargs: object):
    return await _call_backend_export("exchange_device_code", **kwargs)


async def get_device_request_by_user_code(user_code: str):
    return await _call_backend_export("get_device_request_by_user_code", user_code)


async def resolve_request_claims(request: Any) -> dict[str, Any] | None:
    return await _call_backend_export("resolve_request_claims", request)


async def resolve_request_user(request: Any):
    return await _call_backend_export("resolve_request_user", request)


async def login_device_browser_user(**kwargs: object):
    return await _call_backend_export("login_device_browser_user", **kwargs)


async def deny_device_authorization(**kwargs: object):
    return await _call_backend_export("deny_device_authorization", **kwargs)


async def approve_device_authorization(**kwargs: object):
    return await _call_backend_export("approve_device_authorization", **kwargs)


async def rotate_refresh_exchange(**kwargs: object):
    return await _call_backend_export("rotate_refresh_exchange", **kwargs)


async def revoke_access_session(token: str) -> None:
    await _call_backend_export("revoke_access_session", token)


async def log_audit_event(**kwargs: object) -> None:
    await _call_backend_export("log_audit_event", **kwargs)


async def list_api_keys_for_user(**kwargs: object):
    return await _call_backend_export("list_api_keys_for_user", **kwargs)


async def create_api_key_for_user(**kwargs: object):
    return await _call_backend_export("create_api_key_for_user", **kwargs)


async def revoke_api_key_for_user(**kwargs: object):
    return await _call_backend_export("revoke_api_key_for_user", **kwargs)


async def create_session_record(**kwargs: object):
    return await _call_backend_export("create_session_record", **kwargs)


async def load_refresh_session_record(refresh_token: str):
    return await _call_backend_export("load_refresh_session_record", refresh_token)


async def rotate_refresh_session_record(refresh_token: str, **kwargs: object):
    return await _call_backend_export(
        "rotate_refresh_session_record",
        refresh_token,
        **kwargs,
    )


async def revoke_refresh_session_record(refresh_token: str) -> None:
    await _call_backend_export("revoke_refresh_session_record", refresh_token)


async def ensure_personal_organization(*, user_id: UUID):
    return await _call_backend_export("ensure_personal_organization", user_id=user_id)


async def get_user_by_id(user_id: UUID):
    return await _call_backend_export("get_user_by_id", user_id)


async def resolve_auth_context(
    *,
    claims: dict[str, Any],
    session: Any | None = None,
) -> Any:
    return await _call_runtime_helper(
        "resolve_auth_context",
        claims=claims,
        session=session,
    )


async def list_user_organizations(*, user_id: UUID):
    return await _call_backend_export("list_user_organizations", user_id=user_id)


async def patch_auth_user(
    *,
    user_id: UUID,
    updates: dict[str, Any],
    organization_id: UUID | None,
    request: Any,
):
    return await _call_runtime_helper(
        "patch_auth_user",
        user_id=user_id,
        updates=updates,
        organization_id=organization_id,
        request=request,
    )


async def update_auth_user(**kwargs: object):
    return await _call_backend_export("update_auth_user", **kwargs)


async def get_project_record_by_graph_id(
    *,
    organization_id: UUID,
    graph_project_id: str,
) -> Any:
    return await _call_runtime_helper(
        "get_project_record_by_graph_id",
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
    return await _call_runtime_helper(
        "create_project_record",
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
    return await _call_runtime_helper(
        "update_project_record",
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
    return await _call_runtime_helper(
        "delete_project_record",
        organization_id=organization_id,
        graph_project_id=graph_project_id,
    )


async def get_project_record_by_id(
    *,
    organization_id: UUID,
    project_id: UUID,
) -> Any:
    return await _call_runtime_helper(
        "get_project_record_by_id",
        organization_id=organization_id,
        project_id=project_id,
    )


async def list_accessible_project_graph_ids(ctx: Any) -> set[str] | None:
    return await _call_runtime_helper(
        "list_accessible_project_graph_ids",
        ctx=ctx,
    )


async def resolve_accessible_project_graph_ids(
    *,
    user_id: str,
    org_id: str,
    scopes: Any | None = None,
    api_key_project_ids: Any | None = None,
) -> set[str] | None:
    return await _call_runtime_helper(
        "resolve_accessible_project_graph_ids",
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
    return await _call_runtime_helper(
        "verify_entity_project_access",
        ctx=ctx,
        entity_project_id=entity_project_id,
        required_role=required_role,
    )


async def has_owner_membership(*, org_id: str, user_id: str | None) -> bool:
    return await _call_backend_export("has_owner_membership", org_id=org_id, user_id=user_id)


async def list_user_sessions(
    *,
    user_id: UUID,
    include_expired: bool = False,
):
    return await _call_runtime_helper(
        "list_user_sessions",
        user_id=user_id,
        include_expired=include_expired,
    )


async def revoke_all_user_sessions(
    *,
    user_id: UUID,
    exclude_token_hash: str | None = None,
) -> int:
    return await _call_runtime_helper(
        "revoke_all_user_sessions",
        user_id=user_id,
        exclude_token_hash=exclude_token_hash,
    )


async def revoke_user_session(
    *,
    user_id: UUID,
    session_id: UUID,
) -> bool:
    return await _call_runtime_helper(
        "revoke_user_session",
        user_id=user_id,
        session_id=session_id,
    )


async def request_password_reset(email: str) -> None:
    await _call_runtime_helper("request_password_reset", email=email)


async def confirm_password_reset(token: str, new_password: str) -> None:
    await _call_runtime_helper(
        "confirm_password_reset",
        token=token,
        new_password=new_password,
    )


async def list_oauth_connections(*, user_id: UUID):
    return await _call_runtime_helper(
        "list_oauth_connections",
        user_id=user_id,
    )


async def remove_oauth_connection(
    *,
    user_id: UUID,
    connection_id: UUID,
):
    return await _call_runtime_helper(
        "remove_oauth_connection",
        user_id=user_id,
        connection_id=connection_id,
    )
