from types import SimpleNamespace

from sibyl.runtime_shape import (
    default_auth_store,
    fully_surreal_runtime,
    requires_object_relational_support,
    requires_object_surreal_support,
    requires_relational_support,
    requires_surreal_support,
    resolve_coordination_backend,
    resolve_object_auth_store,
    resolve_object_coordination_backend,
    uses_object_relational_auth,
    uses_relational_auth,
)


def test_runtime_shape_helpers_cover_fully_surreal_mode() -> None:
    assert default_auth_store(store="surreal") == "surreal"
    assert fully_surreal_runtime(store="surreal", auth_store="surreal") is True
    assert requires_surreal_support(store="surreal", auth_store="surreal") is True
    assert uses_relational_auth(auth_store="surreal") is False
    assert requires_relational_support(store="surreal", auth_store="surreal") is False
    assert resolve_coordination_backend(store="surreal", coordination_backend="auto") == "local"


def test_runtime_shape_helpers_cover_mixed_surreal_mode() -> None:
    assert default_auth_store(store="legacy") == "surreal"
    assert fully_surreal_runtime(store="legacy", auth_store="surreal") is False
    assert requires_surreal_support(store="legacy", auth_store="surreal") is True
    assert uses_relational_auth(auth_store="postgres") is False
    assert requires_relational_support(store="surreal", auth_store="postgres") is False


def test_runtime_shape_object_helpers_fall_back_to_store_defaults() -> None:
    runtime = SimpleNamespace(store="legacy", coordination_backend="auto")

    assert resolve_object_auth_store(runtime, default_store="surreal") == "surreal"
    assert requires_object_surreal_support(runtime, default_store="surreal") is True
    assert requires_object_relational_support(runtime, default_store="surreal") is False
    assert uses_object_relational_auth(runtime, default_store="surreal") is False
    assert resolve_object_coordination_backend(runtime, default_store="surreal") == "redis"


def test_runtime_shape_object_helpers_prefer_resolved_fields() -> None:
    runtime = SimpleNamespace(
        store="legacy",
        auth_store="surreal",
        coordination_backend="auto",
        resolved_coordination_backend="local",
    )

    assert resolve_object_auth_store(runtime) == "surreal"
    assert requires_object_surreal_support(runtime) is True
    assert requires_object_relational_support(runtime) is False
    assert uses_object_relational_auth(runtime) is False
    assert resolve_object_coordination_backend(runtime) == "local"
