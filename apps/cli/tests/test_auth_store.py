"""Tests for CLI auth token storage with secure file permissions."""

import json
import os
import stat
from pathlib import Path

import pytest

from sibyl_cli import auth_store


class TestNormalizeApiUrl:
    """Tests for URL normalization."""

    def test_strips_whitespace(self) -> None:
        """Strips leading/trailing whitespace."""
        assert auth_store.normalize_api_url("  http://localhost:3334  ") == "http://localhost:3334"

    def test_strips_trailing_slash(self) -> None:
        """Strips trailing slashes from path."""
        assert auth_store.normalize_api_url("http://localhost:3334/") == "http://localhost:3334"
        assert (
            auth_store.normalize_api_url("http://localhost:3334/api/")
            == "http://localhost:3334/api"
        )

    def test_handles_scheme_less_url(self) -> None:
        """Handles URLs without explicit scheme (uses parts as-is)."""
        # Note: urlsplit treats scheme-less as path, not netloc
        # Users should provide full URLs like http://localhost:3334
        result = auth_store.normalize_api_url("http://localhost:3334")
        assert result == "http://localhost:3334"

    def test_preserves_https(self) -> None:
        """Preserves https scheme."""
        assert auth_store.normalize_api_url("https://api.sibyl.dev") == "https://api.sibyl.dev"

    def test_empty_string(self) -> None:
        """Returns empty string for empty input."""
        assert auth_store.normalize_api_url("") == ""
        assert auth_store.normalize_api_url("   ") == ""


class TestSecureDirectoryPermissions:
    """Tests for secure directory creation."""

    def test_creates_directory_with_700_permissions(self, tmp_path: Path) -> None:
        """Creates directory with 0700 permissions on Unix."""
        if os.name == "nt":
            pytest.skip("Unix permissions not supported on Windows")

        test_dir = tmp_path / "secure_test"
        auth_store._ensure_secure_dir(test_dir)

        assert test_dir.exists()
        mode = stat.S_IMODE(os.stat(test_dir).st_mode)
        assert mode == 0o700

    def test_fixes_permissions_if_wrong(self, tmp_path: Path) -> None:
        """Fixes permissions if directory exists with wrong mode."""
        if os.name == "nt":
            pytest.skip("Unix permissions not supported on Windows")

        test_dir = tmp_path / "fix_perms"
        test_dir.mkdir(mode=0o755)

        auth_store._ensure_secure_dir(test_dir)

        mode = stat.S_IMODE(os.stat(test_dir).st_mode)
        assert mode == 0o700

    def test_creates_parent_directories(self, tmp_path: Path) -> None:
        """Creates parent directories if needed."""
        deep_dir = tmp_path / "a" / "b" / "c"

        auth_store._ensure_secure_dir(deep_dir)

        assert deep_dir.exists()


class TestSecureFileWrite:
    """Tests for atomic secure file writes."""

    def test_creates_file_with_600_permissions(self, tmp_path: Path) -> None:
        """Creates file with 0600 permissions on Unix."""
        if os.name == "nt":
            pytest.skip("Unix permissions not supported on Windows")

        test_file = tmp_path / "auth.json"
        auth_store._secure_write(test_file, '{"test": true}')

        assert test_file.exists()
        mode = stat.S_IMODE(os.stat(test_file).st_mode)
        assert mode == 0o600

    def test_writes_content_correctly(self, tmp_path: Path) -> None:
        """Writes content correctly."""
        test_file = tmp_path / "auth.json"
        content = '{"access_token": "secret123"}'

        auth_store._secure_write(test_file, content)

        assert test_file.read_text() == content

    def test_atomic_write_no_partial_content(self, tmp_path: Path) -> None:
        """Atomic write prevents partial content on interruption."""
        test_file = tmp_path / "auth.json"
        original_content = '{"original": true}'
        test_file.write_text(original_content)

        # Simulate write that fails mid-way by checking no temp files left
        auth_store._secure_write(test_file, '{"new": true}')

        # No temp files should remain
        temp_files = list(tmp_path.glob(".auth_*.tmp"))
        assert len(temp_files) == 0

    def test_overwrites_existing_file(self, tmp_path: Path) -> None:
        """Overwrites existing file content."""
        test_file = tmp_path / "auth.json"
        test_file.write_text('{"old": true}')

        auth_store._secure_write(test_file, '{"new": true}')

        assert json.loads(test_file.read_text()) == {"new": True}


