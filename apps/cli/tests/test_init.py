from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from sibyl_cli import config_store
from sibyl_cli.main import app


def _use_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config_store.Path, "home", lambda: tmp_path)


def test_init_creates_active_local_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_home(tmp_path, monkeypatch)

    result = CliRunner().invoke(app, ["init", "--json"])

    assert result.exit_code == 0
    assert '"context": "local"' in result.stdout
    ctx = config_store.get_active_context()
    assert ctx is not None
    assert ctx.name == "local"
    assert ctx.server_url == "http://localhost:3334"


def test_init_creates_remote_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_home(tmp_path, monkeypatch)

    result = CliRunner().invoke(
        app,
        [
            "init",
            "--remote",
            "https://sibyl.example.com",
            "--name",
            "prod",
            "--org",
            "hyper",
            "--project",
            "project_123",
            "--json",
        ],
    )

    assert result.exit_code == 0
    ctx = config_store.get_active_context()
    assert ctx is not None
    assert ctx.name == "prod"
    assert ctx.server_url == "https://sibyl.example.com"
    assert ctx.org_slug == "hyper"
    assert ctx.default_project == "project_123"


def test_init_requires_force_for_existing_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_home(tmp_path, monkeypatch)
    config_store.create_context("local", "http://localhost:3334", set_active=True)

    result = CliRunner().invoke(app, ["init"])

    assert result.exit_code == 1
    assert "already exists" in result.stdout


def test_init_force_updates_existing_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_home(tmp_path, monkeypatch)
    config_store.create_context("local", "http://localhost:3334", set_active=True)

    result = CliRunner().invoke(app, ["init", "--force", "--remote", "https://remote.test"])

    assert result.exit_code == 0
    ctx = config_store.get_active_context()
    assert ctx is not None
    assert ctx.server_url == "https://remote.test"
