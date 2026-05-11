from uuid import uuid4

import pytest

from sibyl.persistence.surreal import setup as surreal_setup


@pytest.mark.asyncio
async def test_surreal_setup_mode_uses_direct_user_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.closed = False

        async def execute_query(self, query: str):
            self.calls.append(query)
            return []

        async def close(self) -> None:
            self.closed = True

    client = FakeClient()

    monkeypatch.setattr(surreal_setup, "build_surreal_auth_client", lambda: client)
    monkeypatch.setattr(
        surreal_setup.SurrealUserRepository,
        "from_client",
        lambda _client: (_ for _ in ()).throw(AssertionError("unexpected user repository")),
    )

    assert await surreal_setup.is_setup_mode() is True
    assert client.calls == ["SELECT uuid FROM users LIMIT 1;"]
    assert client.closed is True


@pytest.mark.asyncio
async def test_surreal_setup_status_batches_user_and_org_probes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.closed = False

        async def execute_query(self, query: str):
            self.calls.append(query)
            return {
                "users": [{"uuid": str(uuid4())}],
                "organizations": [{"uuid": str(uuid4())}],
            }

        async def close(self) -> None:
            self.closed = True

    client = FakeClient()

    monkeypatch.setattr(surreal_setup, "build_surreal_auth_client", lambda: client)
    monkeypatch.setattr(
        surreal_setup.SurrealUserRepository,
        "from_client",
        lambda _client: (_ for _ in ()).throw(AssertionError("unexpected user repository")),
    )
    monkeypatch.setattr(
        surreal_setup.SurrealOrganizationRepository,
        "from_client",
        lambda _client: (_ for _ in ()).throw(AssertionError("unexpected org repository")),
    )

    status = await surreal_setup.get_setup_status()

    assert status.has_users is True
    assert status.has_orgs is True
    assert len(client.calls) == 1
    assert "RETURN" in client.calls[0]
    assert "FROM users" in client.calls[0]
    assert "FROM organizations" in client.calls[0]
    assert client.closed is True


def test_surreal_setup_exports_neutral_runtime_surface() -> None:
    assert surreal_setup.__all__ == [
        "SetupStatus",
        "SurrealOrganizationRepository",
        "SurrealUserRepository",
        "build_surreal_auth_client",
        "get_setup_status",
        "is_setup_mode",
        "require_settings_admin",
        "require_setup_mode_or_admin",
        "require_setup_mode_or_auth",
    ]
