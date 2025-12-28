import pytest

from sibyl.cli.client import SibylClient


@pytest.mark.asyncio
async def test_cli_create_org_payload() -> None:
    client = SibylClient(base_url="http://example.test", auth_token="t")  # noqa: S106
    seen = {}

    async def _request(method, path, json=None, params=None):
        seen["method"] = method
        seen["path"] = path
        seen["json"] = json
        return {"ok": True}

    client._request = _request  # type: ignore[method-assign]

    await client.create_org(name="X", slug=None)
    assert seen["json"] == {"name": "X"}
