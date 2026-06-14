"""Comprehensive tests for sibyl-core auth module (JWT and passwords).

These tests install mock settings through the auth modules' explicit provider
hook so sibyl-core stays independent from the sibyl server package.
"""

from __future__ import annotations

import importlib
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch
from uuid import uuid4

import jwt
import pytest
from pydantic import SecretStr

if TYPE_CHECKING:
    from collections.abc import Generator


# ---------------------------------------------------------------------------
# Mock Settings and Config Module
# ---------------------------------------------------------------------------


def _default_jwt_secret() -> SecretStr:
    return SecretStr("test-jwt-secret-key-for-testing-32-bytes")


def _default_password_pepper() -> SecretStr:
    return SecretStr("test-pepper")


@dataclass
class MockSettings:
    """Mock settings for auth tests."""

    jwt_secret: SecretStr = field(default_factory=_default_jwt_secret)
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60
    refresh_token_expire_days: int = 30
    password_pepper: SecretStr = field(default_factory=_default_password_pepper)
    password_iterations: int = 100_000


def create_mock_config() -> MagicMock:
    """Create a mock sibyl.config module."""
    mock_config = MagicMock()
    mock_config.settings = MockSettings()
    return mock_config


@pytest.fixture(autouse=True)
def mock_sibyl_modules() -> Generator[MagicMock]:
    """Auto-use fixture that mocks all sibyl.* modules before auth modules are imported.

    The auth modules in sibyl-core have imports from sibyl (server package):
    - sibyl.config (for settings)
    - sibyl.db.models (for AuthContext)

    Since we're testing sibyl-core in isolation, we need to mock these.
    """
    mock_config = create_mock_config()

    # Create mock module hierarchy
    mock_sibyl = MagicMock()
    mock_sibyl.config = mock_config
    mock_sibyl.db = MagicMock()
    mock_sibyl.db.models = MagicMock()

    # Save original modules
    saved_modules: dict[str, object] = {}
    modules_to_mock = [
        "sibyl",
        "sibyl.config",
        "sibyl.db",
        "sibyl.db.models",
    ]

    for mod_name in modules_to_mock:
        saved_modules[mod_name] = sys.modules.get(mod_name)

    # Clear any existing sibyl_core.auth modules (force re-import)
    auth_modules = [k for k in list(sys.modules.keys()) if k.startswith("sibyl_core.auth")]
    for mod_name in auth_modules:
        sys.modules.pop(mod_name, None)

    # Install mocks
    sys.modules["sibyl"] = mock_sibyl
    sys.modules["sibyl.config"] = mock_config
    sys.modules["sibyl.db"] = mock_sibyl.db
    sys.modules["sibyl.db.models"] = mock_sibyl.db.models

    yield mock_config

    # Restore original modules
    for mod_name, original in saved_modules.items():
        if original is None:
            sys.modules.pop(mod_name, None)
        else:
            sys.modules[mod_name] = original  # type: ignore[assignment]

    # Clear auth module cache again
    for mod_name in list(sys.modules.keys()):
        if mod_name.startswith("sibyl_core.auth"):
            sys.modules.pop(mod_name, None)


# ---------------------------------------------------------------------------
# JWT Tests
# ---------------------------------------------------------------------------


