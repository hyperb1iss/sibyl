FROM mcr.microsoft.com/devcontainers/python:1-3.13-bookworm

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

# Install system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl jq ca-certificates build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install pnpm globally
RUN npm install -g pnpm@10

# Install proto + moon
RUN curl -fsSL https://moonrepo.dev/install/proto.sh | bash -s -- --yes \
    && /home/vscode/.proto/bin/proto plugin add moon \
       --to /home/vscode/.proto/.prototools \
    && /home/vscode/.proto/bin/proto install moon 2.0.0-beta.1

ENV PATH="/home/vscode/.proto/shims:/home/vscode/.proto/bin:${PATH}"

WORKDIR /workspace/sibyl
