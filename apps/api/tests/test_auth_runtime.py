from __future__ import annotations

from sibyl.persistence import auth_runtime
from sibyl.persistence.auth_common import InvalidAuthClaimsError, UserNotFoundError
from sibyl.persistence.surreal import auth_runtime as surreal_auth_runtime
from sibyl.persistence.surreal.auth import SurrealAuthContextResolver
from sibyl.persistence.surreal.auth_runtime import SurrealSessionRepository


def test_auth_runtime_uses_shared_error_types() -> None:
    assert auth_runtime.InvalidAuthClaimsError is InvalidAuthClaimsError
    assert auth_runtime.UserNotFoundError is UserNotFoundError
    assert auth_runtime.AuthContextResolver is SurrealAuthContextResolver
    assert auth_runtime.SessionRepository is SurrealSessionRepository
    assert "AuthContextResolver" in dir(auth_runtime)


def test_auth_runtime_maps_resolver_name_for_surreal() -> None:
    assert auth_runtime.AuthContextResolver is SurrealAuthContextResolver
    assert auth_runtime.SessionRepository is SurrealSessionRepository


def test_auth_runtime_exports_neutral_runtime_surface() -> None:
    assert "resolve_auth_context" in auth_runtime.__all__
    assert "patch_auth_user" in auth_runtime.__all__
    assert "list_oauth_connections" in auth_runtime.__all__
    assert "validate_access_session" in auth_runtime.__all__

    assert "AuthContextResolver" in surreal_auth_runtime.__all__
    assert "authenticate_api_key" in surreal_auth_runtime.__all__
    assert "SessionRepository" in surreal_auth_runtime.__all__
    assert "list_accessible_project_graph_ids" in surreal_auth_runtime.__all__
    assert "verify_entity_project_access" in surreal_auth_runtime.__all__
    assert "validate_access_session" in surreal_auth_runtime.__all__
    assert surreal_auth_runtime.SessionRepository is SurrealSessionRepository


def test_auth_runtime_surreal_backend_covers_public_exports() -> None:
    skipped = {"InvalidAuthClaimsError", "UserNotFoundError"}

    for name in auth_runtime.__all__:
        if name in skipped:
            continue
        assert hasattr(surreal_auth_runtime, name), name
