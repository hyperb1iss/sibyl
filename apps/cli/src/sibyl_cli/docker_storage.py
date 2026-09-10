"""Shared volume initialization for the non-root SurrealDB image."""

from typing import Any


def surreal_data_mount() -> dict[str, Any]:
    """Prevent image copy-up from replacing initialized volume ownership."""
    return {
        "type": "volume",
        "source": "sibyl_surreal",
        "target": "/data",
        "volume": {"nocopy": True},
    }


def surreal_volume_initializer() -> dict[str, Any]:
    """Initialize only the mount root; preserve existing database file metadata."""
    return {
        "image": "busybox:1.37",
        "container_name": "sibyl-surreal-init",
        "user": "0:0",
        # The official SurrealDB image uses distroless nonroot (65532:65532).
        "command": ["chown", "65532:65532", "/data"],
        "volumes": [surreal_data_mount()],
        "network_mode": "none",
        "read_only": True,
        "cap_drop": ["ALL"],
        "cap_add": ["CHOWN"],
        "security_opt": ["no-new-privileges:true"],
        "restart": "no",
    }
