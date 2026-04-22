from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest

from sibyl.db.models import SystemSetting
from sibyl.services.settings import SettingsService


@pytest.mark.asyncio
async def test_settings_service_get_uses_runtime_helper_and_cache() -> None:
    @asynccontextmanager
    async def mock_session():
        yield None

    service = SettingsService(lambda: mock_session())
    setting = SystemSetting(key="openai_api_key", value="ciphertext", is_secret=True)

    with (
        patch(
            "sibyl.services.settings.get_system_setting",
            AsyncMock(return_value=setting),
        ) as get_setting,
        patch("sibyl.services.settings.decrypt_value", return_value="sk-openai"),
    ):
        first = await service.get("openai_api_key")
        second = await service.get("openai_api_key")

    assert first == "sk-openai"
    assert second == "sk-openai"
    get_setting.assert_awaited_once_with(None, key="openai_api_key")


@pytest.mark.asyncio
async def test_settings_service_set_uses_runtime_save_and_invalidates_cache() -> None:
    @asynccontextmanager
    async def mock_session():
        yield None

    service = SettingsService(lambda: mock_session())
    service._cache["openai_api_key"] = object()  # type: ignore[assignment]

    with (
        patch(
            "sibyl.services.settings.get_system_setting",
            AsyncMock(return_value=None),
        ) as get_setting,
        patch(
            "sibyl.services.settings.save_system_setting",
            AsyncMock(),
        ) as save_setting,
        patch("sibyl.services.settings.encrypt_value", return_value="ciphertext"),
    ):
        await service.set(
            "openai_api_key",
            "sk-openai",
            description="OpenAI key",
        )

    get_setting.assert_awaited_once_with(None, key="openai_api_key")
    save_setting.assert_awaited_once()
    saved = save_setting.await_args.kwargs["setting"]
    assert isinstance(saved, SystemSetting)
    assert saved.key == "openai_api_key"
    assert saved.value == "ciphertext"
    assert saved.is_secret is True
    assert saved.description == "OpenAI key"
    assert "openai_api_key" not in service._cache


@pytest.mark.asyncio
async def test_settings_service_get_all_uses_runtime_listing() -> None:
    @asynccontextmanager
    async def mock_session():
        yield None

    service = SettingsService(lambda: mock_session())
    settings = [
        SystemSetting(key="openai_api_key", value="ciphertext", is_secret=True),
        SystemSetting(key="feature_flag", value="enabled", is_secret=False),
    ]

    with (
        patch(
            "sibyl.services.settings.list_system_settings",
            AsyncMock(return_value=settings),
        ) as list_settings,
        patch("sibyl.services.settings.decrypt_value", return_value="sk-openai"),
        patch("sibyl.services.settings.mask_secret", return_value="sk-***"),
    ):
        result = await service.get_all(include_secrets=False)

    list_settings.assert_awaited_once_with(None)
    assert result["openai_api_key"] == {
        "configured": True,
        "source": "database",
        "is_secret": True,
        "value": None,
        "masked": "sk-***",
        "description": None,
    }
    assert result["feature_flag"] == {
        "configured": True,
        "source": "database",
        "is_secret": False,
        "value": "enabled",
        "masked": None,
        "description": None,
    }
