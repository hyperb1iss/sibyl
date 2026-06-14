from __future__ import annotations

from collections.abc import Mapping

import pytest

from sibyl.api.routes import crawler as crawler_routes
from sibyl.crawler import discovery as discovery_module, service as crawler_service
from sibyl_core.network import SafeFetchResponse


class _FakeRoute:
    def __init__(self) -> None:
        self.aborted = False
        self.fulfilled: dict[str, object] | None = None

    async def abort(self) -> None:
        self.aborted = True

    async def fulfill(
        self,
        *,
        status: int,
        headers: Mapping[str, str],
        body: bytes,
    ) -> None:
        self.fulfilled = {"status": status, "headers": dict(headers), "body": body}


class _FakeRequest:
    def __init__(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.url = url
        self.method = method
        self.headers = dict(headers or {})


@pytest.mark.asyncio
async def test_preview_url_uses_safe_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, int]] = []

    async def fake_safe_fetch(url: str, **kwargs: object) -> SafeFetchResponse:
        seen.append((url, int(kwargs["max_bytes"])))
        return SafeFetchResponse(
            url=url,
            status_code=200,
            headers={"content-type": "text/html"},
            body=b"<html><title>Sibyl Docs | Example</title></html>",
        )

    monkeypatch.setattr(crawler_routes, "safe_fetch", fake_safe_fetch)

    result = await crawler_routes.preview_url("https://docs.example.com/start")

    assert seen == [("https://docs.example.com/start", 50_000)]
    assert result["title"] == "Sibyl Docs | Example"
    assert result["suggested_name"] == "Sibyl Docs"


@pytest.mark.asyncio
async def test_discovery_uses_safe_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    async def fake_safe_fetch(url: str, **kwargs: object) -> SafeFetchResponse:
        seen.append(url)
        del kwargs
        return SafeFetchResponse(
            url=url,
            status_code=200,
            headers={"content-type": "text/plain"},
            body=b"# Docs\n\n- [Guide](/guide)\n- [API](https://docs.example.com/api)\n",
        )

    monkeypatch.setattr(discovery_module, "safe_fetch", fake_safe_fetch)

    async with discovery_module.DiscoveryService() as discovery:
        result = await discovery.discover("https://docs.example.com/path")

    assert result is not None
    assert seen == ["https://docs.example.com/llms.txt"]
    assert result.links == [
        "https://docs.example.com/guide",
        "https://docs.example.com/api",
    ]


@pytest.mark.asyncio
async def test_fetch_favicon_uses_safe_fetch_head(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, str]] = []

    async def fake_safe_fetch(url: str, **kwargs: object) -> SafeFetchResponse:
        method = str(kwargs.get("method") or "GET")
        seen.append((method, url))
        headers: Mapping[str, str] = {}
        status_code = 404
        if url.endswith("/favicon.png"):
            headers = {"content-type": "image/png"}
            status_code = 200
        return SafeFetchResponse(
            url=url,
            status_code=status_code,
            headers=headers,
            body=b"",
        )

    monkeypatch.setattr(crawler_service, "safe_fetch", fake_safe_fetch)

    favicon = await crawler_service.CrawlerService().fetch_favicon("https://docs.example.com")

    assert favicon == "https://docs.example.com/favicon.png"
    assert seen == [
        ("HEAD", "https://docs.example.com/favicon.ico"),
        ("HEAD", "https://docs.example.com/favicon.png"),
    ]


@pytest.mark.asyncio
async def test_fetch_favicon_rejects_private_html_icon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_safe_fetch(url: str, **kwargs: object) -> SafeFetchResponse:
        method = str(kwargs.get("method") or "GET")
        if method == "HEAD":
            return SafeFetchResponse(url=url, status_code=404, headers={}, body=b"")
        return SafeFetchResponse(
            url=url,
            status_code=200,
            headers={"content-type": "text/html"},
            body=b'<link rel="icon" href="http://127.0.0.1/favicon.ico">',
        )

    monkeypatch.setattr(crawler_service, "safe_fetch", fake_safe_fetch)

    favicon = await crawler_service.CrawlerService().fetch_favicon("https://docs.example.com")

    assert favicon is None


@pytest.mark.asyncio
async def test_safe_browser_route_fulfills_from_safe_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, str, bool]] = []

    async def fake_safe_fetch(url: str, **kwargs: object) -> SafeFetchResponse:
        seen.append((url, str(kwargs["method"]), bool(kwargs["follow_redirects"])))
        return SafeFetchResponse(
            url=url,
            status_code=200,
            headers={
                "content-type": "text/html",
                "content-length": "999",
            },
            body=b"<main>ok</main>",
        )

    monkeypatch.setattr(crawler_service, "safe_fetch", fake_safe_fetch)
    route = _FakeRoute()
    request = _FakeRequest("https://docs.example.com", headers={"accept": "text/html"})

    await crawler_service._safe_browser_route(route, request)

    assert seen == [("https://docs.example.com", "GET", False)]
    assert route.aborted is False
    assert route.fulfilled == {
        "status": 200,
        "headers": {"content-type": "text/html"},
        "body": b"<main>ok</main>",
    }


@pytest.mark.asyncio
async def test_safe_browser_route_aborts_blocked_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_safe_fetch(url: str, **kwargs: object) -> SafeFetchResponse:
        del url, kwargs
        raise ValueError("URL host is private")

    monkeypatch.setattr(crawler_service, "safe_fetch", fake_safe_fetch)
    route = _FakeRoute()
    request = _FakeRequest("http://169.254.169.254/latest/meta-data")

    await crawler_service._safe_browser_route(route, request)

    assert route.aborted is True
    assert route.fulfilled is None