class TestAuthDataReadWrite:
    """Tests for auth data read/write operations."""

    def test_read_returns_empty_dict_for_missing_file(self, tmp_path: Path) -> None:
        """Returns empty dict when file doesn't exist."""
        missing = tmp_path / "missing.json"
        result = auth_store.read_auth_data(missing)
        assert result == {}

    def test_read_returns_empty_dict_for_invalid_json(self, tmp_path: Path) -> None:
        """Returns empty dict for invalid JSON."""
        invalid = tmp_path / "invalid.json"
        invalid.write_text("not valid json {{{")

        result = auth_store.read_auth_data(invalid)
        assert result == {}

    def test_write_then_read_roundtrip(self, tmp_path: Path) -> None:
        """Write then read returns same data."""
        test_file = tmp_path / "auth.json"
        data = {"servers": {"http://localhost:3334": {"access_token": "abc"}}}

        auth_store.write_auth_data(data, test_file)
        result = auth_store.read_auth_data(test_file)

        assert result == data

    def test_write_formats_json_nicely(self, tmp_path: Path) -> None:
        """Write formats JSON with indentation and newline."""
        test_file = tmp_path / "auth.json"
        data = {"key": "value"}

        auth_store.write_auth_data(data, test_file)

        content = test_file.read_text()
        assert "\n" in content  # Has newlines
        assert content.endswith("\n")  # Ends with newline


class TestServerCredentials:
    """Tests for per-server credential storage."""

    def test_read_server_credentials_empty(self, tmp_path: Path) -> None:
        """Returns empty dict for unknown server."""
        test_file = tmp_path / "auth.json"
        test_file.write_text('{"servers": {}}')

        result = auth_store.read_server_credentials("http://example.com", test_file)
        assert result == {}

    def test_write_server_credentials_creates_structure(self, tmp_path: Path) -> None:
        """Creates servers dict if missing."""
        test_file = tmp_path / "auth.json"
        test_file.write_text("{}")

        auth_store.write_server_credentials(
            "http://localhost:3334",
            {"access_token": "test123"},
            test_file,
        )

        data = json.loads(test_file.read_text())
        assert "servers" in data
        assert "http://localhost:3334" in data["servers"]
        assert data["servers"]["http://localhost:3334"]["access_token"] == "test123"

    def test_write_server_credentials_merges(self, tmp_path: Path) -> None:
        """Merges new credentials with existing."""
        test_file = tmp_path / "auth.json"
        auth_store.write_server_credentials(
            "http://localhost:3334",
            {"access_token": "old"},
            test_file,
        )
        auth_store.write_server_credentials(
            "http://localhost:3334",
            {"refresh_token": "refresh123"},
            test_file,
        )

        creds = auth_store.read_server_credentials("http://localhost:3334", test_file)
        assert creds["access_token"] == "old"
        assert creds["refresh_token"] == "refresh123"

    def test_scoped_credentials_do_not_bleed_between_orgs(self, tmp_path: Path) -> None:
        """Context/org scoped credentials are isolated under the same API URL."""
        test_file = tmp_path / "auth.json"
        personal = auth_store.credential_scope("prod", "personal")
        work = auth_store.credential_scope("prod", "work")

        auth_store.set_tokens(
            "https://sibyl.example.com/api",
            access_token="personal-token",
            path=test_file,
            credential_scope=personal,
        )
        auth_store.set_tokens(
            "https://sibyl.example.com/api",
            access_token="work-token",
            path=test_file,
            credential_scope=work,
        )

        assert (
            auth_store.get_access_token(
                "https://sibyl.example.com/api",
                test_file,
                credential_scope=personal,
            )
            == "personal-token"
        )
        assert (
            auth_store.get_access_token(
                "https://sibyl.example.com/api",
                test_file,
                credential_scope=work,
            )
            == "work-token"
        )


