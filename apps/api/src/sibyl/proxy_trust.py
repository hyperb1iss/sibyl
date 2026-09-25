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

# The broadest range, per address family, that passes without a warning. No
# proxy fleet needs more than an IPv4 /8 or an IPv6 /32, and covering a whole
# family takes either one wider entry or at least 256 of them, so split
# catch-alls such as 0.0.0.0/1,128.0.0.0/1 are caught entry by entry.
BROADEST_QUIET_PREFIXLEN = {4: 8, 6: 32}


def _broad_entries(trusted: list[str]) -> list[str]:
    """Entries that trust every peer or almost every peer: `*`, or a very wide range."""
    broad: list[str] = []
    for entry in trusted:
        if entry == TRUST_EVERY_FORWARDING_PEER:
            broad.append(entry)
        elif "/" in entry:
            network = ip_network(entry)
            if network.prefixlen < BROADEST_QUIET_PREFIXLEN[network.version]:
                broad.append(entry)
    return broad


def _configured_by() -> str:
    # Mirrors Settings: uvicorn's variable only counts while the Sibyl one is unset.
    if (
        "SIBYL_FORWARDED_ALLOW_IPS" not in os.environ
        and os.environ.get("FORWARDED_ALLOW_IPS", "").strip()
    ):
        return "FORWARDED_ALLOW_IPS"
    return "SIBYL_FORWARDED_ALLOW_IPS"


def warn_if_trust_is_too_broad() -> None:
    """Log loudly when the trust list lets any peer, or nearly any, choose the client address."""
    broad = _broad_entries(list(config_module.settings.forwarded_allow_ips))
    if not broad:
        return
    log.warning(
        "forwarded_allow_ips_trusts_every_peer",
        setting=_configured_by(),
        entries=broad,
        message=(
            "Every peer, or nearly every one, may set X-Forwarded-For, and with every hop "
            "trusted uvicorn takes the leftmost entry, which the client controls unless every "
            "proxy in front overwrites the header. Any client can then choose its own address, "
            "sidestep per-address rate limits, and satisfy the break-glass IP allowlist. List "
            "the proxy IPs or narrow CIDR ranges instead."
        ),
    )


def forwarded_allow_ips() -> list[str]:
    """Return the configured trust list for uvicorn, warning when it is too broad."""
    warn_if_trust_is_too_broad()
    return list(config_module.settings.forwarded_allow_ips)


def forwarded_allow_ips_cli_args() -> list[str]:
    """Uvicorn CLI arguments carrying the trust list to a dev server subprocess.

    The subprocess warns from its app factory, so the parent stays quiet.
    """
    return ["--forwarded-allow-ips", ",".join(config_module.settings.forwarded_allow_ips)]
