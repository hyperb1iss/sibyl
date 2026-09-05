#!/bin/sh
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
init_output() {
    PURPLE='' CYAN='' CORAL='' GREEN='' YELLOW='' RED=''
    DIM='' BOLD='' RESET='' GRAD_2='' GRAD_3='' GRAD_4=''
    if [ -t 1 ] && [ "${TERM:-}" != dumb ] && [ -z "${NO_COLOR:-}" ]; then
        PURPLE=$(printf '\033[38;2;225;53;255m')
        CYAN=$(printf '\033[38;2;128;255;234m')
        CORAL=$(printf '\033[38;2;255;106;193m')
        GREEN=$(printf '\033[38;2;80;250;123m')
        YELLOW=$(printf '\033[38;2;241;250;140m')
        RED=$(printf '\033[38;2;255;99;99m')
        GRAD_2=$(printf '\033[38;2;201;88;247m')
        GRAD_3=$(printf '\033[38;2;176;130;241m')
        GRAD_4=$(printf '\033[38;2;152;172;238m')
        DIM=$(printf '\033[2m')
        BOLD=$(printf '\033[1m')
        RESET=$(printf '\033[0m')
    fi
}

# ============================================================================
# Helpers
# ============================================================================
info() { printf '%s\n' "${CYAN}▸${RESET} $1"; }
success() { printf '%s\n' "${GREEN}✓${RESET} $1"; }
warn() { printf '%s\n' "${YELLOW}!${RESET} $1"; }
error() { printf '%s\n' "${RED}✗${RESET} $1" >&2; exit 1; }

usage() {
    cat << EOF
Sibyl installer

Usage:
  install.sh [--server|--remote|--daemon] [--version VERSION] [--no-start] [--no-open] [--no-pull]

Modes:
  --server   Install Sibyl, start the local API + web UI, and open the browser (default)
  --remote   Install only the sibyl CLI for an existing remote Sibyl server
  --daemon   Install sibyl + sibyld for the embedded daemon without the web UI

Options:
  --version   Install a specific package version (default: latest release)
  --no-start  Install only; print the command to start later
  --no-open   Do not open the browser after starting the web UI
  --no-pull   Do not pull Docker images before starting the local server

Environment:
  SIBYL_INSTALL_MODE      server, remote, or daemon
  SIBYL_INSTALL_VERSION   package version to install, such as 1.3.2
  SIBYL_INSTALL_START     0 to install without starting
  SIBYL_INSTALL_OPEN      0 to skip opening the browser
  SIBYL_INSTALL_PULL      0 to skip pulling Docker images
  NO_COLOR                disable terminal colors
EOF
}

banner() {
    echo
    if [ -n "$PURPLE" ]; then
        printf '%s\n' "         ${CORAL}✦${RESET}"
        printf '%s\n' "      ${PURPLE}╔═╗${GRAD_2}╦${GRAD_3}╔╗ ${GRAD_4}╦ ╦${CYAN}╦${RESET}"
        printf '%s\n' "      ${PURPLE}╚═╗${GRAD_2}║${GRAD_3}╠╩╗${GRAD_4}╚╦╝${CYAN}║${RESET}"
        printf '%s\n' "      ${PURPLE}╚═╝${GRAD_2}╩${GRAD_3}╚═╝ ${GRAD_4}╩ ${CYAN}╩═╝${RESET}"
        printf '%s\n' "      ${DIM}${CYAN}collective intelligence runtime${RESET}"
    else
        printf '%s\n' 'SIBYL' 'collective intelligence runtime'
    fi
    echo
    printf '%s\n' "${DIM}Install mode:${RESET} ${BOLD}${MODE}${RESET}"
    printf '%s\n' "${DIM}Package version:${RESET} ${SIBYL_PYPI_VERSION:-latest release}"
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
        error "Docker is required. Install it from https://docs.docker.com/get-docker/"
    fi

    if ! docker info >/dev/null 2>&1; then
        error "Docker daemon is not running. Start Docker and try again."
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
        printf '%s@latest' "$package"
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

install_skill_stub() {
    info "Installing Sibyl agent skill..."
    if sibyl skill install --quiet; then
        success "Sibyl skill installed"
    else
        warn "Skill install failed. Run 'sibyl skill install' after installation."
    fi
}

install_sibyld() {
    install_tool "sibyld" "sibyld" "Sibyl local daemon"
}

start_local_server() {
    if [ "$START_AFTER_INSTALL" != "1" ]; then
        return
    fi

    if [ "$(docker inspect --format '{{.State.Running}}' sibyl-api 2>/dev/null || true)" = true ]; then
        warn "Existing server found; running server images will remain unchanged."
        info "To upgrade server images, run 'sibyl down && sibyl up --pull' when ready to restart."
    fi
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
    info "Checking the local API..."
    if ! wait_for_api; then
        error "The local API is not ready. Run 'sibyl local logs' to inspect startup errors."
    fi
}

# Match the PID file contract used by `sibyl start`, without signalling processes.
live_daemon_pid() {
    daemon_pid=$(cat "$HOME/.sibyl/run/sibyld.pid" 2>/dev/null) || return 1
    case "$daemon_pid" in ''|*[!0-9]*|0) return 1 ;; esac
    kill -0 "$daemon_pid" 2>/dev/null || return 1
    daemon_command=$(ps -p "$daemon_pid" -o args= 2>/dev/null) || return 1
    case "$daemon_command" in
        *sibyld*" serve --embedded "*"--port 3334 "*) printf '%s' "$daemon_pid" ;;
        *) return 1 ;;
    esac
}

