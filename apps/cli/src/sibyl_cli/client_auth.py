"""Authentication, organization, team, and access-control client operations."""

from typing import Any
from urllib.parse import quote

import httpx

from sibyl_cli.auth_store import (
    auth_file_lock,
    get_refresh_token,
    is_access_token_expired,
    read_server_credentials,
    set_tokens,
)
from sibyl_cli.client_transport import _is_refresh_revoked


class ClientAuthMixin:
    """Authentication, organization, team, and access-control client operations."""

    async def _silent_local_relogin(self, creds: dict[str, Any]) -> tuple[bool, str | None]:
        email = str(creds.get("local_login_email") or "").strip()
        password = str(creds.get("local_login_password") or "").strip()
        if not email or not password:
            return False, "No stored local login credentials are available."

        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
                verify=not self.insecure,
            ) as client:
                response = await client.post(
                    "/auth/local/login",
                    json={"email": email, "password": password},
                )
                if response.status_code != 200:
                    return False, f"Local login returned HTTP {response.status_code}."
                data = response.json()
        except Exception as exc:
            return False, f"Local login failed: {exc}"

        new_access_token = str(data.get("access_token") or "").strip()
        if not new_access_token:
            return False, "Local login response did not include an access token."

        set_tokens(
            self.base_url,
            new_access_token,
            refresh_token=str(data.get("refresh_token") or "").strip() or None,
            expires_in=int(data["expires_in"]) if data.get("expires_in") else None,
            lock=False,
            credential_scope=self.credential_scope,
        )
        self.auth_token = new_access_token
        if self._client and not self._client.is_closed:
            await self._client.aclose()
        self._client = None
        return True, None

    async def _refresh_token(self) -> tuple[bool, str | None]:
        """Attempt to refresh the access token using stored refresh token.

        Returns:
            Tuple of (success, failure_reason)
        """
        if not self._uses_stored_auth:
            return False, "Automatic renewal is only available for stored CLI login tokens."

        try:
            with auth_file_lock():
                creds = read_server_credentials(
                    self.base_url,
                    credential_scope=self.credential_scope,
                )
                stored_access_token = str(creds.get("access_token") or "").strip()
                expires_at = creds.get("access_token_expires_at")
                if (
                    stored_access_token
                    and stored_access_token != self.auth_token
                    and not is_access_token_expired(
                        self.base_url,
                        credential_scope=self.credential_scope,
                    )
                ):
                    self.auth_token = stored_access_token
                    if self._client and not self._client.is_closed:
                        await self._client.aclose()
                    self._client = None
                    return True, None

                refresh_token = get_refresh_token(
                    self.base_url,
                    credential_scope=self.credential_scope,
                )
                if not refresh_token:
                    return False, "No refresh token is available for automatic renewal."

                if (
                    stored_access_token
                    and expires_at is None
                    and stored_access_token != self.auth_token
                ):
                    self.auth_token = stored_access_token
                    if self._client and not self._client.is_closed:
                        await self._client.aclose()
                    self._client = None
                    return True, None

                async with httpx.AsyncClient(
                    base_url=self.base_url,
                    timeout=self.timeout,
                    verify=not self.insecure,
                ) as client:
                    response = await client.post(
                        "/auth/refresh",
                        json={"refresh_token": refresh_token},
                    )

                    if response.status_code != 200:
                        try:
                            detail = response.json().get("detail")
                        except Exception:
                            detail = response.text
                        detail_text = str(detail).strip() if detail is not None else ""
                        if not detail_text:
                            detail_text = f"Refresh request returned HTTP {response.status_code}."
                        if _is_refresh_revoked(detail_text):
                            relogged, relogin_failure = await self._silent_local_relogin(creds)
                            if relogged:
                                return True, None
                            if relogin_failure:
                                detail_text = (
                                    f"{detail_text} Silent re-login failed: {relogin_failure}"
                                )
                        return False, detail_text

                    data = response.json()
                    new_access_token = data.get("access_token")
                    new_refresh_token = data.get("refresh_token")
                    expires_in = data.get("expires_in")

                    if not new_access_token:
                        return False, "Refresh response did not include a new access token."

                    set_tokens(
                        self.base_url,
                        new_access_token,
                        refresh_token=new_refresh_token,
                        expires_in=expires_in,
                        lock=False,
                        credential_scope=self.credential_scope,
                    )

                    self.auth_token = new_access_token

                    if self._client and not self._client.is_closed:
                        await self._client.aclose()
                    self._client = None

                    return True, None

        except Exception as exc:
            return False, f"Refresh request failed: {exc}"

    async def list_api_keys(self) -> dict[str, Any]:
        return await self._request("GET", "/auth/api-keys")

    async def create_api_key(
        self,
        *,
        name: str,
        live: bool = True,
        scopes: list[str] | None = None,
        project_ids: list[str] | None = None,
        memory_space_ids: list[str] | None = None,
        expires_days: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"name": name, "live": live}
        if scopes is not None:
            payload["scopes"] = scopes
        if project_ids is not None:
            payload["project_ids"] = project_ids
        if memory_space_ids is not None:
            payload["memory_space_ids"] = memory_space_ids
        if expires_days is not None:
            payload["expires_days"] = expires_days
        return await self._request("POST", "/auth/api-keys", json=payload)

    async def revoke_api_key(self, api_key_id: str) -> dict[str, Any]:
        return await self._request("POST", f"/auth/api-keys/{api_key_id}/revoke")

    async def local_signup(
        self,
        *,
        email: str,
        password: str,
        name: str,
        redirect: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"email": email, "password": password, "name": name}
        if redirect is not None:
            payload["redirect"] = redirect
        return await self._request("POST", "/auth/local/signup", json=payload)

    async def local_login(
        self,
        *,
        email: str,
        password: str,
        break_glass_reason: str | None = None,
        redirect: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"email": email, "password": password}
        if break_glass_reason is not None:
            payload["break_glass_reason"] = break_glass_reason
        if redirect is not None:
            payload["redirect"] = redirect
        return await self._request("POST", "/auth/local/login", json=payload)

    async def list_orgs(self) -> dict[str, Any]:
        return await self._request("GET", "/orgs")

    async def create_org(self, name: str, slug: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"name": name}
        if slug:
            payload["slug"] = slug
        return await self._request("POST", "/orgs", json=payload)

    async def switch_org(self, slug: str) -> dict[str, Any]:
        return await self._request("POST", f"/orgs/{slug}/switch")

    async def list_org_members(self, slug: str) -> dict[str, Any]:
        """List all members of an organization."""
        return await self._request("GET", f"/orgs/{slug}/members")

    async def add_org_member(self, slug: str, user_id: str, role: str = "member") -> dict[str, Any]:
        """Add a member to an organization."""
        return await self._request(
            "POST", f"/orgs/{slug}/members", json={"user_id": user_id, "role": role}
        )

    async def update_org_member_role(self, slug: str, user_id: str, role: str) -> dict[str, Any]:
        """Update a member's role in an organization."""
        return await self._request("PATCH", f"/orgs/{slug}/members/{user_id}", json={"role": role})

    async def remove_org_member(self, slug: str, user_id: str) -> dict[str, Any]:
        """Remove a member from an organization."""
        return await self._request("DELETE", f"/orgs/{slug}/members/{user_id}")

    async def preview_memory_space_access(
        self,
        *,
        space_id: str,
        target_principal_type: str,
        target_principal_id: str,
        additional_space_ids: list[str] | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Preview effective recall for a memory-space principal."""
        data: dict[str, Any] = {
            "target_principal_type": target_principal_type,
            "target_principal_id": target_principal_id,
            "additional_space_ids": additional_space_ids or [],
            "limit": limit,
        }
        return await self._request(
            "POST",
            f"/memory/spaces/{quote(space_id, safe='')}/members/preview",
            json=data,
        )

    async def list_teams(self) -> dict[str, Any]:
        """List teams for the active organization."""
        return await self._request("GET", "/teams")

    async def create_team(
        self,
        *,
        name: str,
        slug: str | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        """Create a team and its team memory space."""
        data: dict[str, Any] = {"name": name}
        if slug:
            data["slug"] = slug
        if description:
            data["description"] = description
        return await self._request("POST", "/teams", json=data)

    async def add_team_member(
        self,
        *,
        team_id: str,
        user_id: str,
        role: str = "member",
    ) -> dict[str, Any]:
        """Add or update a team member."""
        return await self._request(
            "POST",
            f"/teams/{quote(team_id, safe='')}/members",
            json={"user_id": user_id, "role": role},
        )

    async def remove_team_member(
        self,
        *,
        team_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        """Remove a team member."""
        return await self._request(
            "DELETE",
            f"/teams/{quote(team_id, safe='')}/members/{quote(user_id, safe='')}",
        )

    async def link_team_project(
        self,
        *,
        team_id: str,
        project_id: str,
        role: str = "project_contributor",
    ) -> dict[str, Any]:
        """Grant a team a project role."""
        return await self._request(
            "POST",
            f"/teams/{quote(team_id, safe='')}/projects",
            json={"project_id": project_id, "role": role},
        )

    async def unlink_team_project(
        self,
        *,
        team_id: str,
        project_id: str,
    ) -> dict[str, Any]:
        """Remove a team project grant."""
        return await self._request(
            "DELETE",
            f"/teams/{quote(team_id, safe='')}/projects/{quote(project_id, safe='')}",
        )
