from __future__ import annotations

from uuid import uuid4

from typer.testing import CliRunner

from sibyl.cli import bootstrap as bootstrap_cli

runner = CliRunner()


def test_bootstrap_org_cli_passes_normalized_owner_and_secret(monkeypatch) -> None:
    seen: dict[str, object] = {}
    org_id = uuid4()
    owner_id = uuid4()
    space_id = uuid4()

    async def bootstrap_org_records(**kwargs):
        seen.update(kwargs)
        return bootstrap_cli.BootstrapOrgResult(
            organization_id=org_id,
            organization_slug="acme",
            organization_created=True,
            owner_user_id=owner_id,
            owner_created=True,
            memory_space_id=space_id,
            memory_space_created=True,
        )

    monkeypatch.setenv("SIBYL_BOOTSTRAP_OWNER_PASSWORD", "secret-value")
    monkeypatch.setattr(bootstrap_cli, "bootstrap_org_records", bootstrap_org_records)

    result = runner.invoke(
        bootstrap_cli.app,
        [
            "--name",
            "Acme",
            "--owner-email",
            "OWNER@EXAMPLE.COM",
            "--memory-space-name",
            "Team Memory",
        ],
    )

    assert result.exit_code == 0
    assert seen["organization_name"] == "Acme"
    assert seen["owner_email"] == "owner@example.com"
    assert seen["owner_password"] == "secret-value"
    assert seen["memory_space_name"] == "Team Memory"
    assert "Organization acme created" in result.output


def test_password_from_env_returns_none_for_missing_or_blank(monkeypatch) -> None:
    monkeypatch.delenv("SIBYL_BOOTSTRAP_OWNER_PASSWORD", raising=False)
    assert bootstrap_cli._password_from_env("SIBYL_BOOTSTRAP_OWNER_PASSWORD") is None

    monkeypatch.setenv("SIBYL_BOOTSTRAP_OWNER_PASSWORD", "   ")
    assert bootstrap_cli._password_from_env("SIBYL_BOOTSTRAP_OWNER_PASSWORD") is None

    monkeypatch.setenv("SIBYL_BOOTSTRAP_OWNER_PASSWORD", "present")
    assert bootstrap_cli._password_from_env("SIBYL_BOOTSTRAP_OWNER_PASSWORD") == "present"


async def test_ensure_memory_space_is_idempotent() -> None:
    organization_id = uuid4()
    owner_user_id = uuid4()
    writes: list[dict[str, object]] = []

    class FakeClient:
        async def execute_query(self, query: str, **kwargs):
            if query.strip().startswith("SELECT"):
                if writes:
                    return [{"uuid": writes[0]["uuid"]}]
                return []
            writes.append(kwargs["record"])
            return [kwargs["record"]]

    client = FakeClient()

    first_id, first_created = await bootstrap_cli._ensure_memory_space(
        client,
        organization_id=organization_id,
        owner_user_id=owner_user_id,
        memory_scope="private",
        scope_key=None,
        name="Default memory",
    )
    second_id, second_created = await bootstrap_cli._ensure_memory_space(
        client,
        organization_id=organization_id,
        owner_user_id=owner_user_id,
        memory_scope="private",
        scope_key=None,
        name="Default memory",
    )

    assert first_created is True
    assert second_created is False
    assert second_id == first_id
    assert writes[0]["organization_id"] == str(organization_id)
    assert writes[0]["scope_key"] == str(owner_user_id)