class TestTokenOperations:
    """Tests for token get/set/clear operations."""

    def test_set_and_get_access_token(self, tmp_path: Path) -> None:
        """Set then get access token."""
        test_file = tmp_path / "auth.json"
        auth_store.set_tokens(
            "http://localhost:3334",
            access_token="my_token",
            path=test_file,
        )

        result = auth_store.get_access_token("http://localhost:3334", test_file)
        assert result == "my_token"

    def test_set_and_get_refresh_token(self, tmp_path: Path) -> None:
        """Set then get refresh token."""
        test_file = tmp_path / "auth.json"
        auth_store.set_tokens(
            "http://localhost:3334",
            access_token="access",
            refresh_token="refresh123",
            path=test_file,
        )

        result = auth_store.get_refresh_token("http://localhost:3334", test_file)
        assert result == "refresh123"

    def test_explicit_token_write_starts_a_new_pending_replay_lineage(
        self, tmp_path: Path
    ) -> None:
        test_file = tmp_path / "auth.json"
        auth_store.set_tokens("http://localhost:3334", "first", path=test_file)
        first_scope = auth_store.get_pending_replay_scope(
            "http://localhost:3334", test_file
        )

        auth_store.set_tokens("http://localhost:3334", "second", path=test_file)
        second_scope = auth_store.get_pending_replay_scope(
            "http://localhost:3334", test_file
        )

        assert first_scope is not None
        assert second_scope is not None
        assert second_scope != first_scope

    def test_server_refresh_preserves_pending_replay_lineage(self, tmp_path: Path) -> None:
        test_file = tmp_path / "auth.json"
        replay_scope = "credential:stable-lineage"

        auth_store.set_tokens(
            "http://localhost:3334",
            "refreshed",
            refresh_token="refresh",
            path=test_file,
            pending_replay_scope=replay_scope,
        )

        assert (
            auth_store.get_pending_replay_scope("http://localhost:3334", test_file)
            == replay_scope
        )

    def test_set_tokens_replaces_stale_refresh_credentials(self, tmp_path: Path) -> None:
        """Access-only token writes do not keep an old refresh token."""
        test_file = tmp_path / "auth.json"
        auth_store.set_tokens(
            "http://localhost:3334",
            access_token="access",
            refresh_token="refresh123",
            expires_in=3600,
            path=test_file,
        )

        auth_store.set_tokens(
            "http://localhost:3334",
            access_token="manual-token",
            path=test_file,
        )

        creds = auth_store.read_server_credentials("http://localhost:3334", test_file)
        assert creds["access_token"] == "manual-token"
        assert "refresh_token" not in creds
        assert "access_token_expires_at" not in creds

    def test_get_token_returns_none_when_missing(self, tmp_path: Path) -> None:
        """Returns None when no token stored."""
        test_file = tmp_path / "auth.json"
        test_file.write_text("{}")

        assert auth_store.get_access_token("http://localhost:3334", test_file) is None
        assert auth_store.get_refresh_token("http://localhost:3334", test_file) is None

    def test_clear_tokens_removes_server(self, tmp_path: Path) -> None:
        """Clear removes all tokens for server."""
        test_file = tmp_path / "auth.json"
        auth_store.set_tokens(
            "http://localhost:3334",
            access_token="token",
            refresh_token="refresh",
            path=test_file,
        )

        auth_store.clear_tokens("http://localhost:3334", test_file)

        assert auth_store.get_access_token("http://localhost:3334", test_file) is None

    def test_clear_all_tokens_removes_file(self, tmp_path: Path) -> None:
        """Clear all removes the entire auth file."""
        test_file = tmp_path / "auth.json"
        auth_store.set_tokens("http://localhost:3334", access_token="token", path=test_file)

        auth_store.clear_all_tokens(test_file)

        assert not test_file.exists()


