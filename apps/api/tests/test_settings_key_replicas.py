"""The settings encryption key across API replicas and workers.

Every process that serves the API or runs jobs reads the same encrypted
settings rows, so under Redis coordination they must all hold one key. A
"replica" here is this process with its own home directory (so its own key
file) and a cold key cache, which is all a separate container is to key
resolution. The fingerprint cases run on the embedded engine and, with
SIBYL_LIVE_SURREAL_TESTS=1 and a server SIBYL_SURREAL_URL, against a server.
"""

from __future__ import annotations

import asyncio
import base64
import os
import secrets
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, suppress
from contextvars import ContextVar
from itertools import count
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from sibyl import crypto, runtime_services as runtime_services_module
from sibyl.config import settings
from sibyl.jobs import worker as worker_module
from sibyl.persistence.surreal import system_settings as surreal_system_settings
from sibyl.runtime_services import RuntimeServices
from sibyl.services import settings_key as settings_key_module
from sibyl.services.settings_key import SETTINGS_KEY_FINGERPRINT, verify_shared_settings_key
from sibyl_core.backends.surreal import SurrealContentClient, bootstrap_content_schema
from sibyl_core.backends.surreal.records import normalize_records
from sibyl_core.backends.surreal.url_schemes import is_embedded_surreal_url


@pytest.fixture(autouse=True)
def _no_ambient_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    monkeypatch.delenv("SIBYL_SETTINGS_KEY", raising=False)
    _become_replica(monkeypatch, tmp_path / "default-home")
    yield
    crypto.clear_settings_key_cache()


def _become_replica(monkeypatch: pytest.MonkeyPatch, home: Path) -> Path:
    key_file = home / ".sibyl" / "settings.key"
    monkeypatch.setattr(crypto, "_SETTINGS_KEY_FILE", key_file)
    crypto.clear_settings_key_cache()
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


# ---------------------------------------------------------------------------
# Every process on one store must hold the same key, not merely some key.
# ---------------------------------------------------------------------------

CREDENTIALS = {
    "username": os.environ.get("SIBYL_SURREAL_USERNAME", "root"),
    "password": os.environ.get("SIBYL_SURREAL_PASSWORD", "root"),
}
REPLICAS = 8
RACERS = 32


def _store_url(engine: str) -> str:
    if engine == "embedded":
        return "memory://"
    if os.environ.get("SIBYL_LIVE_SURREAL_TESTS") != "1":
        pytest.skip("live SurrealDB tests are disabled")
    url = os.environ.get("SIBYL_SURREAL_URL", "")
    if not url or is_embedded_surreal_url(url):
        pytest.skip("live SurrealDB tests require SIBYL_SURREAL_URL to point at a server")
    return url


