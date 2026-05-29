from types import SimpleNamespace

from sibyl.runtime_shape import (
    resolve_coordination_backend,
    resolve_object_coordination_backend,
)


def test_resolve_coordination_backend_maps_auto_to_local() -> None:
    assert resolve_coordination_backend(coordination_backend="auto") == "local"


def test_resolve_coordination_backend_passes_redis_through() -> None:
    assert resolve_coordination_backend(coordination_backend="redis") == "redis"


def test_resolve_object_coordination_backend_prefers_resolved_field() -> None:
    runtime = SimpleNamespace(
        coordination_backend="auto",
        resolved_coordination_backend="redis",
    )
    assert resolve_object_coordination_backend(runtime) == "redis"


def test_resolve_object_coordination_backend_falls_back_to_configured() -> None:
    runtime = SimpleNamespace(coordination_backend="redis")
    assert resolve_object_coordination_backend(runtime) == "redis"


def test_resolve_object_coordination_backend_defaults_auto_to_local() -> None:
    runtime = SimpleNamespace(coordination_backend="auto")
    assert resolve_object_coordination_backend(runtime) == "local"
