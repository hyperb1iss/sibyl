# Caddy with the Cloudflare DNS module compiled in, for ACME DNS-01.
# The stock caddy image ships no DNS providers, so we xcaddy a build.

FROM caddy:2-builder AS builder
RUN xcaddy build --with github.com/caddy-dns/cloudflare

FROM caddy:2
COPY --from=builder /usr/bin/caddy /usr/bin/caddy
