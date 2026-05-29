"""Surreal-backed request-time auth adapters.

This package splits the former ``auth_runtime`` god-module into cohesive domain
submodules while keeping the original import path stable: every name that was
importable from ``sibyl.persistence.surreal.auth_runtime`` is re-exported here.
Shared primitives live in :mod:`._common`; domain modules import from it and,
where one domain calls another at runtime, from each other along an acyclic
graph (``sessions`` -> ``api_keys``/``users``, ``users`` -> ``audit``,
``device_authorization`` -> ``login``).
"""

from __future__ import annotations

from sibyl.persistence.surreal.auth import (
    SurrealAuthContextResolver,
    SurrealOrganizationMembershipRepository,
    SurrealOrganizationRepository,
    SurrealUserRepository,
    build_surreal_auth_client,
)
from sibyl.persistence.surreal.auth_runtime._common import (
    DeviceBrowserLogin,
    IssuedAuthSession,
    IssuedOidcSession,
    RefreshRotation,
    SurrealSessionRepository,
    UserDeletionRequestResult,
    _auth_client_scope,  # noqa: F401
    _auth_user_namespace,  # noqa: F401
    _coerce_datetime,  # noqa: F401
    _hash_reset_token,  # noqa: F401
    _issue_auth_session,  # noqa: F401
    _list_user_org_records,  # noqa: F401
    _log_audit_event,  # noqa: F401
    _log_login_history,  # noqa: F401
    _SurrealRepository,  # noqa: F401
    _utcnow,  # noqa: F401
    config_module,  # noqa: F401
)
from sibyl.persistence.surreal.auth_runtime.api_keys import (
    authenticate_api_key,
    create_api_key_for_user,
    list_api_keys_for_user,
    revoke_api_key_for_user,
)
from sibyl.persistence.surreal.auth_runtime.audit import (
    list_audit_events,
    list_memory_audit_events,
    log_audit_event,
    log_memory_audit_event,
)
from sibyl.persistence.surreal.auth_runtime.device_authorization import (
    approve_device_authorization,
    deny_device_authorization,
    exchange_device_code,
    get_device_request_by_user_code,
    login_device_browser_user,
    start_device_authorization,
)
from sibyl.persistence.surreal.auth_runtime.login import (
    _break_glass_audit_details,  # noqa: F401
    delete_failed_local_signup_user,
    login_github_identity,
    login_local_user,
    login_oidc_identity,
    signup_local_user,
)
from sibyl.persistence.surreal.auth_runtime.oauth_connections import (
    list_oauth_connections,
    remove_oauth_connection,
)
from sibyl.persistence.surreal.auth_runtime.password_reset import (
    confirm_password_reset,
    request_password_reset,
)

# Re-exported for sibyl.cli.bootstrap, which depends on these scope helpers.
from sibyl.persistence.surreal.auth_runtime.projects import (  # noqa: F401
    _memory_space_scope_key,
    _memory_space_state,
    add_memory_space_member,
    create_memory_space,
    create_project_record,
    delete_project_record,
    get_memory_space,
    get_project_record_by_graph_id,
    get_project_record_by_id,
    has_owner_membership,
    list_accessible_project_graph_ids,
    list_memory_space_members,
    list_memory_spaces,
    resolve_accessible_project_graph_ids,
    resolve_org_role,
    update_memory_space,
    update_project_record,
    verify_entity_project_access,
)
from sibyl.persistence.surreal.auth_runtime.sessions import (
    create_session_record,
    list_user_sessions,
    load_oauth_client_registration,
    load_refresh_session_record,
    resolve_auth_context,
    resolve_request_claims,
    resolve_request_user,
    revoke_access_session,
    revoke_all_user_sessions,
    revoke_refresh_session_record,
    revoke_user_session,
    rotate_refresh_exchange,
    rotate_refresh_session_record,
    save_oauth_client_registration,
    validate_access_session,
)
from sibyl.persistence.surreal.auth_runtime.users import (
    _apply_password_change,  # noqa: F401
    authenticate_local_user,
    ensure_personal_organization,
    get_user_by_id,
    list_user_organizations,
    patch_auth_user,
    request_user_deletion,
    update_auth_user,
)

