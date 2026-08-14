"""Lightweight local status helpers for `sibyl context --quick`."""

from __future__ import annotations

import json
from time import time

from sibyl_cli.auth_store import credential_scope, read_server_credentials
from sibyl_cli.config_store import (
    get_effective_server_url,
    resolve_effective_context,
    resolve_project_from_cwd,
)


def _auth_status(
    server_url: str,
    *,
    context_name: str | None = None,
    org_slug: str | None = None,
) -> tuple[str, str | None, int | None]:
    api_url = f"{server_url.rstrip('/')}/api"
    scope = credential_scope(context_name, org_slug) if context_name else None
    creds = read_server_credentials(api_url, credential_scope=scope)
    token = str(creds.get("access_token") or "").strip()
    refresh = str(creds.get("refresh_token") or "").strip()
    expires_at = creds.get("access_token_expires_at")

    if not token:
        return ("refresh_needed" if refresh else "missing", None, None)
    if expires_at is None:
        return "valid", None, None

    remaining = max(0, int(int(expires_at) - time()))
    if remaining == 0:
        return "refresh_needed", None, 0
    return "valid", f"expires in {remaining}s", remaining


def quick_context_payload() -> dict[str, object]:
    active = resolve_effective_context()
    linked_project = resolve_project_from_cwd()
    server_url = active.server_url if active else get_effective_server_url()
    effective_project = linked_project or (active.default_project if active else None)
    auth_state, auth_label, auth_expires_in = _auth_status(
        server_url,
        context_name=active.name if active else None,
        org_slug=active.org_slug if active else None,
    )

    payload: dict[str, object] = {
        "server": server_url,
        "org": active.org_slug if active and active.org_slug else "auto",
        "project": effective_project,
        "project_source": "linked"
        if linked_project
        else ("context" if effective_project else None),
        "auth": auth_state,
    }
    if auth_label:
        payload["auth_label"] = auth_label
    if auth_expires_in is not None:
        payload["auth_expires_in"] = auth_expires_in
    return payload


def render_quick_context(payload: dict[str, object], *, json_out: bool) -> None:
    if json_out:
        print(json.dumps(payload, indent=2))
        return

    auth = str(payload["auth"])
    auth_render = {
        "valid": "✓ valid",
        "refresh_needed": "✗ refresh needed",
        "missing": "✗ missing",
    }.get(auth, auth)
    if auth == "valid" and payload.get("auth_label"):
        auth_render = f"✓ {payload['auth_label']}"

    project = payload.get("project")
    project_source = payload.get("project_source")
    project_render = f"{project} ({project_source})" if project and project_source else "not linked"

    print(f"Server:   {payload['server']}")
    print(f"Org:      {payload['org']}")
    print(f"Project:  {project_render}")
    print(f"Auth:     {auth_render}")
