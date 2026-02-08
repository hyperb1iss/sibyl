"""Tests for sandbox controller — runner token minting and pod manifest injection."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from sibyl.agents.sandbox_controller import SandboxController, SandboxControllerError
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


# ---------------------------------------------------------------------------
# Lifecycle tests (mocked K8s + mocked session)
# ---------------------------------------------------------------------------


class TestSandboxLifecycle:
    """Full lifecycle: ensure -> suspend -> resume -> destroy."""

    @pytest.fixture
    def mock_session(self):
        """Mock async session with sandbox-like objects."""
        session = AsyncMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        return session

    @pytest.fixture
    def lifecycle_controller(self, mock_session):
        """Controller with mocked session factory and K8s."""
        factory = AsyncMock(return_value=mock_session)
        return SandboxController(
            session_factory=factory,
            enabled=True,
            server_url="http://localhost:3334",
            k8s_required=False,
        )

    def test_pod_name_deterministic(self, lifecycle_controller, ids):
        """Same sandbox ID always produces the same pod name."""
        name1 = lifecycle_controller._pod_name_for(ids.sandbox)
        name2 = lifecycle_controller._pod_name_for(ids.sandbox)
        assert name1 == name2
        assert name1.startswith("sibyl-sandbox-")

    def test_pod_name_lowercase(self, lifecycle_controller, ids):
        """Pod names are always lowercase."""
        name = lifecycle_controller._pod_name_for(ids.sandbox)
        assert name == name.lower()

    def test_pod_name_truncated(self, lifecycle_controller, ids):
        """Pod name uses first 24 chars of sandbox ID."""
        name = lifecycle_controller._pod_name_for(ids.sandbox)
        sid = str(ids.sandbox).replace("_", "-")
        expected = f"sibyl-sandbox-{sid[:24]}".lower()
        assert name == expected

    def test_require_enabled_raises_when_disabled(self):
        """Controller methods raise when disabled."""
        ctrl = SandboxController(
            session_factory=AsyncMock(),
            enabled=False,
        )
        with pytest.raises(SandboxControllerError, match="disabled"):
            ctrl._require_enabled()

    def test_require_enabled_passes_when_enabled(self, lifecycle_controller):
        """No exception when controller is enabled."""
        lifecycle_controller._require_enabled()

    def test_active_statuses_are_tracked(self):
        """Verify the set of active statuses hasn't changed unexpectedly."""
        assert "running" in SandboxController.ACTIVE_STATUSES
        assert "pending" in SandboxController.ACTIVE_STATUSES
        assert "starting" in SandboxController.ACTIVE_STATUSES
        assert "deleted" not in SandboxController.ACTIVE_STATUSES

    def test_terminal_statuses(self):
        """Verify terminal statuses."""
        assert "deleted" in SandboxController.TERMINAL_STATUSES
        assert "running" not in SandboxController.TERMINAL_STATUSES

    def test_k8s_available_false_by_default(self, lifecycle_controller):
        """K8s starts unavailable until explicitly checked."""
        assert lifecycle_controller.k8s_available is False

    def test_runtime_error_none_initially(self, lifecycle_controller):
        """No runtime error before any operations."""
        assert lifecycle_controller.runtime_error is None

    def test_namespace_default(self):
        """Default namespace is 'default'."""
        ctrl = SandboxController(session_factory=AsyncMock(), enabled=True)
        assert ctrl.namespace == "default"

    def test_namespace_custom(self):
        """Custom namespace is respected."""
        ctrl = SandboxController(
            session_factory=AsyncMock(), enabled=True, namespace="sibyl-prod"
        )
        assert ctrl.namespace == "sibyl-prod"

    def test_constructor_defaults(self):
        """Constructor defaults are sane."""
        ctrl = SandboxController(session_factory=AsyncMock())
        assert ctrl.enabled is False
        assert ctrl.pod_prefix == "sibyl-sandbox"
        assert ctrl.reconcile_interval_seconds == 20
        assert ctrl.idle_ttl_seconds == 1800
        assert ctrl.max_lifetime_seconds == 14400
        assert ctrl.k8s_required is False


class TestSandboxAdminEndpoints:
    """Test suspend_all and find_orphaned_pods controller methods."""

    async def test_suspend_all_returns_zero_when_none_active(self):
        """No active sandboxes -> 0 suspended."""
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        # Mock execute -> result.scalars().all() chain (sync calls on awaited result)
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = []
        mock_result = MagicMock()
        mock_result.scalars.return_value = mock_scalars
        mock_session.execute = AsyncMock(return_value=mock_result)

        factory = MagicMock(return_value=mock_session)
        ctrl = SandboxController(
            session_factory=factory,
            enabled=True,
            k8s_required=False,
        )

        count = await ctrl.suspend_all(uuid4())
        assert count == 0

    async def test_find_orphaned_pods_returns_empty_without_k8s(self):
        """Without K8s, no orphans found."""
        factory = AsyncMock()
        ctrl = SandboxController(
            session_factory=factory,
            enabled=True,
            k8s_required=False,
        )
        # K8s not initialized -> should return empty
        orphans = await ctrl.find_orphaned_pods(uuid4())
        assert orphans == []

    async def test_suspend_all_raises_when_disabled(self):
        """suspend_all raises when controller disabled."""
        ctrl = SandboxController(
            session_factory=AsyncMock(),
            enabled=False,
        )
        with pytest.raises(SandboxControllerError, match="disabled"):
            await ctrl.suspend_all(uuid4())

    def test_is_not_found_true_for_404(self):
        """_is_not_found returns True for status 404."""
        ctrl = SandboxController(session_factory=AsyncMock(), enabled=True)
        exc = type("FakeExc", (), {"status": 404})()
        assert ctrl._is_not_found(exc) is True

    def test_is_not_found_false_for_500(self):
        """_is_not_found returns False for non-404."""
        ctrl = SandboxController(session_factory=AsyncMock(), enabled=True)
        exc = type("FakeExc", (), {"status": 500})()
        assert ctrl._is_not_found(exc) is False

    def test_is_not_found_false_without_status(self):
        """_is_not_found returns False when no status attr."""
        ctrl = SandboxController(session_factory=AsyncMock(), enabled=True)
        assert ctrl._is_not_found(RuntimeError("nope")) is False
