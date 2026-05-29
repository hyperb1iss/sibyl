"""SurrealDB backend foundation for Sibyl's next-form runtime."""

from importlib import import_module
from typing import Any

_EXPORTS = {
    "SurrealAuthClient": ("sibyl_core.backends.surreal.auth_client", "SurrealAuthClient"),
    "SurrealContentClient": ("sibyl_core.backends.surreal.content_client", "SurrealContentClient"),
    "SurrealQueryError": ("sibyl_core.backends.surreal.connection", "SurrealQueryError"),
    "bootstrap_auth_schema": ("sibyl_core.backends.surreal.auth_schema", "bootstrap_auth_schema"),
    "bootstrap_content_schema": (
        "sibyl_core.backends.surreal.content_schema",
        "bootstrap_content_schema",
    ),
    "coerce_datetime": ("sibyl_core.backends.surreal.records", "coerce_datetime"),
    "coerce_uuid": ("sibyl_core.backends.surreal.records", "coerce_uuid"),
    "normalize_record": ("sibyl_core.backends.surreal.records", "normalize_record"),
    "normalize_records": ("sibyl_core.backends.surreal.records", "normalize_records"),
    "query_error": ("sibyl_core.backends.surreal.records", "query_error"),
    "raise_on_error": ("sibyl_core.backends.surreal.records", "raise_on_error"),
    "utcnow": ("sibyl_core.backends.surreal.records", "utcnow"),
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    try:
        module_name, attr_name = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(name) from exc
    module = import_module(module_name)
    value = getattr(module, attr_name)
    globals()[name] = value
    return value
