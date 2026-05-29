"""Active system-setting adapters, re-exported from the SurrealDB backend."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from sibyl.persistence.surreal.system_settings import (
    delete_system_setting,
    get_system_setting,
    list_system_settings,
    save_system_setting,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

__all__ = [
    "delete_system_setting",
    "get_settings_session",
    "get_system_setting",
    "list_system_settings",
    "save_system_setting",
]


@asynccontextmanager
async def get_settings_session() -> AsyncGenerator[object | None]:
    yield None
