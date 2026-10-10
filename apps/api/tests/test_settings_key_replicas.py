"""The settings encryption key across API replicas and workers.

Every process that serves the API or runs jobs reads the same encrypted
settings rows, so under Redis coordination they must all hold one key. A
"replica" here is this process with its own home directory (so its own key
file) and a cold key cache, which is all a separate container is to key
resolution.
"""

from __future__ import annotations

import base64
import secrets
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from sibyl import crypto, runtime_services as runtime_services_module
from sibyl.config import settings
from sibyl.jobs import worker as worker_module
from sibyl.runtime_services import RuntimeServices


@pytest.fixture(autouse=True)
def _no_ambient_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    monkeypatch.delenv("SIBYL_SETTINGS_KEY", raising=False)
    _become_replica(monkeypatch, tmp_path / "default-home")
    yield
    crypto._get_fernet.cache_clear()


def _become_replica(monkeypatch: pytest.MonkeyPatch, home: Path) -> Path:
    key_file = home / ".sibyl" / "settings.key"
    monkeypatch.setattr(crypto, "_SETTINGS_KEY_FILE", key_file)
    crypto._get_fernet.cache_clear()
    return key_file


def test_one_process_still_generates_and_keeps_its_own_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(settings, "coordination_backend", "local")
    key_file = _become_replica(monkeypatch, tmp_path / "solo")

    crypto.require_shared_settings_key()
    assert not key_file.exists()

    ciphertext = crypto.encrypt_value("sk-solo")
    assert key_file.exists()

    _become_replica(monkeypatch, tmp_path / "solo")
    assert crypto.decrypt_value(ciphertext) == "sk-solo"


def test_replica_without_a_shared_key_refuses_to_mint_its_own(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(settings, "coordination_backend", "redis")
    key_file = _become_replica(monkeypatch, tmp_path / "replica-a")

    with pytest.raises(RuntimeError, match="SIBYL_SETTINGS_KEY"):
        crypto.encrypt_value("sk-saved-on-a")

    assert not key_file.exists()


def test_replicas_sharing_the_key_read_each_others_secrets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(settings, "coordination_backend", "redis")
    monkeypatch.setenv("SIBYL_SETTINGS_KEY", secrets.token_hex(32))

    first = _become_replica(monkeypatch, tmp_path / "replica-a")
    crypto.require_shared_settings_key()
    ciphertext = crypto.encrypt_value("sk-saved-on-a")

    second = _become_replica(monkeypatch, tmp_path / "replica-b")
    crypto.require_shared_settings_key()
    assert crypto.decrypt_value(ciphertext) == "sk-saved-on-a"
    assert not first.exists()
    assert not second.exists()


def test_a_key_file_every_replica_mounts_counts_as_shared(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(settings, "coordination_backend", "redis")
    mounted = tmp_path / "mounted"
    key_file = _become_replica(monkeypatch, mounted)
    key_file.parent.mkdir(parents=True)
    key_file.write_text(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())

    crypto.require_shared_settings_key()
    ciphertext = crypto.encrypt_value("sk-saved-on-a")

    _become_replica(monkeypatch, mounted)
    crypto.require_shared_settings_key()
    assert crypto.decrypt_value(ciphertext) == "sk-saved-on-a"


async def test_api_startup_refuses_before_touching_the_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(settings, "coordination_backend", "redis")
    _become_replica(monkeypatch, tmp_path / "api")
    bootstrap = AsyncMock()
    monkeypatch.setattr(runtime_services_module, "bootstrap_surreal_runtime_schemas", bootstrap)

    with pytest.raises(crypto.SettingsKeyError, match="SIBYL_SETTINGS_KEY"):
        await RuntimeServices(log=MagicMock()).startup()

    bootstrap.assert_not_awaited()


async def test_worker_startup_refuses_before_loading_stored_keys(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(settings, "coordination_backend", "redis")
    _become_replica(monkeypatch, tmp_path / "worker")
    load_api_keys = AsyncMock()
    monkeypatch.setattr("sibyl.services.settings.load_api_keys_from_db", load_api_keys)

    with pytest.raises(crypto.SettingsKeyError, match="SIBYL_SETTINGS_KEY"):
        await worker_module.startup({})

    load_api_keys.assert_not_awaited()
