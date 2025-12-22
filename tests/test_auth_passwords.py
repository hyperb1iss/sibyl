import pytest

from sibyl.auth.passwords import hash_password, verify_password
from sibyl.config import Settings


def test_password_hash_roundtrip(monkeypatch) -> None:
    monkeypatch.setenv("SIBYL_PASSWORD_PEPPER", "")
    monkeypatch.setenv("SIBYL_PASSWORD_ITERATIONS", "100000")
    from sibyl import config as config_module

    config_module.settings = Settings(_env_file=None)  # type: ignore[assignment]

    pw = hash_password("supersecret")
    assert verify_password(
        "supersecret", salt_hex=pw.salt_hex, hash_hex=pw.hash_hex, iterations=pw.iterations
    )
    assert not verify_password(
        "nope", salt_hex=pw.salt_hex, hash_hex=pw.hash_hex, iterations=pw.iterations
    )


def test_password_pepper_changes_hash(monkeypatch) -> None:
    monkeypatch.setenv("SIBYL_PASSWORD_ITERATIONS", "100000")
    from sibyl import config as config_module

    monkeypatch.setenv("SIBYL_PASSWORD_PEPPER", "pepper1")
    config_module.settings = Settings(_env_file=None)  # type: ignore[assignment]
    pw1 = hash_password("pw", salt=b"\x00" * 16, iterations=100000)

    monkeypatch.setenv("SIBYL_PASSWORD_PEPPER", "pepper2")
    config_module.settings = Settings(_env_file=None)  # type: ignore[assignment]
    pw2 = hash_password("pw", salt=b"\x00" * 16, iterations=100000)

    assert pw1.hash_hex != pw2.hash_hex


def test_password_verify_rejects_bad_hex() -> None:
    assert verify_password("pw", salt_hex="zz", hash_hex="yy", iterations=100000) is False


def test_password_empty_rejected() -> None:
    with pytest.raises(ValueError):
        hash_password("")
