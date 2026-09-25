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

import os
from ipaddress import ip_network

import structlog

from sibyl import config as config_module
from sibyl.config import TRUST_EVERY_FORWARDING_PEER

log = structlog.get_logger()


def _unbounded_entries(trusted: list[str]) -> list[str]:
    """Entries that trust every peer: `*`, or a range covering a whole address family."""
    return [
        entry
        for entry in trusted
        if entry == TRUST_EVERY_FORWARDING_PEER
        or ("/" in entry and ip_network(entry).prefixlen == 0)
    ]


def _configured_by() -> str:
    if os.environ.get("SIBYL_FORWARDED_ALLOW_IPS", "").strip():
        return "SIBYL_FORWARDED_ALLOW_IPS"
    if os.environ.get("FORWARDED_ALLOW_IPS", "").strip():
        return "FORWARDED_ALLOW_IPS"
    return "SIBYL_FORWARDED_ALLOW_IPS"


def warn_if_every_peer_is_trusted() -> None:
    """Log loudly when the trust list lets any peer choose the client address."""
    unbounded = _unbounded_entries(list(config_module.settings.forwarded_allow_ips))
    if not unbounded:
        return
    log.warning(
        "forwarded_allow_ips_trusts_every_peer",
        setting=_configured_by(),
        entries=unbounded,
        message=(
            "Every peer may set X-Forwarded-For, and with every hop trusted uvicorn takes "
            "the leftmost entry, which the client controls unless every proxy in front "
            "overwrites the header. Any client can then choose its own address, sidestep "
            "per-address rate limits, and satisfy the break-glass IP allowlist. List the "
            "proxy IPs or CIDR ranges instead."
        ),
    )


def forwarded_allow_ips() -> list[str]:
    """Return the configured trust list for uvicorn, warning when it trusts every peer."""
    warn_if_every_peer_is_trusted()
    return list(config_module.settings.forwarded_allow_ips)


def forwarded_allow_ips_cli_args() -> list[str]:
    """Uvicorn CLI arguments carrying the trust list to a dev server subprocess.

    The subprocess warns from its app factory, so the parent stays quiet.
    """
    return ["--forwarded-allow-ips", ",".join(config_module.settings.forwarded_allow_ips)]
