"""The server must name itself on every response so clients can detect drift."""

from __future__ import annotations

import pytest
from fastapi import FastAPI, HTTPException
from starlette.testclient import TestClient

from sibyl.api.app import VersionHeaderMiddleware, settings as app_settings
from sibyl_core.version_contract import MIN_CLIENT_HEADER, SERVER_VERSION_HEADER


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(VersionHeaderMiddleware)

    @app.get("/ok")
    async def ok() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/denied")
    async def denied() -> dict[str, str]:
        raise HTTPException(status_code=403, detail="nope")

    return app


def test_version_is_stamped_on_successful_responses() -> None:
    from sibyl import __version__

    with TestClient(_app()) as client:
        response = client.get("/ok")

    assert response.headers[SERVER_VERSION_HEADER] == __version__


def test_version_is_stamped_on_rejected_responses() -> None:
    # A client refused by an inner layer still needs to learn the server
    # version, otherwise it cannot tell "too old" from "broken".
    from sibyl import __version__

    with TestClient(_app()) as client:
        response = client.get("/denied")

    assert response.status_code == 403
    assert response.headers[SERVER_VERSION_HEADER] == __version__


def test_no_floor_header_when_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    # Absent means "no floor". Emitting an empty or placeholder value would
    # let a client infer a floor that the operator never set.
    monkeypatch.setattr(app_settings, "minimum_client_version", None)

    with TestClient(_app()) as client:
        response = client.get("/ok")

    assert MIN_CLIENT_HEADER not in response.headers


def test_floor_is_advertised_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_settings, "minimum_client_version", "1.2.0")

    with TestClient(_app()) as client:
        response = client.get("/ok")

    assert response.headers[MIN_CLIENT_HEADER] == "1.2.0"


def test_blank_floor_is_treated_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_settings, "minimum_client_version", "   ")

    with TestClient(_app()) as client:
        response = client.get("/ok")

    assert MIN_CLIENT_HEADER not in response.headers