@pytest.fixture(params=["embedded", "live"])
async def settings_store(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[SurrealContentClient]:
    """The shared system_settings table the replicas read and write."""
    url = _store_url(request.param)
    namespace = f"verify_settings_key_{uuid4().hex}"
    # A memory:// store lives inside its client, so embedded replicas share
    # one; against a server every replica has its own client and pool.
    clients = [
        SurrealContentClient(url=url, namespace=namespace, **CREDENTIALS)
        for _ in range(1 if request.param == "embedded" else REPLICAS)
    ]
    await bootstrap_content_schema(clients[0])
    # Open every pooled connection now, so the replicas' first queries race
    # each other instead of queueing behind connection setup.
    await asyncio.gather(
        *(client.execute_query("RETURN 1;") for client in clients for _ in range(RACERS))
    )
    turn = count()

    @asynccontextmanager
    async def scope() -> AsyncIterator[SurrealContentClient]:
        yield clients[next(turn) % len(clients)]

    monkeypatch.setattr(surreal_system_settings, "surreal_content_client", scope)
    try:
        yield clients[0]
    finally:
        if request.param == "live":
            with suppress(Exception):
                await clients[0].execute_query(f"REMOVE NAMESPACE IF EXISTS {namespace};")
        for client in clients:
            await client.close()


def _own_key_file(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    """Become a replica whose home holds a key file of its own."""
    key_file = _become_replica(monkeypatch, home)
    key_file.parent.mkdir(parents=True)
    key_file.write_text(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())


async def _stored_fingerprints(store: SurrealContentClient) -> list[str]:
    rows = normalize_records(
        await store.execute_query(
            "SELECT * FROM system_settings WHERE key = $key;", key=SETTINGS_KEY_FINGERPRINT
        )
    )
    return [str(row["value"]) for row in rows]


async def test_replicas_with_their_own_key_files_cannot_both_start(
    settings_store: SurrealContentClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(settings, "coordination_backend", "redis")

    _own_key_file(monkeypatch, tmp_path / "replica-a")
    await verify_shared_settings_key()
    first = crypto.settings_key_fingerprint()

    _own_key_file(monkeypatch, tmp_path / "replica-b")
    with pytest.raises(crypto.SettingsKeyError, match="SIBYL_SETTINGS_KEY"):
        await verify_shared_settings_key()

    assert await _stored_fingerprints(settings_store) == [first]


async def test_replicas_sharing_the_key_all_start(
    settings_store: SurrealContentClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(settings, "coordination_backend", "redis")
    monkeypatch.setenv("SIBYL_SETTINGS_KEY", secrets.token_hex(32))

    for replica in ("replica-a", "replica-b", "worker"):
        _become_replica(monkeypatch, tmp_path / replica)
        await verify_shared_settings_key()

    assert await _stored_fingerprints(settings_store) == [crypto.settings_key_fingerprint()]


async def _race_first_starts(monkeypatch: pytest.MonkeyPatch, fingerprints: list[str]) -> list[str]:
    """Start one replica per fingerprint at once; return each one's outcome."""
    monkeypatch.setattr(settings, "coordination_backend", "redis")
    replica_fingerprint: ContextVar[str] = ContextVar("replica_fingerprint")
    monkeypatch.setattr(settings_key_module, "settings_key_fingerprint", replica_fingerprint.get)

    async def start(fingerprint: str) -> str:
        replica_fingerprint.set(fingerprint)
        try:
            await verify_shared_settings_key()
        except crypto.SettingsKeyError:
            return "refused"
        except Exception as exc:  # anything else is a replica that crashed
            return f"crashed: {type(exc).__name__}: {exc}"
        return fingerprint

    return list(await asyncio.gather(*(start(fingerprint) for fingerprint in fingerprints)))


async def test_first_starts_racing_with_different_keys_admit_one_key(
    settings_store: SurrealContentClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcomes = await _race_first_starts(monkeypatch, [secrets.token_hex(32) for _ in range(RACERS)])

    started = [outcome for outcome in outcomes if outcome != "refused"]
    assert len(started) == 1, outcomes
    assert await _stored_fingerprints(settings_store) == started


async def test_replicas_sharing_a_key_all_start_when_they_boot_at_once(
    settings_store: SurrealContentClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh deployment's replicas start together; the race's losers read the winner."""
    shared = secrets.token_hex(32)

    outcomes = await _race_first_starts(monkeypatch, [shared] * RACERS)

    assert outcomes == [shared] * RACERS
    assert await _stored_fingerprints(settings_store) == [shared]


async def test_one_process_records_and_checks_nothing(
    settings_store: SurrealContentClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(settings, "coordination_backend", "local")
    _own_key_file(monkeypatch, tmp_path / "solo")

    await verify_shared_settings_key()

    assert await _stored_fingerprints(settings_store) == []


async def test_the_fingerprint_is_not_an_admin_setting(
    settings_store: SurrealContentClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(settings, "coordination_backend", "redis")
    _own_key_file(monkeypatch, tmp_path / "replica-a")
    await verify_shared_settings_key()

    listed = await surreal_system_settings.list_system_settings(None)

    assert SETTINGS_KEY_FINGERPRINT not in {setting.key for setting in listed}


async def test_api_startup_refuses_a_key_the_others_do_not_share(
    settings_store: SurrealContentClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(settings, "coordination_backend", "redis")
    _own_key_file(monkeypatch, tmp_path / "replica-a")
    await verify_shared_settings_key()

    _own_key_file(monkeypatch, tmp_path / "replica-b")
    monkeypatch.setattr(
        runtime_services_module, "bootstrap_surreal_runtime_schemas", AsyncMock(return_value=True)
    )
    load_settings = AsyncMock()
    monkeypatch.setattr(runtime_services_module, "load_runtime_settings_from_db", load_settings)

    with pytest.raises(crypto.SettingsKeyError, match="SIBYL_SETTINGS_KEY"):
        await RuntimeServices(log=MagicMock()).startup()

    load_settings.assert_not_awaited()


async def test_worker_startup_refuses_a_key_the_others_do_not_share(
    settings_store: SurrealContentClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(settings, "coordination_backend", "redis")
    _own_key_file(monkeypatch, tmp_path / "replica-a")
    await verify_shared_settings_key()

    _own_key_file(monkeypatch, tmp_path / "worker")
    load_api_keys = AsyncMock()
    monkeypatch.setattr("sibyl.services.settings.load_api_keys_from_db", load_api_keys)

    with pytest.raises(crypto.SettingsKeyError, match="SIBYL_SETTINGS_KEY"):
        await worker_module.startup({})

    load_api_keys.assert_not_awaited()
