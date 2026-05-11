from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import MagicMock

from sibyl.cli import up_cmd


def _clear_runtime_env(monkeypatch) -> None:
    for key in (
        "SIBYL_STORE",
        "SIBYL_AUTH_STORE",
        "SIBYL_COORDINATION_BACKEND",
        "SIBYL_SURREAL_URL",
        "SIBYL_SURREAL_PORT",
        "SIBYL_SURREAL_USERNAME",
        "SIBYL_SURREAL_PASSWORD",
        "SIBYL_REDIS_HOST",
        "SIBYL_REDIS_PORT",
        "SIBYL_REDIS_PASSWORD",
        "SIBYL_RUN_WORKER",
    ):
        monkeypatch.delenv(key, raising=False)


def _prepare_up_command(monkeypatch, tmp_path: Path) -> tuple[MagicMock, MagicMock]:
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    run_compose = MagicMock(return_value=CompletedProcess(["docker"], 0, "", ""))
    start_foreground = MagicMock()

    monkeypatch.setattr(up_cmd, "_find_project_root", lambda: tmp_path)
    monkeypatch.setattr(up_cmd, "_check_docker", lambda: True)
    monkeypatch.setattr(up_cmd, "_run_docker_compose", run_compose)
    monkeypatch.setattr(up_cmd, "_start_server_foreground", start_foreground)
    monkeypatch.setattr(up_cmd.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(up_cmd.console, "status", lambda *_args, **_kwargs: nullcontext())

    return run_compose, start_foreground


def test_up_defaults_to_surreal_local_without_redis(tmp_path: Path, monkeypatch) -> None:
    _clear_runtime_env(monkeypatch)
    run_compose, start_foreground = _prepare_up_command(monkeypatch, tmp_path)

    up_cmd.up()

    run_compose.assert_called_once_with(["up", "-d", "surrealdb"], tmp_path)
    env = start_foreground.call_args.args[2]
    assert env["SIBYL_STORE"] == "surreal"
    assert env["SIBYL_AUTH_STORE"] == "surreal"
    assert up_cmd._resolve_coordination_backend(env) == "local"
    assert env["SIBYL_SURREAL_URL"] == "ws://127.0.0.1:8000/rpc"
    assert "SIBYL_REDIS_HOST" not in env


def test_coordination_backend_helper_defaults_to_surreal_local() -> None:
    assert up_cmd._resolve_coordination_backend({}) == "local"


def test_up_starts_redis_when_surreal_coordination_backend_is_redis(
    tmp_path: Path, monkeypatch
) -> None:
    _clear_runtime_env(monkeypatch)
    (tmp_path / ".env").write_text("SIBYL_COORDINATION_BACKEND=redis\n")
    run_compose, start_foreground = _prepare_up_command(monkeypatch, tmp_path)

    up_cmd.up()

    run_compose.assert_called_once_with(
        ["up", "-d", "surrealdb", "redis"],
        tmp_path,
    )
    env = start_foreground.call_args.args[2]
    assert env["SIBYL_COORDINATION_BACKEND"] == "redis"
    assert env["SIBYL_REDIS_HOST"] == "127.0.0.1"
    assert env["SIBYL_REDIS_PORT"] == "6381"


def test_up_ignores_removed_postgres_auth_store(tmp_path: Path, monkeypatch) -> None:
    _clear_runtime_env(monkeypatch)
    (tmp_path / ".env").write_text("SIBYL_AUTH_STORE=postgres\n")
    run_compose, start_foreground = _prepare_up_command(monkeypatch, tmp_path)

    up_cmd.up()

    run_compose.assert_called_once_with(["up", "-d", "surrealdb"], tmp_path)
    env = start_foreground.call_args.args[2]
    assert env["SIBYL_STORE"] == "surreal"
    assert env["SIBYL_AUTH_STORE"] == "surreal"


def test_up_starts_legacy_stack_when_store_is_legacy(tmp_path: Path, monkeypatch) -> None:
    _clear_runtime_env(monkeypatch)
    (tmp_path / ".env").write_text("SIBYL_STORE=legacy\n")
    run_compose, start_foreground = _prepare_up_command(monkeypatch, tmp_path)

    up_cmd.up()

    run_compose.assert_called_once_with(
        ["up", "-d", "falkordb", "surrealdb", "postgres", "redis"], tmp_path
    )
    env = start_foreground.call_args.args[2]
    assert env["SIBYL_STORE"] == "legacy"
    assert env["SIBYL_AUTH_STORE"] == "surreal"
    assert up_cmd._resolve_coordination_backend(env) == "redis"
    assert env["SIBYL_REDIS_HOST"] == "127.0.0.1"


def test_warn_if_relational_runtime_is_quiet_for_fully_surreal(monkeypatch) -> None:
    warn = MagicMock()
    info = MagicMock()

    monkeypatch.setattr(up_cmd, "warn", warn)
    monkeypatch.setattr(up_cmd, "info", info)

    up_cmd._warn_if_relational_runtime({"SIBYL_STORE": "surreal", "SIBYL_AUTH_STORE": "surreal"})

    warn.assert_not_called()
    info.assert_not_called()


def test_warn_if_relational_runtime_recommends_surreal(monkeypatch) -> None:
    warn = MagicMock()
    info = MagicMock()

    monkeypatch.setattr(up_cmd, "warn", warn)
    monkeypatch.setattr(up_cmd, "info", info)

    up_cmd._warn_if_relational_runtime({"SIBYL_STORE": "legacy", "SIBYL_AUTH_STORE": "surreal"})

    warn.assert_called_once_with(
        "Legacy relational runtime is deprecated; migrate this install to SurrealDB."
    )
    info.assert_any_call("Recommended settings: SIBYL_STORE=surreal and SIBYL_AUTH_STORE=surreal")
    info.assert_any_call("Local single-org migration: moon run dev -- --migrate-legacy")
    info.assert_any_call("Migration playbook: docs/guide/migrating-from-falkor.md")


def test_configure_requested_worker_mode_skips_extra_worker_for_local_runtime(
    monkeypatch,
) -> None:
    info = MagicMock()
    warn = MagicMock()

    monkeypatch.setattr(up_cmd, "info", info)
    monkeypatch.setattr(up_cmd, "warn", warn)

    env = {"SIBYL_STORE": "surreal", "SIBYL_COORDINATION_BACKEND": "auto"}
    up_cmd._configure_requested_worker_mode(env, with_worker=True)

    assert "SIBYL_RUN_WORKER" not in env
    info.assert_called_once_with("Local coordination already runs jobs and schedules in-process")
    warn.assert_not_called()


def test_configure_requested_worker_mode_defaults_to_local_runtime(monkeypatch) -> None:
    info = MagicMock()
    warn = MagicMock()

    monkeypatch.setattr(up_cmd, "info", info)
    monkeypatch.setattr(up_cmd, "warn", warn)

    env: dict[str, str] = {}
    up_cmd._configure_requested_worker_mode(env, with_worker=True)

    assert "SIBYL_RUN_WORKER" not in env
    info.assert_called_once_with("Local coordination already runs jobs and schedules in-process")
    warn.assert_not_called()


def test_configure_requested_worker_mode_embeds_worker_in_legacy_mode(monkeypatch) -> None:
    info = MagicMock()
    warn = MagicMock()

    monkeypatch.setattr(up_cmd, "info", info)
    monkeypatch.setattr(up_cmd, "warn", warn)

    env = {"SIBYL_STORE": "legacy", "SIBYL_COORDINATION_BACKEND": "auto"}
    up_cmd._configure_requested_worker_mode(env, with_worker=True)

    assert env["SIBYL_RUN_WORKER"] == "true"
    info.assert_called_once_with("Worker mode: running embedded arq worker in the API process")
    warn.assert_not_called()


def test_configure_requested_worker_mode_warns_for_surreal_redis(monkeypatch) -> None:
    info = MagicMock()
    warn = MagicMock()

    monkeypatch.setattr(up_cmd, "info", info)
    monkeypatch.setattr(up_cmd, "warn", warn)

    env = {"SIBYL_STORE": "surreal", "SIBYL_COORDINATION_BACKEND": "redis"}
    up_cmd._configure_requested_worker_mode(env, with_worker=True)

    assert "SIBYL_RUN_WORKER" not in env
    warn.assert_called_once_with("`--with-worker` is only supported in legacy mode")
    info.assert_called_once_with(
        "Run `moon run api:worker` or `uv run sibyld worker` in another shell."
    )
