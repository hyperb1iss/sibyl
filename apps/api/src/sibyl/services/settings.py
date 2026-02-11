"""Settings service with database-first lookup.

Provides API keys and other configuration with:
1. Database lookup (SystemSettings table)
2. Environment variable fallback
3. Caching for performance
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import structlog
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from sibyl.crypto import decrypt_value, encrypt_value, mask_secret
from sibyl.db.models import SystemSetting

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractAsyncContextManager

log = structlog.get_logger()

# Cache TTL in seconds
_CACHE_TTL = 60

# Known settings with their env var mappings and fallbacks
_SETTING_ENV_VARS: dict[str, list[str]] = {
    "openai_api_key": ["SIBYL_OPENAI_API_KEY", "OPENAI_API_KEY"],
    "anthropic_api_key": ["SIBYL_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"],
}

# Settings that should be encrypted
_SECRET_SETTINGS = {"openai_api_key", "anthropic_api_key"}


class _CacheEntry:
    """Cache entry with TTL tracking."""

    __slots__ = ("expires_at", "value")

    def __init__(self, value: str | None, ttl_seconds: int = _CACHE_TTL) -> None:
        self.value = value
        self.expires_at = datetime.now(UTC) + timedelta(seconds=ttl_seconds)

    @property
    def is_expired(self) -> bool:
        return datetime.now(UTC) > self.expires_at


class SettingsService:
    """Service for managing system settings with DB-first lookup.

    Settings are checked in this order:
    1. In-memory cache (if not expired)
    2. Database (SystemSettings table)
    3. Environment variable
    4. Default value (None)

    Encrypted values are automatically decrypted on read and encrypted on write.
    """

    def __init__(
        self, session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]]
    ) -> None:
        """Initialize the settings service.

        Args:
            session_factory: Callable that returns an async database session.
        """
        self._session_factory = session_factory
        self._cache: dict[str, _CacheEntry] = {}
        self._lock = asyncio.Lock()

    def _get_from_env(self, key: str) -> str | None:
        """Get setting value from environment variables.

        Args:
            key: Setting key to look up.

        Returns:
            Value from environment or None.
        """
        env_vars = _SETTING_ENV_VARS.get(key, [f"SIBYL_{key.upper()}"])
        for env_var in env_vars:
            value = os.environ.get(env_var, "").strip()
            if value:
                return value
        return None

    async def get(self, key: str, *, decrypt: bool = True) -> str | None:
        """Get a setting value with DB-first lookup.

        Args:
            key: Setting key to look up.
            decrypt: Whether to decrypt secret values (default True).

        Returns:
            Setting value or None if not found.
        """
        # 1. Check cache
        if key in self._cache and not self._cache[key].is_expired:
            return self._cache[key].value

        # 2. Check database
        async with self._lock:
            # Double-check cache after acquiring lock
            if key in self._cache and not self._cache[key].is_expired:
                return self._cache[key].value

            async with self._session_factory() as session:
                result = await session.execute(
                    select(SystemSetting).where(SystemSetting.key == key)
                )
                setting = result.scalar_one_or_none()

                if setting:
                    value = setting.value
                    # Decrypt if needed
                    if setting.is_secret and decrypt:
                        try:
                            value = decrypt_value(value)
                        except Exception as e:
                            log.warning("Failed to decrypt setting", key=key, error=str(e))
                            value = None

                    self._cache[key] = _CacheEntry(value)
                    return value

        # 3. Fall back to environment
        env_value = self._get_from_env(key)
        if env_value:
            self._cache[key] = _CacheEntry(env_value)
        return env_value

    async def get_with_source(self, key: str) -> tuple[str | None, str]:
        """Get a setting value and its source.

        Args:
            key: Setting key to look up.

        Returns:
            Tuple of (value, source) where source is "database", "environment", or "none".
        """
        # Check database first
        async with self._session_factory() as session:
            result = await session.execute(select(SystemSetting).where(SystemSetting.key == key))
            setting = result.scalar_one_or_none()

            if setting:
                value = setting.value
                if setting.is_secret:
                    try:
                        value = decrypt_value(value)
                    except Exception as e:
                        log.warning(
                            "Failed to decrypt setting for source lookup", key=key, error=str(e)
                        )
                        value = None
                return value, "database"

        # Check environment
        env_value = self._get_from_env(key)
        if env_value:
            return env_value, "environment"

        return None, "none"

    async def set(
        self,
        key: str,
        value: str,
        *,
        is_secret: bool | None = None,
        description: str | None = None,
    ) -> None:
        """Set a setting value in the database.

        Args:
            key: Setting key.
            value: Setting value (will be encrypted if is_secret).
            is_secret: Whether to encrypt the value. Defaults to True for known secrets.
            description: Optional description of the setting.
        """
        # Determine if this should be a secret
        if is_secret is None:
            is_secret = key in _SECRET_SETTINGS

        # Encrypt if needed
        stored_value = encrypt_value(value) if is_secret else value

        async with self._lock:
            async with self._session_factory() as session:
                # Upsert the setting
                result = await session.execute(
                    select(SystemSetting).where(SystemSetting.key == key)
                )
                setting = result.scalar_one_or_none()

                if setting:
                    setting.value = stored_value
                    setting.is_secret = is_secret
                    if description is not None:
                        setting.description = description
                else:
                    setting = SystemSetting(
                        key=key,
                        value=stored_value,
                        is_secret=is_secret,
                        description=description,
                    )
                    session.add(setting)

                await session.commit()

            # Invalidate cache
            self._cache.pop(key, None)

        log.info("Setting updated", key=key, is_secret=is_secret)

    async def delete(self, key: str) -> bool:
        """Delete a setting from the database.

        Args:
            key: Setting key to delete.

        Returns:
            True if setting was deleted, False if it didn't exist.
        """
        async with self._lock, self._session_factory() as session:
            result = await session.execute(select(SystemSetting).where(SystemSetting.key == key))
            setting = result.scalar_one_or_none()

            if setting:
                await session.delete(setting)
                await session.commit()
                self._cache.pop(key, None)
                log.info("Setting deleted", key=key)
                return True

        return False

    async def get_all(self, *, include_secrets: bool = False) -> dict[str, dict]:
        """Get all settings with their metadata.

        Args:
            include_secrets: Whether to include decrypted secret values.

        Returns:
            Dict of key -> {value, source, is_secret, masked_value}
        """
        result: dict[str, dict] = {}

        # Get all DB settings
        async with self._session_factory() as session:
            db_result = await session.execute(select(SystemSetting))
            for setting in db_result.scalars():
                value = None
                if setting.is_secret:
                    if include_secrets:
                        try:
                            value = decrypt_value(setting.value)
                        except Exception as e:
                            log.warning(
                                "Failed to decrypt setting in get_all",
                                key=setting.key,
                                error=str(e),
                            )
                    masked = (
                        mask_secret(decrypt_value(setting.value)) if setting.is_secret else None
                    )
                else:
                    value = setting.value
                    masked = None

                result[setting.key] = {
                    "configured": True,
                    "source": "database",
                    "is_secret": setting.is_secret,
                    "value": value if not setting.is_secret or include_secrets else None,
                    "masked": masked,
                    "description": setting.description,
                }

        # Check known env vars that aren't in DB
        for key in _SETTING_ENV_VARS:
            if key not in result:
                env_value = self._get_from_env(key)
                if env_value:
                    is_secret = key in _SECRET_SETTINGS
                    result[key] = {
                        "configured": True,
                        "source": "environment",
                        "is_secret": is_secret,
                        "value": env_value if not is_secret or include_secrets else None,
                        "masked": mask_secret(env_value) if is_secret else None,
                        "description": None,
                    }
                else:
                    result[key] = {
                        "configured": False,
                        "source": "none",
                        "is_secret": key in _SECRET_SETTINGS,
                        "value": None,
                        "masked": None,
                        "description": None,
                    }

        return result

    def clear_cache(self) -> None:
        """Clear the settings cache."""
        self._cache.clear()

    # Convenience methods for common settings

    async def get_openai_key(self) -> str | None:
        """Get the OpenAI API key."""
        return await self.get("openai_api_key")

    async def get_anthropic_key(self) -> str | None:
        """Get the Anthropic API key."""
        return await self.get("anthropic_api_key")


# Global service instance (initialized lazily)
_settings_service: SettingsService | None = None


def get_settings_service() -> SettingsService:
    """Get the global settings service instance.

    Returns:
        The settings service singleton.
    """
    global _settings_service  # noqa: PLW0603
    if _settings_service is None:
        from sibyl.db.connection import get_session

        _settings_service = SettingsService(get_session)
    return _settings_service


def reset_settings_service() -> None:
    """Reset the global settings service (for testing)."""
    global _settings_service  # noqa: PLW0603
    _settings_service = None


async def load_api_keys_from_db() -> list[str]:
    """Load API keys from database into environment variables.

    Only loads keys that are not already set in the environment.
    This should be called at startup before GraphClient is initialized.

    Returns:
        List of keys that were loaded from the database.
    """
    loaded: list[str] = []
    settings_svc = get_settings_service()

    for setting_key, env_var in [
        ("openai_api_key", "OPENAI_API_KEY"),
        ("anthropic_api_key", "ANTHROPIC_API_KEY"),
    ]:
        try:
            if not os.environ.get(env_var):
                key = await settings_svc.get(setting_key)
                if key:
                    os.environ[env_var] = key
                    loaded.append(setting_key)
                    log.debug(f"Loaded {setting_key} from database settings")
        except Exception as e:
            log.warning(f"Failed to load {setting_key} from database", error=str(e))

    return loaded
