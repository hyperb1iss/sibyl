"""Which peers may report the client address through X-Forwarded-For.

Uvicorn applies the trust list before the app sees a request: when the direct
peer is trusted it replaces ``scope["client"]`` with the rightmost
X-Forwarded-For entry that is not itself trusted. Everything downstream that
reads ``request.client.host`` (the per-address rate limit, request and audit
logs, session records, the break-glass allowlist) therefore agrees on one
resolved address. Every launcher must hand uvicorn the same list, so they all
read it from here.
"""

from __future__ import annotations

import structlog

from sibyl import config as config_module
from sibyl.config import TRUST_EVERY_FORWARDING_PEER

log = structlog.get_logger()


def forwarded_allow_ips() -> list[str]:
    """Return the configured trust list for uvicorn, warning when it trusts every peer."""
    trusted = list(config_module.settings.forwarded_allow_ips)
    if trusted == [TRUST_EVERY_FORWARDING_PEER]:
        log.warning(
            "forwarded_allow_ips_trusts_every_peer",
            setting="SIBYL_FORWARDED_ALLOW_IPS",
            message=(
                "Every peer may set X-Forwarded-For, and uvicorn then takes its leftmost "
                "entry, which the client controls unless every proxy in front overwrites "
                "the header. Any client can then choose its own address, sidestep "
                "per-address rate limits, and satisfy the break-glass IP allowlist. "
                "List the proxy IPs or CIDR ranges instead."
            ),
        )
    return trusted


def forwarded_allow_ips_cli_args() -> list[str]:
    """Uvicorn CLI arguments carrying the trust list to a server subprocess."""
    return ["--forwarded-allow-ips", ",".join(forwarded_allow_ips())]
