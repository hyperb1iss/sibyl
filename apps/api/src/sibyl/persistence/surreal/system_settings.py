"""Surreal-backed system-setting helpers."""

from __future__ import annotations

from collections.abc import Mapping

from sibyl.persistence.settings_types import SystemSettingRecord
from sibyl.persistence.surreal.content import (
    _coerce_bool,
    _coerce_optional_str,
    _coerce_str,
    surreal_content_client,
)
from sibyl_core.backends.surreal.records import (
    coerce_datetime as _coerce_datetime,
    normalize_records as _normalize_records,
    query_error as _query_error,
    utcnow as _utcnow,
)


def _setting_from_record(record: Mapping[str, object]) -> SystemSettingRecord:
    now = _utcnow()
    return SystemSettingRecord(
        key=_coerce_str(record.get("key")),
        value=_coerce_str(record.get("value")),
        is_secret=_coerce_bool(record.get("is_secret")),
        description=_coerce_optional_str(record.get("description")),
        created_at=_coerce_datetime(record.get("created_at")) or now,
        updated_at=_coerce_datetime(record.get("updated_at")) or now,
    )


def _setting_record(setting: SystemSettingRecord) -> dict[str, object]:
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
    return _normalize_records(result)


async def _execute_write(query: str, **params: object) -> list[dict[str, object]]:
    return await _select_many(query, **params)


async def get_system_setting(
    _session: object,
    *,
    key: str,
) -> SystemSettingRecord | None:
    rows = await _select_many(
        "SELECT * FROM system_settings WHERE key = $key LIMIT 1;",
        key=key,
    )
    return _setting_from_record(rows[0]) if rows else None


# Keys under this prefix are bookkeeping the server keeps for itself, not
# settings an admin manages, so listings leave them out.
INTERNAL_SETTING_PREFIX = "internal."


async def list_system_settings(_session: object) -> list[SystemSettingRecord]:
    rows = await _select_many("SELECT * FROM system_settings;")
    settings = [
        _setting_from_record(row)
        for row in rows
        if not _coerce_str(row.get("key")).startswith(INTERNAL_SETTING_PREFIX)
    ]
    return sorted(settings, key=lambda setting: setting.key)


def _is_duplicate_key(error: Exception) -> bool:
    lowered = str(error).lower()
    return "already contains" in lowered or "unique" in lowered


async def claim_system_setting(
    _session: object,
    *,
    setting: SystemSettingRecord,
) -> SystemSettingRecord:
    """Store ``setting`` unless its key exists, and return whichever row is stored.

    The unique index on ``key`` decides concurrent claims: one CREATE wins
    and every other caller reads the winner's row.
    """
    existing = await get_system_setting(None, key=setting.key)
    if existing is not None:
        return existing
    now = _utcnow()
    setting.created_at = setting.created_at or now
    setting.updated_at = now
    try:
        rows = await _execute_write(
            "CREATE system_settings CONTENT $record;", record=_setting_record(setting)
        )
    except Exception as exc:
        # A lost race surfaces as the SDK's own error (InternalError on a
        # server) or as a statement error; either way the winner's row is
        # there to read.
        if not _is_duplicate_key(exc):
            raise
        rows = []
    if rows:
        return _setting_from_record(rows[0])
    stored = await get_system_setting(None, key=setting.key)
    if stored is None:
        msg = f"Failed to claim system setting {setting.key!r}"
        raise RuntimeError(msg)
    return stored


async def save_system_setting(
    _session: object,
    *,
    setting: SystemSettingRecord,
) -> SystemSettingRecord:
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
