"""OIDC silent-refresh response helpers."""

from __future__ import annotations

from urllib.parse import urlencode

from fastapi import Request
from fastapi.responses import RedirectResponse

from sibyl import config as config_module

SOFT_REFRESH_ERRORS = frozenset(
    {
        "login_required",
        "interaction_required",
        "consent_required",
        "account_selection_required",
    }
)


def is_soft_refresh_error(error: str | None) -> bool:
    return (error or "").strip() in SOFT_REFRESH_ERRORS


def silent_refresh_bounce(request: Request, *, error: str) -> RedirectResponse:
    params = {"error": error}
    next_path = (request.query_params.get("next") or "").strip()
    if next_path.startswith("/") and not next_path.startswith("//"):
        params["next"] = next_path
    base = config_module.settings.frontend_url.rstrip("/")
    return RedirectResponse(url=f"{base}/login?{urlencode(params)}", status_code=302)