__all__ = [
    "AuthContextResolver",
    "DeviceBrowserLogin",
    "IssuedAuthSession",
    "IssuedOidcSession",
    "OrganizationMembershipRepository",
    "OrganizationRepository",
    "RefreshRotation",
    "SessionRepository",
    "SurrealAuthContextResolver",
    "SurrealOrganizationMembershipRepository",
    "SurrealOrganizationRepository",
    "SurrealSessionRepository",
    "SurrealUserRepository",
    "UserDeletionRequestResult",
    "UserRepository",
    "approve_device_authorization",
    "authenticate_api_key",
    "authenticate_local_user",
    "build_surreal_auth_client",
    "confirm_password_reset",
    "add_memory_space_member",
    "create_api_key_for_user",
    "create_memory_space",
    "create_project_record",
    "create_session_record",
    "delete_project_record",
    "delete_failed_local_signup_user",
    "deny_device_authorization",
    "ensure_personal_organization",
    "exchange_device_code",
    "get_device_request_by_user_code",
    "get_memory_space",
    "get_project_record_by_graph_id",
    "get_project_record_by_id",
    "get_user_by_id",
    "has_owner_membership",
    "list_accessible_project_graph_ids",
    "list_audit_events",
    "list_api_keys_for_user",
    "list_memory_audit_events",
    "list_memory_space_members",
    "list_memory_spaces",
    "list_oauth_connections",
    "list_user_organizations",
    "list_user_sessions",
    "load_oauth_client_registration",
    "load_refresh_session_record",
    "log_audit_event",
    "log_memory_audit_event",
    "login_device_browser_user",
    "login_github_identity",
    "login_local_user",
    "login_oidc_identity",
    "patch_auth_user",
    "remove_oauth_connection",
    "request_user_deletion",
    "request_password_reset",
    "resolve_accessible_project_graph_ids",
    "resolve_auth_context",
    "resolve_org_role",
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
    "save_oauth_client_registration",
    "start_device_authorization",
    "update_auth_user",
    "update_memory_space",
    "update_project_record",
    "validate_access_session",
    "verify_entity_project_access",
]


AuthContextResolver = SurrealAuthContextResolver
OrganizationMembershipRepository = SurrealOrganizationMembershipRepository
OrganizationRepository = SurrealOrganizationRepository
SessionRepository = SurrealSessionRepository
UserRepository = SurrealUserRepository


# --- Flat-namespace compatibility shim -------------------------------------
#
# This module used to be a single flat module. Existing callers and tests reach
# for names through this path (``from ...auth_runtime import X`` and
# ``auth_runtime.X``), and the test suite monkeypatches dependency seams on this
# module (``monkeypatch.setattr(auth_runtime, "_log_audit_event", fake)``) then
# invokes a public function and asserts the patched seam was used.
#
# After splitting the implementation into submodules, each function references
# its seams through its own module globals, so a patch applied only to this
# package would not reach them. To keep the old flat behavior exactly:
#   * reads of any name fall through to the owning submodule (``__getattr__``);
#   * writes of a name propagate to every submodule (and ``_common``) that binds
#     it, so a seam patched here is observed at the real call sites.
from types import ModuleType as _ModuleType  # noqa: E402

from sibyl.persistence.surreal.auth_runtime import (  # noqa: E402
    _common as _common_module,
    api_keys as _api_keys_module,
    audit as _audit_module,
    device_authorization as _device_authorization_module,
    login as _login_module,
    oauth_connections as _oauth_connections_module,
    password_reset as _password_reset_module,
    projects as _projects_module,
    sessions as _sessions_module,
    users as _users_module,
)

_SUBMODULES: tuple[_ModuleType, ...] = (
    _common_module,
    _api_keys_module,
    _audit_module,
    _device_authorization_module,
    _login_module,
    _oauth_connections_module,
    _password_reset_module,
    _projects_module,
    _sessions_module,
    _users_module,
)


def __getattr__(name: str) -> object:
    for module in _SUBMODULES:
        try:
            return getattr(module, name)
        except AttributeError:
            continue
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)


class _AuthRuntimePackage(_ModuleType):
    def __setattr__(self, name: str, value: object) -> None:
        super().__setattr__(name, value)
        for module in _SUBMODULES:
            if name in vars(module):
                setattr(module, name, value)


import sys as _sys  # noqa: E402

_sys.modules[__name__].__class__ = _AuthRuntimePackage
