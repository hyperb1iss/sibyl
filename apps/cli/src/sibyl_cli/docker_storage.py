"""SurrealDB service pieces shared by the CLI's compose runtimes."""

from typing import Any

# The SurrealDB server this CLI release runs. Both runtimes reference it through
# SIBYL_SURREAL_IMAGE, so an operator can pin another image without editing the
# compose file, and `sibyl docker upgrade` moves older defaults up to it.
SURREAL_IMAGE = "surrealdb/surrealdb:v3.2.4"
SURREAL_IMAGE_REFERENCE = f"${{SIBYL_SURREAL_IMAGE:-{SURREAL_IMAGE}}}"


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
