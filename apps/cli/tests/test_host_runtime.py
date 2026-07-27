from __future__ import annotations

import plistlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from sibyl_cli import config_store
from sibyl_cli import host as host_module
from sibyl_cli.main import app


def _use_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config_store.Path, "home", lambda: tmp_path)


def test_serve_requires_local_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _use_home(tmp_path, monkeypatch)
    config_store.create_context("prod", "https://sibyl.example.com", set_active=True)

    result = CliRunner().invoke(app, ["serve"])

    assert result.exit_code == 1
    assert "points to https://sibyl.example.com" in result.stdout


def test_serve_background_starts_embedded_daemon(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_home(tmp_path, monkeypatch)
    config_store.create_context("local", "http://localhost:3334", set_active=True)
    monkeypatch.setattr(host_module, "SIBYL_RUN_DIR", tmp_path / "run")
    monkeypatch.setattr(host_module, "SIBYLD_PID_FILE", tmp_path / "run" / "sibyld.pid")
    monkeypatch.setattr(host_module, "SIBYLD_LOG_FILE", tmp_path / "run" / "sibyld.log")
    monkeypatch.setattr(host_module, "pid_alive", lambda _pid: False)
    calls: list[list[str]] = []

    def fake_popen(cmd: list[str], **_kwargs: object) -> SimpleNamespace:
        calls.append(cmd)
        return SimpleNamespace(pid=4242)

    monkeypatch.setattr(host_module.subprocess, "Popen", fake_popen)

    result = CliRunner().invoke(app, ["serve", "--background"])

    assert result.exit_code == 0
    assert calls == [
        [
            "sibyld",
            "serve",
            "--embedded",
            "--host",
            "127.0.0.1",
            "--port",
            "3334",
            "--transport",
            "streamable-http",
        ]
    ]
    assert (tmp_path / "run" / "sibyld.pid").read_text() == "4242\n"


def test_start_alias_runs_background_daemon(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_home(tmp_path, monkeypatch)
    config_store.create_context("local", "http://localhost:3334", set_active=True)
    monkeypatch.setattr(host_module, "SIBYL_RUN_DIR", tmp_path / "run")
    monkeypatch.setattr(host_module, "SIBYLD_PID_FILE", tmp_path / "run" / "sibyld.pid")
    monkeypatch.setattr(host_module, "SIBYLD_LOG_FILE", tmp_path / "run" / "sibyld.log")
    monkeypatch.setattr(host_module, "pid_alive", lambda _pid: False)

    def fake_popen(_cmd: list[str], **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(pid=4242)

    monkeypatch.setattr(host_module.subprocess, "Popen", fake_popen)

    result = CliRunner().invoke(app, ["start"])

    assert result.exit_code == 0
    assert (tmp_path / "run" / "sibyld.pid").read_text() == "4242\n"


def test_service_install_writes_launchd_plist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_home(tmp_path, monkeypatch)
    config_store.create_context("local", "http://localhost:3334", set_active=True)
    monkeypatch.setattr(host_module, "SIBYL_RUN_DIR", tmp_path / "run")
    monkeypatch.setattr(host_module.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(host_module.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(
        host_module, "resolve_sibyld_executable", lambda: "/opt/homebrew/bin/sibyld"
    )

    result = CliRunner().invoke(app, ["service", "install"])

    assert result.exit_code == 0
    plist_path = tmp_path / "Library" / "LaunchAgents" / "tech.hyperbliss.sibyl.plist"
    payload = plistlib.loads(plist_path.read_bytes())
    assert payload["Label"] == "tech.hyperbliss.sibyl"
    assert payload["ProgramArguments"][:2] == ["/opt/homebrew/bin/sibyld", "serve"]
    assert "--embedded" in payload["ProgramArguments"]
    assert "launchctl bootstrap" in result.stdout
    assert "not started automatically" in result.stdout


def test_service_install_writes_systemd_unit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_home(tmp_path, monkeypatch)
    config_store.create_context("local", "http://localhost:3334", set_active=True)
    monkeypatch.setattr(host_module, "SIBYL_RUN_DIR", tmp_path / "run")
    monkeypatch.setattr(host_module.platform, "system", lambda: "Linux")
    monkeypatch.setattr(host_module.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(host_module, "resolve_sibyld_executable", lambda: "/usr/bin/sibyld")

    result = CliRunner().invoke(app, ["service", "install"])

    assert result.exit_code == 0
    unit_path = tmp_path / ".config" / "systemd" / "user" / "sibyl.service"
    unit = unit_path.read_text()
    assert "ExecStart=/usr/bin/sibyld serve --embedded" in unit
    assert "Restart=on-failure" in unit
    assert "systemctl --user enable --now sibyl.service" in result.stdout


def test_stop_removes_pid_file_after_signal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pid_path = tmp_path / "sibyld.pid"
    pid_path.write_text("4242\n")
    monkeypatch.setattr(host_module, "SIBYLD_PID_FILE", pid_path)
    states = iter([True, False])
    monkeypatch.setattr(host_module, "pid_alive", lambda _pid: next(states))
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(host_module.os, "kill", lambda pid, sig: killed.append((pid, sig)))

    host_module.stop(timeout=0.1)

    assert killed == [(4242, host_module.signal.SIGTERM)]
    assert not pid_path.exists()
