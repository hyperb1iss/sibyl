#!/usr/bin/env bash
set -euo pipefail

echo "Installing Python dependencies..."
uv sync --all-groups

echo "Installing Node dependencies..."
pnpm install

echo "Installing Sibyl dev tools (editable)..."
moon run install-dev

echo "Running database migrations..."
uv run --package sibyld sibyld db upgrade

echo "Devcontainer ready! Run 'moon run dev' to start."