class TestJwt:
    """Tests for JWT token creation and verification."""

    def _import_jwt(self):
        """Import JWT module fresh and install mock settings."""
        if "sibyl_core.auth.jwt" in sys.modules:
            module = importlib.reload(sys.modules["sibyl_core.auth.jwt"])
        else:
            module = importlib.import_module("sibyl_core.auth.jwt")
        mock_config = sys.modules["sibyl.config"]
        module.install_settings_provider(lambda: mock_config.settings)
        return module

    # === Access Token Creation ===

    def test_create_access_token_basic(self, mock_sibyl_modules: MagicMock) -> None:
        """Access token is created with required claims (sub, typ, iat, exp)."""
        jwt_module = self._import_jwt()
        user_id = uuid4()

        token = jwt_module.create_access_token(user_id=user_id)

        # Decode without verification to check payload
        payload = jwt.decode(token, options={"verify_signature": False})

        assert payload["sub"] == str(user_id)
        assert payload["typ"] == "access"
        assert "iat" in payload
        assert "exp" in payload
        assert payload["exp"] > payload["iat"]

    def test_create_access_token_with_org(self, mock_sibyl_modules: MagicMock) -> None:
        """Access token includes organization_id when provided."""
        jwt_module = self._import_jwt()
        user_id = uuid4()
        org_id = uuid4()

        token = jwt_module.create_access_token(user_id=user_id, organization_id=org_id)
        payload = jwt.decode(token, options={"verify_signature": False})

        assert payload["sub"] == str(user_id)
        assert payload["org"] == str(org_id)

    def test_create_access_token_without_org(self, mock_sibyl_modules: MagicMock) -> None:
        """Access token excludes org claim when organization_id is None."""
        jwt_module = self._import_jwt()
        user_id = uuid4()

        token = jwt_module.create_access_token(user_id=user_id, organization_id=None)
        payload = jwt.decode(token, options={"verify_signature": False})

        assert "org" not in payload

    def test_create_access_token_with_extras(self, mock_sibyl_modules: MagicMock) -> None:
        """Access token includes extra_claims when provided."""
        jwt_module = self._import_jwt()
        user_id = uuid4()
        extras = {"role": "admin", "scope": "read write"}

        token = jwt_module.create_access_token(user_id=user_id, extra_claims=extras)
        payload = jwt.decode(token, options={"verify_signature": False})

        assert payload["role"] == "admin"
        assert payload["scope"] == "read write"

    def test_create_access_token_custom_expiry(self, mock_sibyl_modules: MagicMock) -> None:
        """Access token respects custom expires_in parameter."""
        jwt_module = self._import_jwt()
        user_id = uuid4()
        custom_ttl = timedelta(minutes=5)

        token = jwt_module.create_access_token(user_id=user_id, expires_in=custom_ttl)
        payload = jwt.decode(token, options={"verify_signature": False})

        # Expiry should be approximately 5 minutes from now (with small tolerance)
        expected_exp = datetime.now(UTC).timestamp() + custom_ttl.total_seconds()
        assert abs(payload["exp"] - expected_exp) < 5  # 5 second tolerance

    def test_create_access_token_no_secret_raises(self, mock_sibyl_modules: MagicMock) -> None:
        """Creating access token without JWT secret raises JwtError."""
        mock_sibyl_modules.settings.jwt_secret = SecretStr("")
        jwt_module = self._import_jwt()

        with pytest.raises(jwt_module.JwtError, match="JWT secret is not configured"):
            jwt_module.create_access_token(user_id=uuid4())

    # === Access Token Verification ===

    def test_verify_access_token_valid(self, mock_sibyl_modules: MagicMock) -> None:
        """Valid access token is verified successfully."""
        jwt_module = self._import_jwt()
        user_id = uuid4()
        org_id = uuid4()

        token = jwt_module.create_access_token(user_id=user_id, organization_id=org_id)
        claims = jwt_module.verify_access_token(token)

        assert claims["sub"] == str(user_id)
        assert claims["org"] == str(org_id)
        assert claims["typ"] == "access"

    def test_verify_access_token_expired(self, mock_sibyl_modules: MagicMock) -> None:
        """Expired access token raises JwtError."""
        jwt_module = self._import_jwt()
        user_id = uuid4()

        # Create token that already expired
        token = jwt_module.create_access_token(user_id=user_id, expires_in=timedelta(seconds=-10))

        with pytest.raises(jwt_module.JwtError):
            jwt_module.verify_access_token(token)

    def test_verify_access_token_wrong_type(self, mock_sibyl_modules: MagicMock) -> None:
        """Refresh token rejected when verifying as access token."""
        jwt_module = self._import_jwt()
        user_id = uuid4()

        refresh_token, _ = jwt_module.create_refresh_token(user_id=user_id)

        with pytest.raises(jwt_module.JwtError, match="Invalid token type"):
            jwt_module.verify_access_token(refresh_token)

    def test_verify_access_token_tampered(self, mock_sibyl_modules: MagicMock) -> None:
        """Tampered access token fails signature verification."""
        jwt_module = self._import_jwt()
        user_id = uuid4()

        token = jwt_module.create_access_token(user_id=user_id)

        # Tamper with the token (change a character in payload)
        parts = token.split(".")
        tampered_payload = parts[1][:-1] + ("A" if parts[1][-1] != "A" else "B")
        tampered_token = f"{parts[0]}.{tampered_payload}.{parts[2]}"

        with pytest.raises(jwt_module.JwtError):
            jwt_module.verify_access_token(tampered_token)

    def test_verify_access_token_wrong_secret(self, mock_sibyl_modules: MagicMock) -> None:
        """Token signed with different secret fails verification."""
        jwt_module = self._import_jwt()
        user_id = uuid4()

        # Create token with original secret
        token = jwt_module.create_access_token(user_id=user_id)

        # Change secret for verification
        mock_sibyl_modules.settings.jwt_secret = SecretStr("different-jwt-secret-key-for-tests")

        with pytest.raises(jwt_module.JwtError):
            jwt_module.verify_access_token(token)

    def test_verify_access_token_malformed(self, mock_sibyl_modules: MagicMock) -> None:
        """Malformed token string raises JwtError."""
        jwt_module = self._import_jwt()

        with pytest.raises(jwt_module.JwtError):
            jwt_module.verify_access_token("not.a.valid.jwt.token")

    def test_verify_access_token_empty(self, mock_sibyl_modules: MagicMock) -> None:
        """Empty token string raises JwtError."""
        jwt_module = self._import_jwt()

        with pytest.raises(jwt_module.JwtError):
            jwt_module.verify_access_token("")

    # === Refresh Token Creation ===

    def test_create_refresh_token_basic(self, mock_sibyl_modules: MagicMock) -> None:
        """Refresh token is created with required claims and returns expiry datetime."""
        jwt_module = self._import_jwt()
        user_id = uuid4()

        token, expires_at = jwt_module.create_refresh_token(user_id=user_id)
        payload = jwt.decode(token, options={"verify_signature": False})

        assert payload["sub"] == str(user_id)
        assert payload["typ"] == "refresh"
        assert "jti" in payload  # unique token ID
        assert "iat" in payload
        assert "exp" in payload

        # expires_at should be a datetime
        assert isinstance(expires_at, datetime)
        assert expires_at.tzinfo is not None  # Should be timezone-aware

    def test_create_refresh_token_with_org(self, mock_sibyl_modules: MagicMock) -> None:
        """Refresh token includes organization_id when provided."""
        jwt_module = self._import_jwt()
        user_id = uuid4()
        org_id = uuid4()

        token, _ = jwt_module.create_refresh_token(user_id=user_id, organization_id=org_id)
        payload = jwt.decode(token, options={"verify_signature": False})

        assert payload["org"] == str(org_id)

    def test_create_refresh_token_with_session_id(self, mock_sibyl_modules: MagicMock) -> None:
        """Refresh token includes session_id when provided."""
        jwt_module = self._import_jwt()
        user_id = uuid4()
        session_id = uuid4()

        token, _ = jwt_module.create_refresh_token(user_id=user_id, session_id=session_id)
        payload = jwt.decode(token, options={"verify_signature": False})

        assert payload["sid"] == str(session_id)

    def test_create_refresh_token_unique_jti(self, mock_sibyl_modules: MagicMock) -> None:
        """Each refresh token has a unique jti (JWT ID)."""
        jwt_module = self._import_jwt()
        user_id = uuid4()

        token1, _ = jwt_module.create_refresh_token(user_id=user_id)
        token2, _ = jwt_module.create_refresh_token(user_id=user_id)

        payload1 = jwt.decode(token1, options={"verify_signature": False})
        payload2 = jwt.decode(token2, options={"verify_signature": False})

        assert payload1["jti"] != payload2["jti"]

    def test_create_refresh_token_custom_expiry(self, mock_sibyl_modules: MagicMock) -> None:
        """Refresh token respects custom expires_in parameter."""
        jwt_module = self._import_jwt()
        user_id = uuid4()
        custom_ttl = timedelta(days=7)

        token, _expires_at = jwt_module.create_refresh_token(user_id=user_id, expires_in=custom_ttl)
        payload = jwt.decode(token, options={"verify_signature": False})

        # Expiry should be approximately 7 days from now
        expected_exp = datetime.now(UTC).timestamp() + custom_ttl.total_seconds()
        assert abs(payload["exp"] - expected_exp) < 5  # 5 second tolerance

    def test_create_refresh_token_no_secret_raises(self, mock_sibyl_modules: MagicMock) -> None:
        """Creating refresh token without JWT secret raises JwtError."""
        mock_sibyl_modules.settings.jwt_secret = SecretStr("")
        jwt_module = self._import_jwt()

        with pytest.raises(jwt_module.JwtError, match="JWT secret is not configured"):
            jwt_module.create_refresh_token(user_id=uuid4())

    # === Refresh Token Verification ===

    def test_verify_refresh_token_valid(self, mock_sibyl_modules: MagicMock) -> None:
        """Valid refresh token is verified successfully."""
        jwt_module = self._import_jwt()
        user_id = uuid4()
        org_id = uuid4()

        token, _ = jwt_module.create_refresh_token(user_id=user_id, organization_id=org_id)
        claims = jwt_module.verify_refresh_token(token)

        assert claims["sub"] == str(user_id)
        assert claims["org"] == str(org_id)
        assert claims["typ"] == "refresh"
        assert "jti" in claims

    def test_verify_refresh_token_expired(self, mock_sibyl_modules: MagicMock) -> None:
        """Expired refresh token raises JwtError with expiry check enabled."""
        jwt_module = self._import_jwt()
        user_id = uuid4()

        # Create token that already expired
        token, _ = jwt_module.create_refresh_token(
            user_id=user_id, expires_in=timedelta(seconds=-10)
        )

        with pytest.raises(jwt_module.JwtError, match="Refresh token expired"):
            jwt_module.verify_refresh_token(token, verify_expiry=True)

    def test_verify_refresh_token_expired_grace_period(self, mock_sibyl_modules: MagicMock) -> None:
        """Expired refresh token succeeds with verify_expiry=False (grace period)."""
        jwt_module = self._import_jwt()
        user_id = uuid4()

        # Create token that already expired
        token, _ = jwt_module.create_refresh_token(
            user_id=user_id, expires_in=timedelta(seconds=-10)
        )

        # Should succeed with grace period
        claims = jwt_module.verify_refresh_token(token, verify_expiry=False)

        assert claims["sub"] == str(user_id)
        assert claims["typ"] == "refresh"

    def test_verify_refresh_token_wrong_type(self, mock_sibyl_modules: MagicMock) -> None:
        """Access token rejected when verifying as refresh token.

        Access tokens fail verification as refresh tokens because they lack
        the required 'jti' claim (checked before type verification).
        """
        jwt_module = self._import_jwt()
        user_id = uuid4()

        access_token = jwt_module.create_access_token(user_id=user_id)

        # Access tokens lack 'jti', so they fail the required claims check first
        with pytest.raises(jwt_module.JwtError, match='missing the "jti" claim'):
            jwt_module.verify_refresh_token(access_token)

    def test_verify_refresh_token_tampered(self, mock_sibyl_modules: MagicMock) -> None:
        """Tampered refresh token fails signature verification."""
        jwt_module = self._import_jwt()
        user_id = uuid4()

        token, _ = jwt_module.create_refresh_token(user_id=user_id)

        # Tamper with signature
        parts = token.split(".")
        tampered_token = f"{parts[0]}.{parts[1]}.invalidSignature"

        with pytest.raises(jwt_module.JwtError):
            jwt_module.verify_refresh_token(tampered_token)

    def test_verify_refresh_token_missing_jti(self, mock_sibyl_modules: MagicMock) -> None:
        """Refresh token without jti claim fails verification."""
        jwt_module = self._import_jwt()
        secret = mock_sibyl_modules.settings.jwt_secret.get_secret_value()

        # Manually create a token without jti
        payload = {
            "sub": str(uuid4()),
            "typ": "refresh",
            "iat": int(datetime.now(UTC).timestamp()),
            "exp": int((datetime.now(UTC) + timedelta(days=1)).timestamp()),
            # Note: no jti
        }
        token = jwt.encode(payload, secret, algorithm="HS256")

        with pytest.raises(jwt_module.JwtError):
            jwt_module.verify_refresh_token(token)

    # === Decode Token Unverified ===

    def test_decode_token_unverified_valid(self, mock_sibyl_modules: MagicMock) -> None:
        """decode_token_unverified returns claims without signature check."""
        jwt_module = self._import_jwt()
        user_id = uuid4()

        token = jwt_module.create_access_token(user_id=user_id)

        # Change secret so signature verification would fail
        mock_sibyl_modules.settings.jwt_secret = SecretStr("different-jwt-secret-key-for-tests")

        # Should still decode successfully
        claims = jwt_module.decode_token_unverified(token)

        assert claims["sub"] == str(user_id)
        assert claims["typ"] == "access"

    def test_decode_token_unverified_malformed(self, mock_sibyl_modules: MagicMock) -> None:
        """decode_token_unverified returns empty dict for malformed token."""
        jwt_module = self._import_jwt()

        claims = jwt_module.decode_token_unverified("not.a.valid.jwt")

        assert claims == {}

    def test_decode_token_unverified_empty(self, mock_sibyl_modules: MagicMock) -> None:
        """decode_token_unverified returns empty dict for empty string."""
        jwt_module = self._import_jwt()

        claims = jwt_module.decode_token_unverified("")

        assert claims == {}


