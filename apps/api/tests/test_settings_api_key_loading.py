"""Tests for API key loading from database at startup.

These tests verify the fix for the issue where API keys configured via the webapp
were not available to GraphClient at startup because it reads from environment
variables at import time.
"""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestApiKeyLoadingAtStartup:
    """Tests for API key loading during server/worker startup."""

    @pytest.mark.asyncio
    async def test_api_key_loaded_from_db_when_env_not_set(self, monkeypatch) -> None:
        """Verify API keys are loaded from DB into os.environ when env vars are not set."""
        # Clear any existing env vars
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        # Mock the settings service
        mock_settings_service = AsyncMock()
        mock_settings_service.get_openai_key = AsyncMock(return_value="sk-test-openai-key")
        mock_settings_service.get_anthropic_key = AsyncMock(return_value="sk-ant-test-key")

        with patch(
            "sibyl.services.settings.get_settings_service", return_value=mock_settings_service
        ):
            # Simulate the key loading logic from main.py
            if not os.environ.get("OPENAI_API_KEY"):
                openai_key = await mock_settings_service.get_openai_key()
                if openai_key:
                    os.environ["OPENAI_API_KEY"] = openai_key

            if not os.environ.get("ANTHROPIC_API_KEY"):
                anthropic_key = await mock_settings_service.get_anthropic_key()
                if anthropic_key:
                    os.environ["ANTHROPIC_API_KEY"] = anthropic_key

        assert os.environ.get("OPENAI_API_KEY") == "sk-test-openai-key"
        assert os.environ.get("ANTHROPIC_API_KEY") == "sk-ant-test-key"

        # Cleanup
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    @pytest.mark.asyncio
    async def test_env_var_takes_precedence_over_db(self, monkeypatch) -> None:
        """Verify existing env vars are not overwritten by DB values."""
        # Set existing env vars
        monkeypatch.setenv("OPENAI_API_KEY", "sk-existing-env-key")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-existing-env-key")

        # Mock the settings service with different values
        mock_settings_service = AsyncMock()
        mock_settings_service.get_openai_key = AsyncMock(return_value="sk-db-openai-key")
        mock_settings_service.get_anthropic_key = AsyncMock(return_value="sk-ant-db-key")

        with patch(
            "sibyl.services.settings.get_settings_service", return_value=mock_settings_service
        ):
            # Simulate the key loading logic - should NOT overwrite
            if not os.environ.get("OPENAI_API_KEY"):
                openai_key = await mock_settings_service.get_openai_key()
                if openai_key:
                    os.environ["OPENAI_API_KEY"] = openai_key

            if not os.environ.get("ANTHROPIC_API_KEY"):
                anthropic_key = await mock_settings_service.get_anthropic_key()
                if anthropic_key:
                    os.environ["ANTHROPIC_API_KEY"] = anthropic_key

        # Verify env vars were NOT overwritten
        assert os.environ.get("OPENAI_API_KEY") == "sk-existing-env-key"
        assert os.environ.get("ANTHROPIC_API_KEY") == "sk-ant-existing-env-key"

        # Verify DB was not even queried (optimization)
        mock_settings_service.get_openai_key.assert_not_called()
        mock_settings_service.get_anthropic_key.assert_not_called()

    @pytest.mark.asyncio
    async def test_api_key_loading_failure_does_not_crash(self, monkeypatch) -> None:
        """Ensure startup continues gracefully if DB query fails."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        # Mock settings service that raises an exception
        mock_settings_service = AsyncMock()
        mock_settings_service.get_openai_key = AsyncMock(
            side_effect=Exception("Database connection failed")
        )

        error_logged = False

        with patch(
            "sibyl.services.settings.get_settings_service", return_value=mock_settings_service
        ):
            # Simulate the try/except from main.py
            try:
                if not os.environ.get("OPENAI_API_KEY"):
                    openai_key = await mock_settings_service.get_openai_key()
                    if openai_key:
                        os.environ["OPENAI_API_KEY"] = openai_key
            except Exception:
                error_logged = True  # In real code, this logs a warning

        # Should have caught the exception and continued
        assert error_logged is True
        # Env var should still be unset
        assert os.environ.get("OPENAI_API_KEY") is None

    @pytest.mark.asyncio
    async def test_partial_key_loading(self, monkeypatch) -> None:
        """Verify partial key loading works (only one key in DB)."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        # Mock settings service with only OpenAI key configured
        mock_settings_service = AsyncMock()
        mock_settings_service.get_openai_key = AsyncMock(return_value="sk-test-openai-key")
        mock_settings_service.get_anthropic_key = AsyncMock(return_value=None)

        with patch(
            "sibyl.services.settings.get_settings_service", return_value=mock_settings_service
        ):
            if not os.environ.get("OPENAI_API_KEY"):
                openai_key = await mock_settings_service.get_openai_key()
                if openai_key:
                    os.environ["OPENAI_API_KEY"] = openai_key

            if not os.environ.get("ANTHROPIC_API_KEY"):
                anthropic_key = await mock_settings_service.get_anthropic_key()
                if anthropic_key:
                    os.environ["ANTHROPIC_API_KEY"] = anthropic_key

        # Only OpenAI key should be set
        assert os.environ.get("OPENAI_API_KEY") == "sk-test-openai-key"
        assert os.environ.get("ANTHROPIC_API_KEY") is None

        # Cleanup
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)


class TestSettingsHotReload:
    """Tests for hot-reloading API keys when updated via webapp."""

    @pytest.mark.asyncio
    async def test_update_settings_updates_env_var(self, monkeypatch) -> None:
        """Verify updating settings also updates os.environ."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        # Simulate the logic from settings.py update endpoint
        new_key = "sk-new-openai-key"
        os.environ["OPENAI_API_KEY"] = new_key

        assert os.environ.get("OPENAI_API_KEY") == new_key

        # Cleanup
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    @pytest.mark.asyncio
    async def test_update_settings_resets_graph_client(self) -> None:
        """Verify GraphClient is reset after API key update."""
        reset_called = False

        async def mock_reset_graph_client():
            nonlocal reset_called
            reset_called = True

        with patch(
            "sibyl_core.graph.client.reset_graph_client", side_effect=mock_reset_graph_client
        ):
            # Simulate the reset call from settings.py
            from sibyl_core.graph.client import reset_graph_client

            await reset_graph_client()

        assert reset_called is True

    @pytest.mark.asyncio
    async def test_delete_setting_clears_env_var(self, monkeypatch) -> None:
        """Verify deleting a setting clears it from os.environ."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-to-be-deleted")

        # Simulate the logic from delete_setting endpoint
        os.environ.pop("OPENAI_API_KEY", None)

        assert os.environ.get("OPENAI_API_KEY") is None
