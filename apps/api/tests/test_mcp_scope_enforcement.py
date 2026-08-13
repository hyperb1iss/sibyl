"""MCP tools enforce the same API-key scopes REST enforces."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from sibyl.auth.mcp_auth import (
    effective_api_key_scopes,
    insufficient_mcp_scope_message,
    mcp_scopes_allow,
)
from sibyl.auth.mcp_oauth import SibylMcpOAuthProvider
from sibyl.server import (
    McpContext,
    _add_mcp_entity,
    _get_mcp_context,
    _manage_mcp_action,
    _optional_mcp_org_id,
    _remember_mcp_memory,
    _require_mcp_context,
    _synthesis_mcp_draft,
)


def _api_key_ctx(scopes: list[str] | None) -> McpContext:
    return McpContext(
        org_id=str(uuid4()),
        user_id=str(uuid4()),
        scopes=scopes,
        is_api_key=True,
    )


@pytest.mark.parametrize(
    ("scopes", "read_allowed", "write_allowed"),
    [
        (["mcp"], True, True),
        (["mcp", "api:write"], True, True),
        (["mcp", "api:read", "api:write"], True, True),
        (["mcp", "api:read"], True, False),
        (["api:read"], False, False),
        (["api:write"], False, False),
        (["api:read", "api:write"], False, False),
        ([], False, False),
        (None, False, False),
        (["billing:admin"], False, False),
        (["mcp", "billing:admin"], True, False),
        (["mcp", "billing:admin", "api:write"], True, True),
        ([" mcp "], True, True),
        ([" mcp ", " api:read "], True, False),
    ],
)
def test_mcp_scopes_allow_matrix(
    scopes: list[str] | None, read_allowed: bool, write_allowed: bool
) -> None:
    assert mcp_scopes_allow(scopes, write=False) is read_allowed
    assert mcp_scopes_allow(scopes, write=True) is write_allowed


@pytest.mark.parametrize("stored", [[], None, [""], ["  "]])
def test_empty_stored_scopes_resolve_to_the_legacy_shape(stored: list[str] | None) -> None:
    """Keys minted before the scopes column landed act as bare-mcp keys."""
    resolved = effective_api_key_scopes(stored)

    assert resolved == {"mcp"}
    assert mcp_scopes_allow(resolved, write=False) is True
    assert mcp_scopes_allow(resolved, write=True) is True


@pytest.mark.parametrize("stored", [["mcp", "api:read"], ["api:read"], ["billing:admin"]])
def test_stored_scopes_are_left_alone_when_present(stored: list[str]) -> None:
    assert effective_api_key_scopes(stored) == set(stored)


def test_api_key_request_model_refuses_an_empty_scope_list() -> None:
    """The legacy carve-out stays closed to newly minted keys."""
    from pydantic import ValidationError

    from sibyl.api.routes.auth import ApiKeyCreateRequest

    assert ApiKeyCreateRequest(name="default").scopes == ["mcp"]

    for empty in ([], [""], ["   "]):
        with pytest.raises(ValidationError, match="at least one scope"):
            ApiKeyCreateRequest(name="scopeless", scopes=empty)


@pytest.mark.asyncio
@pytest.mark.parametrize("empty", [[], [""], ["   "]])
async def test_api_key_issuer_refuses_an_empty_scope_list(empty: list[str]) -> None:
    """The issuer holds the invariant too, not just the HTTP model above it."""
    from sibyl.persistence.surreal.auth_runtime.api_keys import create_api_key_for_user

    client_scope = AsyncMock()
    with (
        patch(
            "sibyl.persistence.surreal.auth_runtime.api_keys._auth_client_scope",
            client_scope,
        ),
        pytest.raises(ValueError, match="at least one scope"),
    ):
        await create_api_key_for_user(
            organization_id=uuid4(),
            user_id=uuid4(),
            name="scopeless",
            live=True,
            scopes=empty,
            expires_at=None,
            request=None,
        )

    client_scope.assert_not_called()


def test_insufficient_scope_message_names_the_missing_scope() -> None:
    surface = insufficient_mcp_scope_message(["api:read"], write=True)
    assert "Expected mcp" in surface
    assert "api:read" in surface

    granular = insufficient_mcp_scope_message(["mcp", "api:read"], write=True)
    assert "Expected mcp and api:write" in granular

    empty = insufficient_mcp_scope_message([], write=False)
    assert "key has none" in empty


@pytest.mark.asyncio
async def test_require_mcp_context_refuses_write_for_read_only_key() -> None:
    ctx = _api_key_ctx(["mcp", "api:read"])

    with patch("sibyl.server._get_mcp_context", AsyncMock(return_value=ctx)):
        assert await _require_mcp_context() is ctx
        with pytest.raises(ValueError, match="api:write"):
            await _require_mcp_context(write=True)


@pytest.mark.asyncio
async def test_require_mcp_context_allows_write_for_legacy_mcp_key() -> None:
    ctx = _api_key_ctx(["mcp"])

    with patch("sibyl.server._get_mcp_context", AsyncMock(return_value=ctx)):
        assert await _require_mcp_context(write=True) is ctx


@pytest.mark.asyncio
@pytest.mark.parametrize("scopes", [[], None])
async def test_require_mcp_context_keeps_legacy_scopeless_keys_working(
    scopes: list[str] | None,
) -> None:
    ctx = _api_key_ctx(scopes)

    with patch("sibyl.server._get_mcp_context", AsyncMock(return_value=ctx)):
        assert await _require_mcp_context() is ctx
        assert await _require_mcp_context(write=True) is ctx


@pytest.mark.asyncio
async def test_require_mcp_context_allows_write_for_granular_write_key() -> None:
    ctx = _api_key_ctx(["mcp", "api:write"])

    with patch("sibyl.server._get_mcp_context", AsyncMock(return_value=ctx)):
        assert await _require_mcp_context(write=True) is ctx


@pytest.mark.asyncio
@pytest.mark.parametrize("scopes", [["api:read"], ["billing:admin"]])
async def test_require_mcp_context_refuses_keys_without_mcp_scope(
    scopes: list[str] | None,
) -> None:
    ctx = _api_key_ctx(scopes)

    with patch("sibyl.server._get_mcp_context", AsyncMock(return_value=ctx)):
        with pytest.raises(ValueError, match="Expected mcp"):
            await _require_mcp_context()
        with pytest.raises(ValueError, match="Expected mcp"):
            await _require_mcp_context(write=True)


@pytest.mark.asyncio
async def test_require_mcp_context_leaves_user_sessions_ungated() -> None:
    ctx = McpContext(org_id=str(uuid4()), user_id=str(uuid4()), scopes=None)

    with patch("sibyl.server._get_mcp_context", AsyncMock(return_value=ctx)):
        assert await _require_mcp_context(write=True) is ctx


@pytest.mark.asyncio
async def test_remember_refuses_read_only_key_without_writing() -> None:
    ctx = _api_key_ctx(["mcp", "api:read"])
    add = AsyncMock()
    remember_raw = AsyncMock()

    with (
        patch("sibyl.server._get_mcp_context", AsyncMock(return_value=ctx)),
        patch("sibyl_core.tools.core.add", add),
        patch("sibyl_core.services.surreal_content.remember_raw_memory", remember_raw),
        pytest.raises(ValueError, match="api:write"),
    ):
        await _remember_mcp_memory(
            title="Should never land",
            content="A read-only key must not reach the write path.",
            kind="decision",
            domain="sibyl",
            project="project-a",
            tags=None,
            related_to=None,
        )

    add.assert_not_awaited()
    remember_raw.assert_not_awaited()


@pytest.mark.asyncio
async def test_add_refuses_read_only_key_without_writing() -> None:
    ctx = _api_key_ctx(["mcp", "api:read"])
    add = AsyncMock()

    with (
        patch("sibyl.server._get_mcp_context", AsyncMock(return_value=ctx)),
        patch("sibyl_core.tools.core.add", add),
        pytest.raises(ValueError, match="api:write"),
    ):
        await _add_mcp_entity(
            title="Should never land",
            content="A read-only key must not reach the write path.",
            entity_type="task",
            category=None,
            languages=None,
            tags=None,
            related_to=None,
            metadata=None,
            project="project-a",
            priority=None,
            assignees=None,
            due_date=None,
            technologies=None,
            depends_on=None,
            repository_url=None,
        )

    add.assert_not_awaited()


@pytest.mark.asyncio
async def test_manage_refuses_read_only_key_without_writing() -> None:
    ctx = _api_key_ctx(["mcp", "api:read"])
    manage = AsyncMock()

    with (
        patch("sibyl.server._get_mcp_context", AsyncMock(return_value=ctx)),
        patch("sibyl_core.tools.manage.manage", manage),
        pytest.raises(ValueError, match="api:write"),
    ):
        await _manage_mcp_action(action="complete_task", entity_id="task_1", data=None)

    manage.assert_not_awaited()


@pytest.mark.asyncio
async def test_manage_refuses_read_only_key_when_idempotency_key_is_present() -> None:
    ctx = _api_key_ctx(["mcp", "api:read"])
    manage = AsyncMock()

    with (
        patch("sibyl.server._get_mcp_context", AsyncMock(return_value=ctx)),
        patch("sibyl_core.tools.manage.manage", manage),
        patch("sibyl.server.idempotency_lock") as lock,
        pytest.raises(ValueError, match="api:write"),
    ):
        await _manage_mcp_action(
            action="complete_task",
            entity_id="task_1",
            data={"idempotency_key": "key-1"},
        )

    lock.assert_not_called()
    manage.assert_not_awaited()


@pytest.mark.asyncio
async def test_synthesis_draft_gates_only_the_remembering_variant() -> None:
    ctx = _api_key_ctx(["mcp", "api:read"])
    draft = AsyncMock(return_value={"success": True})

    with (
        patch("sibyl.server._get_mcp_context", AsyncMock(return_value=ctx)),
        patch("sibyl.server._resolve_mcp_project_scope", AsyncMock(return_value={"project-a"})),
        patch("sibyl_core.tools.core.synthesis_draft", draft),
    ):
        assert await _synthesis_mcp_draft(goal="ship faster", project="project-a") == {
            "success": True
        }
        with pytest.raises(ValueError, match="api:write"):
            await _synthesis_mcp_draft(goal="ship faster", project="project-a", remember=True)

    draft.assert_awaited_once()


@pytest.mark.asyncio
async def test_health_org_lookup_stays_anonymous_without_a_credential() -> None:
    with patch("sibyl.server._get_mcp_context", AsyncMock(return_value=None)):
        assert await _optional_mcp_org_id() is None


@pytest.mark.asyncio
async def test_health_org_lookup_applies_the_read_gate() -> None:
    allowed = _api_key_ctx(["mcp", "api:read"])
    refused = _api_key_ctx(["billing:admin"])

    with patch("sibyl.server._get_mcp_context", AsyncMock(return_value=allowed)):
        assert await _optional_mcp_org_id() == allowed.org_id

    with (
        patch("sibyl.server._get_mcp_context", AsyncMock(return_value=refused)),
        pytest.raises(ValueError, match="Expected mcp"),
    ):
        await _optional_mcp_org_id()


@pytest.mark.asyncio
@pytest.mark.parametrize("scopes", [["api:read"], ["billing:admin"]])
async def test_oauth_load_access_token_refuses_keys_without_mcp_scope(
    monkeypatch, scopes: list[str] | None
) -> None:
    provider = SibylMcpOAuthProvider()
    auth = SimpleNamespace(api_key_id=uuid4(), scopes=scopes)
    monkeypatch.setattr(provider, "_authenticate_api_key", AsyncMock(return_value=auth))

    assert await provider.load_access_token("sk_live_x") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("scopes", [[], None])
async def test_oauth_load_access_token_admits_legacy_scopeless_keys(
    monkeypatch, scopes: list[str] | None
) -> None:
    provider = SibylMcpOAuthProvider()
    auth = SimpleNamespace(api_key_id=uuid4(), scopes=scopes)
    monkeypatch.setattr(provider, "_authenticate_api_key", AsyncMock(return_value=auth))

    access = await provider.load_access_token("sk_live_x")

    assert access is not None
    assert access.scopes == ["mcp"]


@pytest.mark.asyncio
async def test_oauth_load_access_token_preserves_granular_scopes(monkeypatch) -> None:
    provider = SibylMcpOAuthProvider()
    auth = SimpleNamespace(api_key_id=uuid4(), scopes=["mcp", "api:read"])
    monkeypatch.setattr(provider, "_authenticate_api_key", AsyncMock(return_value=auth))

    access = await provider.load_access_token("sk_live_x")

    assert access is not None
    assert sorted(access.scopes) == ["api:read", "mcp"]


@pytest.mark.asyncio
async def test_get_mcp_context_marks_api_keys_and_not_user_sessions() -> None:
    auth = SimpleNamespace(
        organization_id=uuid4(),
        user_id=uuid4(),
        scopes=["mcp", "api:read"],
        project_ids=None,
    )

    with (
        patch("sibyl.server.get_access_token", return_value=SimpleNamespace(token="sk_live_x")),
        patch("sibyl.server.authenticate_api_key", AsyncMock(return_value=auth)),
    ):
        key_ctx = await _get_mcp_context()

    assert key_ctx is not None
    assert key_ctx.is_api_key is True
    assert key_ctx.scopes == ["mcp", "api:read"]

    with (
        patch("sibyl.server.get_access_token", return_value=SimpleNamespace(token="jwt.token")),
        patch(
            "sibyl.auth.jwt.verify_access_token",
            return_value={"org": str(uuid4()), "sub": str(uuid4()), "scopes": ["mcp"]},
        ),
        patch("sibyl.server.resolve_org_role", AsyncMock(return_value="member")),
    ):
        user_ctx = await _get_mcp_context()

    assert user_ctx is not None
    assert user_ctx.is_api_key is False
