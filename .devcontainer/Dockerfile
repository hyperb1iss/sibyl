FROM mcr.microsoft.com/devcontainers/python:1-3.13-bookworm

# Install uv
COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /uvx /usr/local/bin/

# Install system deps
# Remove stale Yarn apt source from base image (we use pnpm, not Yarn)
RUN rm -f /etc/apt/sources.list.d/yarn.list \
    && apt-get update && apt-get install -y --no-install-recommends \
    git curl jq ca-certificates build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install proto + moon + node + pnpm (versions from .prototools)
# PROTO_HOME in /opt so it's accessible to both root (build) and vscode (runtime)
ENV PROTO_HOME=/opt/proto
ENV PATH="/opt/proto/shims:/opt/proto/bin:${PATH}"

ARG PROTO_VERSION=0.61.1
ARG PROTO_INSTALLER_SHA256=71e0a91c9dab49b714c701d9ea62d4ac5016783b29d8d553463e1c16ac7a3047

RUN installer="$(mktemp)" \
    && curl -fsSL \
        "https://github.com/moonrepo/proto/releases/download/v${PROTO_VERSION}/proto_cli-installer.sh" \
        -o "$installer" \
    && printf '%s  %s\n' "$PROTO_INSTALLER_SHA256" "$installer" \
        | sha256sum -c - \
    && bash "$installer" \
    && rm -f "$installer" \
    && proto install moon 2.5.3 --pin global \
    && proto install python 3.13.15 --pin global \
    && proto install node 24.19.0 --pin global \
    && proto install pnpm 11.23.0 --pin global \
    && printf '%s\n' \
        'export PROTO_HOME=/opt/proto' \
        'export PATH="/opt/proto/shims:/opt/proto/bin:$PATH"' \
        > /etc/profile.d/proto.sh \
    && chmod 0644 /etc/profile.d/proto.sh \
    && chmod -R a+rx /opt/proto

WORKDIR /workspace/sibyl
