from __future__ import annotations

import os
import sys
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import MagicMock

from typer.testing import CliRunner

from sibyl.cli.main import app

runner = CliRunner()
cli_main = import_module("sibyl.cli.main")


def _clear_embedded_runtime_env(monkeypatch) -> None:
    for key in (
        "SIBYL_STORE",
        "SIBYL_AUTH_STORE",
        "SIBYL_COORDINATION_BACKEND",
        "SIBYL_ALLOW_EMBEDDED_SINGLE_WRITER",
        "SIBYL_SURREAL_URL",
    ):
        if key not in os.environ:
            monkeypatch.setenv(key, "")
        monkeypatch.delenv(key, raising=False)


def test_top_level_version_uses_package_metadata(monkeypatch) -> None:
    monkeypatch.setattr(cli_main, "pkg_version", lambda package_name: "9.9.9")

    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.output.strip() == "sibyld 9.9.9"


def test_version_command_uses_package_metadata(monkeypatch) -> None:
    monkeypatch.setattr(cli_main, "pkg_version", lambda package_name: "9.9.9")

    result = runner.invoke(app, ["version"])

    assert result.exit_code == 0
    assert "Version 9.9.9" in result.output


def test_worker_command_exits_cleanly_in_local_mode(monkeypatch) -> None:
    monkeypatch.setattr("sibyl.config.settings.store", "surreal")
    monkeypatch.setattr("sibyl.config.settings.auth_store", "surreal")
    monkeypatch.setattr("sibyl.config.settings.coordination_backend", "auto")

    result = runner.invoke(app, ["worker"])

    assert result.exit_code == 0
    assert "runs background jobs in-process under" in result.output


def test_worker_command_keeps_arq_path_in_redis_mode(monkeypatch) -> None:
    run_worker = MagicMock()

    monkeypatch.setattr("sibyl.config.settings.store", "surreal")
    monkeypatch.setattr("sibyl.config.settings.coordination_backend", "redis")
    monkeypatch.setattr("arq.run_worker", run_worker)

    result = runner.invoke(app, ["worker", "--burst"])

    assert result.exit_code == 0
    run_worker.assert_called_once()


def test_serve_with_reload_enables_dev_diagnostics(monkeypatch) -> None:
    cli_main = sys.modules["sibyl.cli.main"]
    process = MagicMock()
    process.wait.return_value = 0
    popen = MagicMock(return_value=process)

    monkeypatch.setattr("subprocess.Popen", popen)

    cli_main._serve_with_reload("localhost", 3334)

    kwargs = popen.call_args.kwargs
    env = kwargs["env"]
    args = popen.call_args.args[0]
    assert env["PYTHONFAULTHANDLER"] == "1"
    assert env["SIBYL_DEV_DIAGNOSTICS"] == "1"
    assert "sibyl.main:create_dev_app" in args
    assert "--factory" in args
    assert "--timeout-graceful-shutdown" in args
    assert args[args.index("--reload-dir") + 1].endswith("apps/api/src")


def test_configure_embedded_environment(monkeypatch, tmp_path) -> None:
    _clear_embedded_runtime_env(monkeypatch)

    data_dir = cli_main._configure_embedded_environment(tmp_path / "surreal")

    assert data_dir == tmp_path / "surreal"
    assert os.environ["SIBYL_STORE"] == "surreal"
    assert os.environ["SIBYL_AUTH_STORE"] == "surreal"
    assert os.environ["SIBYL_COORDINATION_BACKEND"] == "local"
    assert os.environ["SIBYL_ALLOW_EMBEDDED_SINGLE_WRITER"] == "1"
    assert os.environ["SIBYL_SURREAL_URL"] == f"surrealkv://{data_dir}"


def test_configure_embedded_environment_refreshes_global_settings(monkeypatch, tmp_path) -> None:
    _clear_embedded_runtime_env(monkeypatch)
    monkeypatch.setattr("sibyl.config.settings.surreal_url", "")

    data_dir = cli_main._configure_embedded_environment(tmp_path / "surreal")

    assert os.environ["SIBYL_SURREAL_URL"] == f"surrealkv://{data_dir}"
    assert f"surrealkv://{data_dir}" == import_module("sibyl.config").settings.resolved_surreal_url


