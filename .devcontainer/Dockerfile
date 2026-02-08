FROM mcr.microsoft.com/devcontainers/python:1-3.13-bookworm

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

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

RUN curl -fsSL https://moonrepo.dev/install/proto.sh | bash -s -- --yes \
    && proto install moon 2.0.0-beta.1 \
    && proto install node 24 \
    && proto install pnpm 10 \
    && chmod -R a+rx /opt/proto

WORKDIR /workspace/sibyl
