"""Tests for retired RLS dependency shims."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from sibyl.auth.rls import (
    apply_rls_from_auth_context,
    get_auth_session,
    get_rls_session,
    require_rls_session,
)


@pytest.mark.asyncio
async def test_get_rls_session_is_unavailable() -> None:
    with pytest.raises(HTTPException) as exc_info:  # noqa: PT012
        gen = get_rls_session(MagicMock())
        await gen.__anext__()

    assert exc_info.value.status_code == 501
    assert "removed after the v0.6.0 compatibility release" in exc_info.value.detail


@pytest.mark.asyncio
async def test_require_rls_session_is_unavailable() -> None:
    with pytest.raises(HTTPException) as exc_info:  # noqa: PT012
        gen = require_rls_session(MagicMock())
        await gen.__anext__()

    assert exc_info.value.status_code == 501
    assert "removed after the v0.6.0 compatibility release" in exc_info.value.detail


@pytest.mark.asyncio
async def test_apply_rls_from_auth_context_is_noop() -> None:
    session = AsyncMock()

    await apply_rls_from_auth_context(session, MagicMock())

    session.execute.assert_not_called()


@pytest.mark.asyncio
async def test_get_auth_session_builds_context_without_relational_session() -> None:
    ctx = MagicMock()

    with patch("sibyl.auth.dependencies.build_auth_context", AsyncMock(return_value=ctx)):
        gen = get_auth_session(MagicMock())
        auth_session = await gen.__anext__()

    assert auth_session.ctx is ctx
    assert auth_session.session is None