def test_setup_surreal_services_skips_redis_for_local_coordination(monkeypatch) -> None:
    checked: list[tuple[str, int]] = []

    def tcp_service_running(host: str, port: int) -> bool:
        checked.append((host, port))
        return True

    monkeypatch.setattr(cli_main, "_tcp_service_running", tcp_service_running)

    runtime_settings = SimpleNamespace(
        resolved_surreal_url="ws://127.0.0.1:8000/rpc",
        auth_store="surreal",
        resolved_coordination_backend="local",
        redis_host="127.0.0.1",
        redis_port=6381,
    )

    assert cli_main._check_surreal_services(runtime_settings) is True
    assert checked == [("127.0.0.1", 8000)]


def test_setup_surreal_services_ignores_removed_postgres_auth(monkeypatch) -> None:
    checked: list[tuple[str, int]] = []

    def tcp_service_running(host: str, port: int) -> bool:
        checked.append((host, port))
        return True

    monkeypatch.setattr(cli_main, "_tcp_service_running", tcp_service_running)

    runtime_settings = SimpleNamespace(
        resolved_surreal_url="ws://127.0.0.1:8000/rpc",
        auth_store="postgres",
        resolved_coordination_backend="local",
        redis_host="127.0.0.1",
        redis_port=6381,
    )

    assert cli_main._check_surreal_services(runtime_settings) is True
    assert checked == [("127.0.0.1", 8000)]


def test_setup_surreal_services_checks_redis_when_configured(monkeypatch) -> None:
    checked: list[tuple[str, int]] = []

    def tcp_service_running(host: str, port: int) -> bool:
        checked.append((host, port))
        return True

    monkeypatch.setattr(cli_main, "_tcp_service_running", tcp_service_running)

    runtime_settings = SimpleNamespace(
        resolved_surreal_url="ws://127.0.0.1:8000/rpc",
        auth_store="surreal",
        resolved_coordination_backend="redis",
        redis_host="127.0.0.1",
        redis_port=6381,
    )

    assert cli_main._check_surreal_services(runtime_settings) is True
    assert checked == [("127.0.0.1", 8000), ("127.0.0.1", 6381)]


def test_setup_runtime_services_checks_legacy_store_through_surreal_stack(monkeypatch) -> None:
    check_surreal = MagicMock(return_value=True)
    check_coordination = MagicMock(return_value=True)

    monkeypatch.setattr(cli_main, "_check_surreal_services", check_surreal)
    monkeypatch.setattr(cli_main, "_check_coordination_services", check_coordination)

    runtime_settings = SimpleNamespace(
        store="legacy",
        auth_store="surreal",
        coordination_backend="auto",
    )

    assert cli_main._check_runtime_services(runtime_settings) is True
    check_surreal.assert_called_once_with(runtime_settings)
    check_coordination.assert_not_called()


def test_setup_runtime_services_defaults_missing_store_to_surreal(monkeypatch) -> None:
    check_surreal = MagicMock(return_value=True)
    check_coordination = MagicMock(return_value=True)

    monkeypatch.setattr(cli_main, "_check_surreal_services", check_surreal)
    monkeypatch.setattr(cli_main, "_check_coordination_services", check_coordination)

    runtime_settings = SimpleNamespace(
        auth_store="surreal",
        coordination_backend="auto",
    )

    assert cli_main._check_runtime_services(runtime_settings) is True
    check_surreal.assert_called_once_with(runtime_settings)
    check_coordination.assert_not_called()


def test_setup_runtime_services_checks_surreal_stack_for_mixed_legacy_mode(
    monkeypatch,
) -> None:
    check_surreal = MagicMock(return_value=True)
    check_coordination = MagicMock(return_value=True)

    monkeypatch.setattr(cli_main, "_check_surreal_services", check_surreal)
    monkeypatch.setattr(cli_main, "_check_coordination_services", check_coordination)

    runtime_settings = SimpleNamespace(
        store="legacy",
        auth_store="surreal",
        coordination_backend="auto",
    )

    assert cli_main._check_runtime_services(runtime_settings) is True
    check_surreal.assert_called_once_with(runtime_settings)
    check_coordination.assert_not_called()


def test_setup_does_not_create_project_dotenv(monkeypatch, tmp_path) -> None:
    (tmp_path / ".env.example").write_text("SIBYL_JWT_SECRET=example\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli_main, "_check_openai_api_key_configured", lambda settings: True)
    monkeypatch.setattr(cli_main, "_check_docker_available", lambda: True)
    monkeypatch.setattr(cli_main, "_check_runtime_services", lambda settings: True)

    result = runner.invoke(app, ["setup"])

    assert result.exit_code == 0
    assert not (tmp_path / ".env").exists()
