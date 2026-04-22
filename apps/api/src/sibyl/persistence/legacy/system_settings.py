"""Legacy system-setting adapters backed by the relational runtime."""

from __future__ import annotations

from typing import Any

from sqlmodel import select

from sibyl.db.models import SystemSetting


async def get_system_setting(
    session: Any,
    *,
    key: str,
) -> SystemSetting | None:
    result = await session.execute(select(SystemSetting).where(SystemSetting.key == key))
    return result.scalar_one_or_none()


async def list_system_settings(session: Any) -> list[SystemSetting]:
    result = await session.execute(select(SystemSetting))
    return list(result.scalars())


async def save_system_setting(
    session: Any,
    *,
    setting: SystemSetting,
) -> SystemSetting:
    existing = await get_system_setting(session, key=setting.key)
    if existing is None:
        session.add(setting)
        await session.flush()
        await session.refresh(setting)
        return setting

    existing.value = setting.value
    existing.is_secret = setting.is_secret
    existing.description = setting.description
    await session.flush()
    await session.refresh(existing)
    return existing


async def delete_system_setting(
    session: Any,
    *,
    key: str,
) -> bool:
    setting = await get_system_setting(session, key=key)
    if setting is None:
        return False
    await session.delete(setting)
    return True
