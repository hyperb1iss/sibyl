"""Surreal-backed system-setting helpers."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import cast

from sibyl.db.models import SystemSetting
from sibyl.persistence.surreal.content import (
    _coerce_bool,
    _coerce_datetime,
    _coerce_optional_str,
    _coerce_str,
    _normalize_records,
    _query_error,
    surreal_content_client,
)


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _setting_from_record(record: Mapping[str, object]) -> SystemSetting:
    now = _utcnow()
    return SystemSetting(
        key=_coerce_str(record.get("key")),
        value=_coerce_str(record.get("value")),
        is_secret=_coerce_bool(record.get("is_secret")),
        description=_coerce_optional_str(record.get("description")),
        created_at=_coerce_datetime(record.get("created_at")) or now,
        updated_at=_coerce_datetime(record.get("updated_at")) or now,
    )


def _setting_record(setting: SystemSetting) -> dict[str, object]:
    return {
        "key": setting.key,
        "value": setting.value,
        "is_secret": setting.is_secret,
        "description": setting.description,
        "created_at": setting.created_at,
        "updated_at": setting.updated_at,
    }


async def _select_many(query: str, **params: object) -> list[dict[str, object]]:
    async with surreal_content_client() as client:
        result = await client.execute_query(query, **params)

    error = _query_error(result)
    if error is not None:
        raise RuntimeError(error)
    return cast("list[dict[str, object]]", _normalize_records(result))


async def _execute_write(query: str, **params: object) -> list[dict[str, object]]:
    return await _select_many(query, **params)


async def get_system_setting(
    _session: object,
    *,
    key: str,
) -> SystemSetting | None:
    rows = await _select_many(
        "SELECT * FROM system_settings WHERE key = $key LIMIT 1;",
        key=key,
    )
    return _setting_from_record(rows[0]) if rows else None


async def list_system_settings(_session: object) -> list[SystemSetting]:
    rows = await _select_many("SELECT * FROM system_settings;")
    settings = [_setting_from_record(row) for row in rows]
    return sorted(settings, key=lambda setting: setting.key)


async def save_system_setting(
    _session: object,
    *,
    setting: SystemSetting,
) -> SystemSetting:
    existing = await get_system_setting(None, key=setting.key)
    now = _utcnow()
    if existing is None and setting.created_at is None:
        setting.created_at = now
    setting.updated_at = now

    rows = await _execute_write(
        "UPSERT system_settings CONTENT $record WHERE key = $key;",
        key=setting.key,
        record=_setting_record(setting),
    )
    if not rows:
        msg = f"Failed to write system setting {setting.key!r}"
        raise RuntimeError(msg)
    return _setting_from_record(rows[0])


async def delete_system_setting(
    _session: object,
    *,
    key: str,
) -> bool:
    existing = await get_system_setting(None, key=key)
    if existing is None:
        return False
    await _execute_write("DELETE FROM system_settings WHERE key = $key;", key=key)
    return True
