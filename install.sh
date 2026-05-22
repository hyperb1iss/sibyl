#!/usr/bin/env bash
# Sibyl Installer
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/hyperb1iss/sibyl/main/install.sh | sh
#   curl -fsSL https://raw.githubusercontent.com/hyperb1iss/sibyl/main/install.sh | sh -s -- --remote
#   curl -fsSL https://raw.githubusercontent.com/hyperb1iss/sibyl/main/install.sh | sh -s -- --daemon
#
# This script:
#   1. Installs uv as bootstrap plumbing when needed
#   2. Installs the Sibyl CLI
#   3. Starts the local server + web UI by default

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

usage() {
    cat << EOF
Sibyl installer

Usage:
  install.sh [--server|--remote|--daemon] [--version VERSION] [--no-start] [--no-open]

Modes:
  --server   Install Sibyl, start the local API + web UI, and open the browser (default)
  --remote   Install only the sibyl CLI for an existing remote Sibyl server
  --daemon   Install sibyl + sibyld for the embedded daemon without the web UI

Options:
  --no-start  Install only; print the command to start later
  --no-open   Do not open the browser after starting the web UI
  --no-pull   Do not pull Docker images before starting the local server

Environment:
  SIBYL_INSTALL_MODE      server, remote, or daemon
  SIBYL_INSTALL_VERSION   package version to install, such as 1.0.0rc1
  SIBYL_INSTALL_START     0 to install without starting
  SIBYL_INSTALL_OPEN      0 to skip opening the browser
  SIBYL_INSTALL_PULL      0 to skip pulling Docker images
EOF
}

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

normalize_version() {
    if [ -z "${SIBYL_INSTALL_VERSION:-}" ]; then
        return 0
    fi

    SIBYL_PYPI_VERSION=$(printf '%s' "$SIBYL_INSTALL_VERSION" | sed -E 's/-(alpha|beta|a|b|rc)\.?/\1/')
}

package_spec() {
    package="$1"
    if [ -n "${SIBYL_PYPI_VERSION:-}" ]; then
        printf '%s==%s' "$package" "$SIBYL_PYPI_VERSION"
    else
        printf '%s' "$package"
    fi
}

install_tool() {
    package="$1"
    command_name="$2"
    label="$3"
    spec=$(package_spec "$package")

    if command -v "$command_name" >/dev/null 2>&1; then
        info "Updating $label..."
    else
        info "Installing $label..."
    fi

    if ! uv tool install "$spec" --force; then
        error "Failed to install $label ($spec). Check that the package is published."
    fi
    export PATH="$HOME/.local/bin:$PATH"

    if command -v "$command_name" >/dev/null 2>&1; then
        success "$label installed"
    else
        error "$label was installed, but '$command_name' is not on PATH. Add $HOME/.local/bin to PATH."
    fi
}

install_sibyl() {
    install_tool "sibyl-dev" "sibyl" "Sibyl CLI"
}

install_sibyld() {
    install_tool "sibyld" "sibyld" "Sibyl local daemon"
}

start_local_server() {
    if [ "$START_AFTER_INSTALL" != "1" ]; then
        return
    fi

    check_docker

    info "Starting Sibyl local server..."
    set -- up
    if [ "$PULL_IMAGES" = "1" ]; then
        set -- "$@" --pull
    fi
    if [ "$OPEN_BROWSER" != "1" ]; then
        set -- "$@" --no-browser
    fi

    if ! sibyl "$@"; then
        error "Failed to start Sibyl local server."
    fi
}

start_embedded_daemon() {
    if [ "$START_AFTER_INSTALL" != "1" ]; then
        return
    fi

    info "Initializing local embedded context..."
    if ! sibyl init --local --force; then
        error "Failed to initialize the local embedded context."
    fi

    info "Starting embedded daemon..."
    if ! sibyl start; then
        warn "Embedded daemon did not start. It may already be running; run 'sibyl doctor'."
        return
    fi
}

# ============================================================================
# Main
# ============================================================================
print_next_steps() {
    echo
    printf '%s\n' "${GREEN}${BOLD}Installation complete!${RESET}"
    echo
    case "$MODE" in
        server)
            if [ "$START_AFTER_INSTALL" = "1" ]; then
                printf '%s\n' "${BOLD}Sibyl server:${RESET} http://localhost:3337"
            else
                printf '%s\n' "${BOLD}Start the local server and web UI:${RESET}"
                printf '%s\n' "  sibyl up"
            fi
            ;;
        remote)
            printf '%s\n' "${BOLD}Connect to a remote Sibyl server:${RESET}"
            printf '%s\n' "  sibyl init --remote https://sibyl.example.com"
            printf '%s\n' "  sibyl auth login"
            ;;
        daemon)
            if [ "$START_AFTER_INSTALL" = "1" ]; then
                printf '%s\n' "${BOLD}Embedded daemon:${RESET} http://localhost:3334"
            else
                printf '%s\n' "${BOLD}Start the embedded daemon:${RESET}"
                printf '%s\n' "  sibyl init --local"
                printf '%s\n' "  sibyl start"
            fi
            ;;
    esac
}

parse_args() {
    MODE="${SIBYL_INSTALL_MODE:-server}"
    SIBYL_INSTALL_VERSION="${SIBYL_INSTALL_VERSION:-}"
    START_AFTER_INSTALL="${SIBYL_INSTALL_START:-1}"
    OPEN_BROWSER="${SIBYL_INSTALL_OPEN:-1}"
    PULL_IMAGES="${SIBYL_INSTALL_PULL:-1}"

    while [ "$#" -gt 0 ]; do
        case "$1" in
            --server|server|--local|local|--docker|docker)
                MODE=server
                ;;
            --remote|remote|--cli|cli)
                MODE=remote
                ;;
            --daemon|daemon)
                MODE=daemon
                ;;
            --no-start)
                START_AFTER_INSTALL=0
                ;;
            --no-open|--no-browser)
                OPEN_BROWSER=0
                ;;
            --no-pull)
                PULL_IMAGES=0
                ;;
            --version|-v)
                if [ "$#" -lt 2 ]; then
                    error "--version requires a value"
                fi
                SIBYL_INSTALL_VERSION="$2"
                shift
                ;;
            --help|-h)
                usage
                exit 0
                ;;
            *)
                error "Unknown option: $1"
                ;;
        esac
        shift
    done
}

main() {
    banner
    parse_args "$@"
    normalize_version

    check_os

    echo
    install_uv
    install_sibyl

    case "$MODE" in
        server)
            start_local_server
            ;;
        daemon)
            install_sibyld
            start_embedded_daemon
            ;;
        remote)
            ;;
        *)
            error "Unknown install mode: $MODE (use server, remote, or daemon)"
            ;;
    esac

    print_next_steps
}

main "$@"
