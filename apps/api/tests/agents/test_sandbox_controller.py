"""Tests for sandbox controller — runner token minting and pod manifest injection."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from sibyl.agents.sandbox_controller import SandboxController
from sibyl.auth.jwt import JwtError, create_runner_token, verify_access_token
from sibyl.config import Settings

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _jwt_env(monkeypatch):
    """Bootstrap JWT config for all tests in this module."""
    monkeypatch.setenv("SIBYL_JWT_SECRET", "test-sandbox-secret")
    monkeypatch.setenv("SIBYL_JWT_ALGORITHM", "HS256")
    from sibyl import config as config_module

    config_module.settings = Settings(_env_file=None)  # type: ignore[assignment]


@pytest.fixture
def ids():
    """Fresh set of UUIDs for each test."""
    return SimpleNamespace(
        user=uuid4(),
        org=uuid4(),
        runner=uuid4(),
        sandbox=uuid4(),
    )


@pytest.fixture
def controller():
    """Minimal sandbox controller with mocked session factory."""
    session_factory = AsyncMock()
    return SandboxController(
        session_factory=session_factory,
        enabled=True,
        server_url="http://localhost:3334",
    )


# ---------------------------------------------------------------------------
# create_runner_token — unit tests
# ---------------------------------------------------------------------------


class TestCreateRunnerToken:
    """Verify token shape, claims, and scoping."""

    def test_sandbox_bound_token(self, ids):
        """Token with sandbox_id gets 'sandbox:runner' scope."""
        token = create_runner_token(
            user_id=ids.user,
            organization_id=ids.org,
            runner_id=ids.runner,
            sandbox_id=ids.sandbox,
        )
        claims = verify_access_token(token)

        assert claims["sub"] == str(ids.user)
        assert claims["org"] == str(ids.org)
        assert claims["rid"] == str(ids.runner)
        assert claims["sid"] == str(ids.sandbox)
        assert claims["scp"] == "sandbox:runner"
        assert claims["typ"] == "access"

    def test_standalone_runner_token(self, ids):
        """Token without sandbox_id gets broader runner scopes."""
        token = create_runner_token(
            user_id=ids.user,
            organization_id=ids.org,
            runner_id=ids.runner,
        )
        claims = verify_access_token(token)

        assert claims["rid"] == str(ids.runner)
        assert "sid" not in claims
        assert claims["scp"] == "runner runner:connect mcp"

    def test_custom_expiry(self, ids):
        """Custom TTL is respected in token claims."""
        token = create_runner_token(
            user_id=ids.user,
            organization_id=ids.org,
            runner_id=ids.runner,
            expires_in=timedelta(minutes=30),
        )
        claims = verify_access_token(token)
        ttl = claims["exp"] - claims["iat"]
        assert ttl == 30 * 60

    def test_default_expiry_is_24h(self, ids):
        """Default TTL is 24 hours."""
        token = create_runner_token(
            user_id=ids.user,
            organization_id=ids.org,
            runner_id=ids.runner,
        )
        claims = verify_access_token(token)
        ttl = claims["exp"] - claims["iat"]
        assert ttl == 24 * 60 * 60

    def test_token_verifies_successfully(self, ids):
        """Round-trip: mint → verify → claims match."""
        token = create_runner_token(
            user_id=ids.user,
            organization_id=ids.org,
            runner_id=ids.runner,
            sandbox_id=ids.sandbox,
        )
        # Should not raise
        claims = verify_access_token(token)
        assert claims["sub"] == str(ids.user)

    def test_token_fails_with_wrong_secret(self, ids, monkeypatch):
        """Token minted with one secret fails verification with another."""
        token = create_runner_token(
            user_id=ids.user,
            organization_id=ids.org,
            runner_id=ids.runner,
        )
        # Rotate secret
        monkeypatch.setenv("SIBYL_JWT_SECRET", "different-secret")
        from sibyl import config as config_module

        config_module.settings = Settings(_env_file=None)  # type: ignore[assignment]

        with pytest.raises(JwtError):
            verify_access_token(token)


# ---------------------------------------------------------------------------
# _pod_manifest — token injection into env vars
# ---------------------------------------------------------------------------


class TestPodManifestTokenInjection:
    """Verify runner token appears in pod env when IDs are present."""

    def _make_sandbox(self, ids, *, include_runner: bool = True):
        """Build a minimal sandbox-like namespace."""
        return SimpleNamespace(
            id=ids.sandbox,
            organization_id=ids.org,
            user_id=ids.user,
            runner_id=ids.runner if include_runner else None,
            image="ghcr.io/hyperb1iss/sibyl-sandbox:latest",
            cpu_request="250m",
            memory_request="512Mi",
            ephemeral_storage_request="1Gi",
            cpu_limit="1000m",
            memory_limit="2Gi",
            ephemeral_storage_limit="4Gi",
        )

    def test_token_injected_when_runner_present(self, controller, ids):
        """SIBYL_RUNNER_TOKEN env var appears when runner_id/org_id/user_id exist."""
        sandbox = self._make_sandbox(ids)
        manifest = controller._pod_manifest(pod_name="test-pod", sandbox=sandbox)

        env_vars = manifest["spec"]["containers"][0]["env"]
        env_names = {e["name"] for e in env_vars}

        assert "SIBYL_RUNNER_TOKEN" in env_names
        assert "SIBYL_RUNNER_ID" in env_names
        assert "SIBYL_SANDBOX_ID" in env_names

        # Verify the token is a valid JWT with correct claims
        token_var = next(e for e in env_vars if e["name"] == "SIBYL_RUNNER_TOKEN")
        claims = verify_access_token(token_var["value"])
        assert claims["rid"] == str(ids.runner)
        assert claims["sid"] == str(ids.sandbox)
        assert claims["scp"] == "sandbox:runner"

    def test_no_token_without_runner_id(self, controller, ids):
        """No SIBYL_RUNNER_TOKEN when runner_id is absent."""
        sandbox = self._make_sandbox(ids, include_runner=False)
        manifest = controller._pod_manifest(pod_name="test-pod", sandbox=sandbox)

        env_vars = manifest["spec"]["containers"][0]["env"]
        env_names = {e["name"] for e in env_vars}

        assert "SIBYL_RUNNER_TOKEN" not in env_names
        assert "SIBYL_RUNNER_ID" not in env_names

    def test_token_mint_failure_is_non_fatal(self, controller, ids):
        """If JWT minting fails, pod manifest still builds (no token env var)."""
        sandbox = self._make_sandbox(ids)

        # Patch at the source — the lazy import inside _pod_manifest pulls from sibyl.auth.jwt
        with patch(
            "sibyl.auth.jwt.create_runner_token",
            side_effect=JwtError("boom"),
        ):
            manifest = controller._pod_manifest(pod_name="test-pod", sandbox=sandbox)

        env_vars = manifest["spec"]["containers"][0]["env"]
        env_names = {e["name"] for e in env_vars}

        # Token mint failed but manifest was still produced
        assert "SIBYL_SANDBOX_ID" in env_names
        assert "SIBYL_RUNNER_ID" in env_names
        assert "SIBYL_RUNNER_TOKEN" not in env_names

    def test_manifest_structure_unchanged(self, controller, ids):
        """Token injection doesn't break existing manifest structure."""
        sandbox = self._make_sandbox(ids)
        manifest = controller._pod_manifest(pod_name="test-pod", sandbox=sandbox)

        assert manifest["apiVersion"] == "v1"
        assert manifest["kind"] == "Pod"
        assert manifest["metadata"]["name"] == "test-pod"
        assert manifest["metadata"]["labels"]["app"] == "sibyl-sandbox"

        container = manifest["spec"]["containers"][0]
        assert container["name"] == "runner"
        assert container["securityContext"]["runAsNonRoot"] is True
        assert container["securityContext"]["allowPrivilegeEscalation"] is False
        assert container["resources"]["requests"]["cpu"] == "250m"