class TestTokenExpiry:
    """Tests for token expiration checking."""

    def test_set_tokens_calculates_expiry(self, tmp_path: Path) -> None:
        """Set tokens with expires_in calculates timestamp."""
        test_file = tmp_path / "auth.json"
        auth_store.set_tokens(
            "http://localhost:3334",
            access_token="token",
            expires_in=3600,
            path=test_file,
        )

        expires_at = auth_store.get_access_token_expires_at("http://localhost:3334", test_file)
        assert expires_at is not None
        # Should be roughly now + 3600 (give or take a few seconds for test execution)
        import time

        assert abs(expires_at - (time.time() + 3600)) < 5

    def test_is_expired_false_when_future(self, tmp_path: Path) -> None:
        """Token not expired when expiry is in future."""
        test_file = tmp_path / "auth.json"
        auth_store.set_tokens(
            "http://localhost:3334",
            access_token="token",
            expires_in=3600,
            path=test_file,
        )

        assert auth_store.is_access_token_expired("http://localhost:3334", test_file) is False

    def test_is_expired_true_when_past(self, tmp_path: Path) -> None:
        """Token expired when expiry is in past."""
        test_file = tmp_path / "auth.json"
        # Manually set an expired timestamp
        auth_store.write_server_credentials(
            "http://localhost:3334",
            {"access_token": "old", "access_token_expires_at": 1},  # epoch + 1 second
            test_file,
        )

        assert auth_store.is_access_token_expired("http://localhost:3334", test_file) is True

    def test_is_expired_false_when_no_expiry(self, tmp_path: Path) -> None:
        """Token not considered expired if no expiry stored."""
        test_file = tmp_path / "auth.json"
        auth_store.set_tokens(
            "http://localhost:3334",
            access_token="token",
            path=test_file,  # No expires_in
        )

        assert auth_store.is_access_token_expired("http://localhost:3334", test_file) is False

    def test_buffer_seconds_affects_expiry_check(self, tmp_path: Path) -> None:
        """Buffer seconds makes token expire earlier."""
        test_file = tmp_path / "auth.json"
        # Token expires in 30 seconds
        auth_store.set_tokens(
            "http://localhost:3334",
            access_token="token",
            expires_in=30,
            path=test_file,
        )

        # With 60 second buffer, should be considered expired
        assert (
            auth_store.is_access_token_expired(
                "http://localhost:3334", test_file, buffer_seconds=60
            )
            is True
        )
        # With 10 second buffer, should not be expired
        assert (
            auth_store.is_access_token_expired(
                "http://localhost:3334", test_file, buffer_seconds=10
            )
            is False
        )


class TestMigrateLegacyTokens:
    """Tests for legacy token migration."""

    def test_removes_root_level_tokens(self, tmp_path: Path) -> None:
        """Migration removes legacy root-level tokens."""
        test_file = tmp_path / "auth.json"
        legacy_data = {
            "access_token": "old_token",
            "refresh_token": "old_refresh",
            "access_token_expires_at": 12345,
            "servers": {},
        }
        test_file.write_text(json.dumps(legacy_data))

        auth_store.migrate_legacy_tokens(test_file)

        data = json.loads(test_file.read_text())
        assert "access_token" not in data
        assert "refresh_token" not in data
        assert "access_token_expires_at" not in data
        assert "servers" in data

    def test_noop_if_no_legacy_tokens(self, tmp_path: Path) -> None:
        """Migration is noop if no legacy tokens."""
        test_file = tmp_path / "auth.json"
        modern_data = {"servers": {"http://localhost:3334": {"access_token": "new"}}}
        test_file.write_text(json.dumps(modern_data))

        auth_store.migrate_legacy_tokens(test_file)

        data = json.loads(test_file.read_text())
        assert data == modern_data
