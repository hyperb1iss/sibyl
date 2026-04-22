"""SurrealDB backend foundation for Sibyl's next-form runtime."""

from sibyl_core.backends.surreal.auth_client import SurrealAuthClient
from sibyl_core.backends.surreal.auth_schema import bootstrap_auth_schema
from sibyl_core.backends.surreal.content_client import SurrealContentClient
from sibyl_core.backends.surreal.content_schema import bootstrap_content_schema
from sibyl_core.backends.surreal.driver import SurrealDriver, SurrealDriverSession

__all__ = [
    "SurrealAuthClient",
    "SurrealContentClient",
    "SurrealDriver",
    "SurrealDriverSession",
    "bootstrap_auth_schema",
    "bootstrap_content_schema",
]
