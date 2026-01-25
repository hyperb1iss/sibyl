#!/usr/bin/env bash
# Sibyl Installer
# Usage: curl -fsSL https://sibyl.dev/install.sh | sh
#
# This script:
#   1. Installs uv (if not present)
#   2. Installs sibyl-cli via uv
#   3. Starts Sibyl (prompts for API keys on first run)

set -eu

# ============================================================================
# Colors (SilkCircuit palette)
# ============================================================================
PURPLE=$(printf '\033[38;2;225;53;255m')
CYAN=$(printf '\033[38;2;128;255;234m')
GREEN=$(printf '\033[38;2;80;250;123m')
YELLOW=$(printf '\033[38;2;241;250;140m')
RED=$(printf '\033[38;2;255;99;99m')
DIM=$(printf '\033[2m')
BOLD=$(printf '\033[1m')
RESET=$(printf '\033[0m')

# ============================================================================
# Helpers
# ============================================================================
info() { printf '%s\n' "${CYAN}▸${RESET} $1"; }
success() { printf '%s\n' "${GREEN}✓${RESET} $1"; }
warn() { printf '%s\n' "${YELLOW}!${RESET} $1"; }
error() { printf '%s\n' "${RED}✗${RESET} $1"; exit 1; }

banner() {
    printf '%s' "${PURPLE}${BOLD}"
    cat << 'EOF'
   _____ _ __          __
  / ___/(_) /_  __  __/ /
  \__ \/ / __ \/ / / / /
 ___/ / / /_/ / /_/ / /
/____/_/_.___/\__, /_/
             /____/
EOF
    printf '%s\n' "${RESET}"
    printf '%s\n' "${DIM}Collective Intelligence Runtime${RESET}"
    echo
}

# ============================================================================
# Checks
# ============================================================================
check_os() {
    case "$(uname -s)" in
        Linux*)  OS=linux ;;
        Darwin*) OS=macos ;;
        *)       error "Unsupported OS: $(uname -s). Use Linux or macOS." ;;
    esac
}

check_docker() {
    if ! command -v docker >/dev/null 2>&1; then
        error "Docker is required but not installed.\n\n  Install from: https://docs.docker.com/get-docker/"
    fi

    if ! docker info >/dev/null 2>&1; then
        error "Docker daemon is not running.\n\n  Start Docker and try again."
    fi

    success "Docker is available"
}

# ============================================================================
# Installation
# ============================================================================
install_uv() {
    if command -v uv >/dev/null 2>&1; then
        success "uv is already installed ($(uv --version))"
        return
    fi

    info "Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh

    # Add to PATH for this session
    export PATH="$HOME/.local/bin:$PATH"

    if command -v uv >/dev/null 2>&1; then
        success "uv installed successfully"
    else
        error "Failed to install uv"
    fi
}

install_sibyl() {
    info "Installing sibyl-dev..."

    if uv tool list 2>/dev/null | grep -q "sibyl-dev"; then
        warn "sibyl-dev is already installed, upgrading..."
        uv tool upgrade sibyl-dev
    else
        uv tool install sibyl-dev
    fi

    # Ensure tool bin is in PATH
    export PATH="$HOME/.local/bin:$PATH"

    if command -v sibyl >/dev/null 2>&1; then
        success "sibyl-dev installed successfully"
    else
        error "Failed to install sibyl-dev"
    fi
}

# ============================================================================
# Main
# ============================================================================
setup_agent_integration() {
    info "Setting up agent integration (skills + hooks)..."
    if sibyl local setup >/dev/null 2>&1; then
        success "Agent integration configured"
    else
        warn "Agent integration setup skipped (run 'sibyl local setup' later)"
    fi
}

main() {
    banner

    check_os
    check_docker

    echo
    install_uv
    install_sibyl

    echo
    setup_agent_integration

    echo
    printf '%s\n' "${GREEN}${BOLD}Installation complete!${RESET}"
    echo
    printf '%s\n' "Starting Sibyl..."
    echo

    # Start Sibyl (will prompt for API keys on first run)
    sibyl local start
}

main "$@"
