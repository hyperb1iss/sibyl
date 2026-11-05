"""The SurrealDB server release Sibyl supports.

Sibyl supports one server release at a time. Code that must name it imports
it from here; the deployment surfaces that cannot import (compose files, the
Helm chart, the Ansible role, CI workflows) carry it as a literal image tag,
and `tools/tests/test_surreal_server_pins.py` holds every one of them to this
value.
"""

SURREAL_SERVER_VERSION = "4.0.0"
SURREAL_SERVER_IMAGE = f"surrealdb/surrealdb:v{SURREAL_SERVER_VERSION}"
# What the server's version RPC answers for that release, before any
# `+build` suffix.
SURREAL_SERVER_VERSION_STRING = f"surrealdb-{SURREAL_SERVER_VERSION}"


# Storage settings every SurrealDB server Sibyl starts carries. The same
# parity test holds each deployment surface's server environment to this.
# PENDING-VLOG-DECISION: the 4.0 disk lane confirms or replaces this setting.
SURREAL_STORAGE_ENV: dict[str, str] = {"SURREAL_SURREALKV_ENABLE_VLOG": "false"}


def is_supported_server_version(reported: str) -> bool:
    """Whether a server's reported version is exactly the supported release."""
    return reported.split("+", 1)[0] == SURREAL_SERVER_VERSION_STRING
