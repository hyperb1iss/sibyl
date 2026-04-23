"""Pure runtime-shape helpers shared across settings and CLI flows."""

from __future__ import annotations

from typing import Literal, cast

RuntimeStore = Literal["legacy", "surreal"]
AuthStore = Literal["postgres", "surreal"]
ConfiguredCoordinationBackend = Literal["auto", "local", "redis"]
ResolvedCoordinationBackend = Literal["local", "redis"]


def default_auth_store(*, store: RuntimeStore) -> AuthStore:
    return "surreal" if store == "surreal" else "postgres"


def requires_surreal_support(*, store: RuntimeStore, auth_store: AuthStore) -> bool:
    return store == "surreal" or auth_store == "surreal"


def fully_surreal_runtime(*, store: RuntimeStore, auth_store: AuthStore) -> bool:
    return store == "surreal" and auth_store == "surreal"


def uses_relational_auth(*, auth_store: AuthStore) -> bool:
    return auth_store == "postgres"


def requires_relational_support(*, store: RuntimeStore, auth_store: AuthStore) -> bool:
    return store == "legacy" or uses_relational_auth(auth_store=auth_store)


def resolve_coordination_backend(
    *,
    store: RuntimeStore,
    coordination_backend: ConfiguredCoordinationBackend,
) -> ResolvedCoordinationBackend:
    if coordination_backend == "auto":
        return "redis" if store == "legacy" else "local"
    return coordination_backend


def resolve_object_store(value: object, *, default: RuntimeStore) -> RuntimeStore:
    store = getattr(value, "store", None)
    if store in {"legacy", "surreal"}:
        return cast("RuntimeStore", store)
    return default


def resolve_object_auth_store(
    value: object,
    *,
    default_store: RuntimeStore = "surreal",
) -> AuthStore:
    auth_store = getattr(value, "auth_store", None)
    if auth_store in {"postgres", "surreal"}:
        return cast("AuthStore", auth_store)
    return default_auth_store(store=resolve_object_store(value, default=default_store))


def uses_object_relational_auth(
    value: object,
    *,
    default_store: RuntimeStore = "surreal",
) -> bool:
    return uses_relational_auth(
        auth_store=resolve_object_auth_store(value, default_store=default_store)
    )


def resolve_object_coordination_backend(
    value: object,
    *,
    default_store: RuntimeStore = "surreal",
) -> ResolvedCoordinationBackend:
    backend = getattr(value, "resolved_coordination_backend", None)
    if backend in {"local", "redis"}:
        return cast("ResolvedCoordinationBackend", backend)

    configured_backend = getattr(value, "coordination_backend", None)
    if configured_backend not in {"auto", "local", "redis"}:
        configured_backend = "auto"

    return resolve_coordination_backend(
        store=resolve_object_store(value, default=default_store),
        coordination_backend=cast("ConfiguredCoordinationBackend", configured_backend),
    )
