"""Dedicated SurrealDB client for Sibyl auth storage."""

from __future__ import annotations

from sibyl_core.backends.surreal.dedicated_client import DedicatedSurrealClient


class SurrealAuthClient(DedicatedSurrealClient):
    """Small wrapper around AsyncSurreal for the shared auth namespace."""

    def __init__(
        self,
        *,
        url: str,
        username: str = "",
        password: str = "",
        token: str = "",
        namespace: str = "sibyl_auth",
        database: str = "auth",
        pool_size: int | None = None,
    ) -> None:
        super().__init__(
            url=url,
            username=username,
            password=password,
            token=token,
            namespace=namespace,
            database=database,
            client_kind="auth",
            pool_size=pool_size,
        )