wait_for_api() {
    expected_pid="${1:-}"
    readiness_deadline=$(( $(date +%s) + 120 ))
    while [ "$(date +%s)" -lt "$readiness_deadline" ]; do
        if [ -n "$expected_pid" ]; then
            [ "$(live_daemon_pid)" = "$expected_pid" ] || return 1
        fi
        if curl -fsS --noproxy '*' --max-time 5 http://localhost:3334/api/health/ready >/dev/null 2>&1; then
            if [ -n "$expected_pid" ]; then
                # A failed child must not borrow readiness from another listener.
                sleep 1
                [ "$(live_daemon_pid)" = "$expected_pid" ] || return 1
            fi
            return 0
        fi
        sleep 1
    done
    return 1
}

start_embedded_daemon() {
    if [ "$START_AFTER_INSTALL" != "1" ]; then
        return
    fi

    existing_pid=$(live_daemon_pid) || existing_pid=''
    if [ -n "$existing_pid" ]; then
        if ! wait_for_api "$existing_pid"; then
            error "Existing daemon is not ready; it was left running. Run 'sibyl doctor' for details."
        fi
        warn "Existing daemon preserved; restart it to use the installed packages."
        info "Run 'sibyl stop && sibyl start' when ready to restart with the installed version."
        return
    fi
    # Even an unhealthy HTTP listener occupies the daemon's configured port.
    if curl -sS --noproxy '*' --max-time 5 http://localhost:3334/api/health/ready >/dev/null 2>&1; then
        error "Port 3334 already serves an API without a matching daemon PID. Existing services were left unchanged. Use --no-start for a package-only update."
    fi

    info "Initializing local embedded context..."
    if ! sibyl init --local --force; then
        error "Failed to initialize the local embedded context."
    fi

    info "Starting embedded daemon..."
    if ! sibyl start; then
        error "Embedded daemon did not start. Run 'sibyl doctor' for details."
    fi
    started_pid=$(live_daemon_pid) || started_pid=''
    if [ -z "$started_pid" ] || ! wait_for_api "$started_pid"; then
        error "Embedded daemon did not become ready. Run 'sibyl doctor' and inspect ~/.sibyl/run/sibyld.log."
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
    init_output
    parse_args "$@"
    case "$MODE" in
        server|remote|daemon) ;;
        *) error "Unknown install mode: $MODE (use server, remote, or daemon)" ;;
    esac
    case "$START_AFTER_INSTALL:$OPEN_BROWSER:$PULL_IMAGES" in
        [01]:[01]:[01]) ;;
        *) error "SIBYL_INSTALL_START, SIBYL_INSTALL_OPEN, and SIBYL_INSTALL_PULL must be 0 or 1" ;;
    esac
    if { [ "$START_AFTER_INSTALL" = 1 ] && [ "$MODE" != remote ]; } || ! command -v uv >/dev/null 2>&1; then
        command -v curl >/dev/null 2>&1 || error "curl is required for bootstrap and local readiness checks. Install curl and try again."
    fi
    normalize_version
    check_os
    banner
    if [ "$MODE" = server ] && [ "$START_AFTER_INSTALL" = 1 ]; then
        check_docker
    fi

    echo
    install_uv
    install_sibyl
    install_skill_stub

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
    esac

    print_next_steps
}

main "$@"