# ---------------------------------------------------------------------------
# Password Tests
# ---------------------------------------------------------------------------


class TestPasswords:
    """Tests for password hashing and verification."""

    def _import_passwords(self):
        """Import passwords module fresh and install mock settings."""
        if "sibyl_core.auth.passwords" in sys.modules:
            module = importlib.reload(sys.modules["sibyl_core.auth.passwords"])
        else:
            module = importlib.import_module("sibyl_core.auth.passwords")
        mock_config = sys.modules["sibyl.config"]
        module.install_settings_provider(lambda: mock_config.settings)
        return module

    # === Hash Password ===

    def test_hash_password_basic(self, mock_sibyl_modules: MagicMock) -> None:
        """hash_password returns a PasswordHash with salt, hash, and iterations."""
        pwd_module = self._import_passwords()

        result = pwd_module.hash_password("mysecretpassword")

        assert hasattr(result, "salt_hex")
        assert hasattr(result, "hash_hex")
        assert hasattr(result, "iterations")

        # Should be valid hex strings
        bytes.fromhex(result.salt_hex)  # Should not raise
        bytes.fromhex(result.hash_hex)  # Should not raise

        assert result.iterations == mock_sibyl_modules.settings.password_iterations

    def test_hash_password_empty_raises(self, mock_sibyl_modules: MagicMock) -> None:
        """hash_password raises PasswordError for empty password."""
        pwd_module = self._import_passwords()

        with pytest.raises(pwd_module.PasswordError, match="Password is empty"):
            pwd_module.hash_password("")

    def test_hash_password_deterministic_with_salt(self, mock_sibyl_modules: MagicMock) -> None:
        """Same password + salt + iterations = same hash (deterministic)."""
        pwd_module = self._import_passwords()
        password = "testpassword"
        salt = b"\x00" * 16
        iterations = 100_000

        result1 = pwd_module.hash_password(password, salt=salt, iterations=iterations)
        result2 = pwd_module.hash_password(password, salt=salt, iterations=iterations)

        assert result1.hash_hex == result2.hash_hex
        assert result1.salt_hex == result2.salt_hex

    def test_hash_password_different_salt_different_hash(
        self, mock_sibyl_modules: MagicMock
    ) -> None:
        """Different salt produces different hash."""
        pwd_module = self._import_passwords()
        password = "testpassword"
        iterations = 100_000

        result1 = pwd_module.hash_password(password, salt=b"\x00" * 16, iterations=iterations)
        result2 = pwd_module.hash_password(password, salt=b"\x01" * 16, iterations=iterations)

        assert result1.hash_hex != result2.hash_hex

    def test_hash_password_random_salt_when_not_provided(
        self, mock_sibyl_modules: MagicMock
    ) -> None:
        """hash_password generates random salt when not provided."""
        pwd_module = self._import_passwords()
        password = "testpassword"

        result1 = pwd_module.hash_password(password)
        result2 = pwd_module.hash_password(password)

        # Salt should be different (random)
        assert result1.salt_hex != result2.salt_hex
        # Hash should also be different (due to different salt)
        assert result1.hash_hex != result2.hash_hex

    def test_hash_password_uses_pepper(self, mock_sibyl_modules: MagicMock) -> None:
        """Password pepper is incorporated into the hash."""
        pwd_module = self._import_passwords()
        password = "testpassword"
        salt = b"\x00" * 16
        iterations = 100_000

        # Hash with one pepper
        mock_sibyl_modules.settings.password_pepper = SecretStr("pepper1")
        result1 = pwd_module.hash_password(password, salt=salt, iterations=iterations)

        # Hash with different pepper
        mock_sibyl_modules.settings.password_pepper = SecretStr("pepper2")
        result2 = pwd_module.hash_password(password, salt=salt, iterations=iterations)

        assert result1.hash_hex != result2.hash_hex

    def test_hash_password_custom_iterations(self, mock_sibyl_modules: MagicMock) -> None:
        """hash_password respects custom iterations parameter."""
        pwd_module = self._import_passwords()
        password = "testpassword"
        custom_iterations = 50_000

        result = pwd_module.hash_password(password, iterations=custom_iterations)

        assert result.iterations == custom_iterations

    def test_hash_password_unicode(self, mock_sibyl_modules: MagicMock) -> None:
        """hash_password handles unicode passwords correctly."""
        pwd_module = self._import_passwords()
        password = "p@ssw0rd!$"

        result = pwd_module.hash_password(password)

        assert result.salt_hex
        assert result.hash_hex

    # === Verify Password ===

    def test_verify_password_correct(self, mock_sibyl_modules: MagicMock) -> None:
        """verify_password returns True for correct password."""
        pwd_module = self._import_passwords()
        password = "mysecretpassword"

        hashed = pwd_module.hash_password(password)
        result = pwd_module.verify_password(
            password,
            salt_hex=hashed.salt_hex,
            hash_hex=hashed.hash_hex,
            iterations=hashed.iterations,
        )

        assert result is True

    def test_verify_password_wrong(self, mock_sibyl_modules: MagicMock) -> None:
        """verify_password returns False for wrong password."""
        pwd_module = self._import_passwords()
        password = "mysecretpassword"
        wrong_password = "wrongpassword"

        hashed = pwd_module.hash_password(password)
        result = pwd_module.verify_password(
            wrong_password,
            salt_hex=hashed.salt_hex,
            hash_hex=hashed.hash_hex,
            iterations=hashed.iterations,
        )

        assert result is False

    def test_verify_password_empty(self, mock_sibyl_modules: MagicMock) -> None:
        """verify_password returns False for empty password (doesn't raise)."""
        pwd_module = self._import_passwords()
        hashed = pwd_module.hash_password("somepassword")

        result = pwd_module.verify_password(
            "",
            salt_hex=hashed.salt_hex,
            hash_hex=hashed.hash_hex,
            iterations=hashed.iterations,
        )

        assert result is False

    def test_verify_password_timing_floor_burns_for_empty_password(
        self, mock_sibyl_modules: MagicMock
    ) -> None:
        """verify_password_timing_floor always executes PBKDF2."""
        pwd_module = self._import_passwords()

        with patch.object(pwd_module, "pbkdf2_hmac", wraps=pwd_module.pbkdf2_hmac) as pbkdf2:
            assert pwd_module.verify_password_timing_floor("", iterations=1) is None
            assert pwd_module.verify_password_timing_floor("candidate", iterations=1) is None

        assert pbkdf2.call_count == 2
        assert [call.args[3] for call in pbkdf2.call_args_list] == [1, 1]

    def test_verify_password_malformed_hex_salt(self, mock_sibyl_modules: MagicMock) -> None:
        """verify_password returns False for malformed salt hex."""
        pwd_module = self._import_passwords()

        result = pwd_module.verify_password(
            "password",
            salt_hex="zzzz-not-valid-hex",
            hash_hex="a" * 64,
            iterations=100_000,
        )

        assert result is False

    def test_verify_password_malformed_hex_hash(self, mock_sibyl_modules: MagicMock) -> None:
        """verify_password returns False for malformed hash hex."""
        pwd_module = self._import_passwords()

        result = pwd_module.verify_password(
            "password",
            salt_hex="a" * 32,
            hash_hex="not-valid-hex!!!",
            iterations=100_000,
        )

        assert result is False

    def test_verify_password_case_sensitive(self, mock_sibyl_modules: MagicMock) -> None:
        """verify_password is case-sensitive."""
        pwd_module = self._import_passwords()
        password = "MyPassword"

        hashed = pwd_module.hash_password(password)

        assert (
            pwd_module.verify_password(
                "mypassword",
                salt_hex=hashed.salt_hex,
                hash_hex=hashed.hash_hex,
                iterations=hashed.iterations,
            )
            is False
        )
        assert (
            pwd_module.verify_password(
                "MYPASSWORD",
                salt_hex=hashed.salt_hex,
                hash_hex=hashed.hash_hex,
                iterations=hashed.iterations,
            )
            is False
        )
        assert (
            pwd_module.verify_password(
                "MyPassword",
                salt_hex=hashed.salt_hex,
                hash_hex=hashed.hash_hex,
                iterations=hashed.iterations,
            )
            is True
        )

    def test_verify_password_different_iterations(self, mock_sibyl_modules: MagicMock) -> None:
        """verify_password fails with wrong iteration count."""
        pwd_module = self._import_passwords()
        password = "mysecretpassword"
        iterations = 100_000

        hashed = pwd_module.hash_password(password, iterations=iterations)

        # Verify with different iterations should fail
        result = pwd_module.verify_password(
            password,
            salt_hex=hashed.salt_hex,
            hash_hex=hashed.hash_hex,
            iterations=iterations + 1,  # Wrong!
        )

        assert result is False

    def test_verify_password_roundtrip_unicode(self, mock_sibyl_modules: MagicMock) -> None:
        """Password with unicode characters verifies correctly."""
        pwd_module = self._import_passwords()
        password = "p@sswrd"

        hashed = pwd_module.hash_password(password)
        result = pwd_module.verify_password(
            password,
            salt_hex=hashed.salt_hex,
            hash_hex=hashed.hash_hex,
            iterations=hashed.iterations,
        )

        assert result is True

    def test_verify_password_wrong_pepper(self, mock_sibyl_modules: MagicMock) -> None:
        """Password verification fails if pepper changed after hashing."""
        pwd_module = self._import_passwords()
        password = "mysecretpassword"

        # Hash with one pepper
        mock_sibyl_modules.settings.password_pepper = SecretStr("original-pepper")
        hashed = pwd_module.hash_password(password)

        # Verify with different pepper
        mock_sibyl_modules.settings.password_pepper = SecretStr("different-pepper")
        result = pwd_module.verify_password(
            password,
            salt_hex=hashed.salt_hex,
            hash_hex=hashed.hash_hex,
            iterations=hashed.iterations,
        )

        assert result is False
