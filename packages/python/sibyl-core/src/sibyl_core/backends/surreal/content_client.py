"""Dedicated SurrealDB client for Sibyl content storage."""

from __future__ import annotations

from sibyl_core.backends.surreal.dedicated_client import DedicatedSurrealClient


class SurrealContentClient(DedicatedSurrealClient):
    """Small wrapper around AsyncSurreal for the shared content namespace."""

    def __init__(
        self,
        *,
        url: str,
        username: str = "",
        password: str = "",
        token: str = "",
        namespace: str = "sibyl_content",
        database: str = "content",
    ) -> None:
        super().__init__(
            url=url,
            username=username,
            password=password,
            token=token,
            namespace=namespace,
            database=database,
        )
