from __future__ import annotations

import gzip
import importlib

import pytest

from sibyl_core.network import SAFE_FETCH_MAX_BYTES
from sibyl_core.network.safe_fetch import (
    SafeFetchResponse,
    decode_safe_fetch_body,
    normalize_safe_url,
    safe_fetch,
)

safe_fetch_module = importlib.import_module("sibyl_core.network.safe_fetch")


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:3337/docs",
        "http://169.254.169.254/latest/meta-data",
        "http://2130706433/docs",
        "http://0x7f000001/docs",
        "http://017700000001/docs",
        "http://[::1]/docs",
    ],
)
def test_normalize_safe_url_rejects_private_hosts(url: str) -> None:
    with pytest.raises(ValueError, match="private"):
        normalize_safe_url(url)


@pytest.mark.asyncio
async def test_safe_fetch_blocks_redirect_to_private_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_fetch_once(url: str, **kwargs: object) -> SafeFetchResponse:
        assert kwargs["allow_private_network"] is False
        return SafeFetchResponse(
            url=url,
            status_code=302,
            headers={"location": "http://169.254.169.254/latest/meta-data"},
            body=b"",
        )

    monkeypatch.setattr(
        safe_fetch_module,
        "_resolve_host_addresses",
        lambda host: ["93.184.216.34"] if host == "docs.example.com" else [],
    )
    monkeypatch.setattr(safe_fetch_module, "_fetch_once", fake_fetch_once)

    with pytest.raises(ValueError, match="private"):
        await safe_fetch("https://docs.example.com/start")


@pytest.mark.asyncio
async def test_safe_fetch_can_return_redirect_without_following(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_fetch_once(url: str, **kwargs: object) -> SafeFetchResponse:
        del kwargs
        return SafeFetchResponse(
            url=url,
            status_code=302,
            headers={"location": "http://169.254.169.254/latest/meta-data"},
            body=b"",
        )

    monkeypatch.setattr(
        safe_fetch_module,
        "_resolve_host_addresses",
        lambda host: ["93.184.216.34"] if host == "docs.example.com" else [],
    )
    monkeypatch.setattr(safe_fetch_module, "_fetch_once", fake_fetch_once)

    page = await safe_fetch("https://docs.example.com/start", follow_redirects=False)

    assert page.status_code == 302
    assert page.headers["location"] == "http://169.254.169.254/latest/meta-data"


@pytest.mark.asyncio
async def test_safe_fetch_pins_validated_public_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, str]] = []

    async def fake_http1_fetch(**kwargs: object) -> SafeFetchResponse:
        seen.append((str(kwargs["url"]), str(kwargs["connect_host"])))
        return SafeFetchResponse(
            url=str(kwargs["url"]),
            status_code=200,
            headers={"content-type": "text/plain"},
            body=b"ok",
        )

    monkeypatch.setattr(
        safe_fetch_module,
        "_resolve_host_addresses",
        lambda host: ["93.184.216.34"] if host == "docs.example.com" else [],
    )
    monkeypatch.setattr(safe_fetch_module, "_http1_fetch", fake_http1_fetch)

    page = await safe_fetch("https://docs.example.com/start")

    assert page.body == b"ok"
    assert seen == [("https://docs.example.com/start", "93.184.216.34")]


def test_decode_safe_fetch_body_rejects_oversized_compressed_body() -> None:
    body = gzip.compress(b"x" * (SAFE_FETCH_MAX_BYTES + 1))

    with pytest.raises(ValueError, match="too large"):
        decode_safe_fetch_body(body, {"content-encoding": "gzip"})
