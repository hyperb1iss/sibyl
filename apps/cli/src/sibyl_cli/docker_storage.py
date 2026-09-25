"""SurrealDB service pieces shared by the CLI's compose runtimes."""

import re
from typing import Any

from packaging.version import InvalidVersion, Version

# The SurrealDB server this CLI release runs. Both runtimes reference it through
# SIBYL_SURREAL_IMAGE, so an operator can pin another image without editing the
# compose file, and both upgrade commands move older defaults up to it.
SURREAL_IMAGE = "surrealdb/surrealdb:v3.2.4"
SURREAL_IMAGE_REFERENCE = f"${{SIBYL_SURREAL_IMAGE:-{SURREAL_IMAGE}}}"
MANAGED_SURREAL_IMAGE = re.compile(r"\$\{SIBYL_SURREAL_IMAGE:-surrealdb/surrealdb:(?P<tag>[^}]+)\}")


def surreal_version(tag: str) -> Version | None:
    try:
        return Version(tag.removeprefix("v"))
    except InvalidVersion:
        return None


def upgraded_surreal_image(image: str) -> str | None:
    """The SurrealDB image an upgrade writes in place of `image`, or None to keep it.

    Only a CLI-written `${SIBYL_SURREAL_IMAGE:-...}` default at or below the
    server this CLI runs moves, so an upgrade never takes SurrealDB backwards
    and never replaces an image someone wrote by hand.
    """
    if image == SURREAL_IMAGE_REFERENCE:
        return image
    match = MANAGED_SURREAL_IMAGE.fullmatch(image)
    current = surreal_version(match["tag"]) if match else None
    shipped = surreal_version(SURREAL_IMAGE.rpartition(":")[2])
    if current is None or shipped is None or current > shipped:
        return None
    return SURREAL_IMAGE_REFERENCE


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
