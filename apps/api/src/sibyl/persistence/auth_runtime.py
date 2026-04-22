"""Active auth runtime adapters for the configured auth backend."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

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
        resolve_legacy_request_claims,
        resolve_legacy_request_user,
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
    "surreal": "sibyl.persistence.surreal.auth",
}

_BACKEND_NAME_OVERRIDES = {
    "surreal": {
        "LegacyAuthContextResolver": "SurrealAuthContextResolver",
        "LegacyOrganizationMembershipRepository": "SurrealOrganizationMembershipRepository",
        "LegacyOrganizationRepository": "SurrealOrganizationRepository",
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
    "create_legacy_session_record",
    "deny_legacy_device_authorization",
    "ensure_legacy_personal_organization",
    "exchange_legacy_device_code",
    "get_legacy_device_request_by_user_code",
    "get_legacy_user_by_id",
    "has_legacy_owner_membership",
    "list_legacy_accessible_project_graph_ids",
    "list_legacy_api_keys_for_user",
    "list_legacy_user_organizations",
    "load_legacy_refresh_session_record",
    "log_legacy_audit_event",
    "login_legacy_device_browser_user",
    "login_legacy_github_identity",
    "login_legacy_local_user",
    "resolve_legacy_accessible_project_graph_ids",
    "resolve_legacy_request_claims",
    "resolve_legacy_request_user",
    "revoke_legacy_access_session",
    "revoke_legacy_api_key_for_user",
    "revoke_legacy_refresh_session_record",
    "rotate_legacy_refresh_exchange",
    "rotate_legacy_refresh_session_record",
    "signup_legacy_local_user",
    "start_legacy_device_authorization",
    "update_legacy_auth_user",
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
        "Keep SIBYL_AUTH_STORE=postgres for request-time auth flows until the "
        "remaining Surreal adapters land."
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
