#!/usr/bin/env bash
# Sibyl Development Environment Setup
# Ensures all toolchain dependencies are installed and configured

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly REPO_ROOT

readonly PROTO_INSTALLER_VERSION="0.61.1"
readonly PROTO_INSTALLER_SHA256="71e0a91c9dab49b714c701d9ea62d4ac5016783b29d8d553463e1c16ac7a3047"
readonly PROTO_INSTALLER_URL="https://github.com/moonrepo/proto/releases/download/v${PROTO_INSTALLER_VERSION}/proto_cli-installer.sh"

# ═══════════════════════════════════════════════════════════════════════════════
# SilkCircuit Neon Palette
# ═══════════════════════════════════════════════════════════════════════════════

ELECTRIC_PURPLE='\033[38;2;225;53;255m'
NEON_CYAN='\033[38;2;128;255;234m'
CORAL='\033[38;2;255;106;193m'
ELECTRIC_YELLOW='\033[38;2;241;250;140m'
SUCCESS_GREEN='\033[38;2;80;250;123m'
ERROR_RED='\033[38;2;255;99;99m'
DIM='\033[2m'
ITALIC='\033[3m'
BOLD='\033[1m'
RESET='\033[0m'

# Banner gradient (electric purple → neon cyan, sampled across the wordmark)
GRAD_1='\033[38;2;225;53;255m'
GRAD_2='\033[38;2;201;88;247m'
GRAD_3='\033[38;2;176;130;241m'
GRAD_4='\033[38;2;152;172;238m'
GRAD_5='\033[38;2;128;255;234m'

# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════

info() { echo -e "${NEON_CYAN}→${RESET} $1"; }
success() { echo -e "${SUCCESS_GREEN}✓${RESET} $1"; }
warn() { echo -e "${ELECTRIC_YELLOW}!${RESET} $1"; }
error() { echo -e "${ERROR_RED}✗${RESET} $1" >&2; }
header() { echo -e "\n${ELECTRIC_PURPLE}${BOLD}═══ $1 ═══${RESET}\n"; }

command_exists() { command -v "$1" &>/dev/null; }

calculate_sha256() {
    local path="$1"

    if command_exists sha256sum; then
        sha256sum "$path" | awk '{print $1}'
    elif command_exists shasum; then
        shasum -a 256 "$path" | awk '{print $1}'
    else
        error "SHA-256 verification requires sha256sum or shasum"
        return 1
    fi
}

required_version() {
    local tool="$1"
    local version

    version=$(sed -nE \
        "s/^[[:space:]]*${tool}[[:space:]]*=[[:space:]]*\"([^\"]+)\".*/\\1/p" \
        "$REPO_ROOT/.prototools" | head -1)
    if [[ ! "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        error "Missing exact ${tool} version in .prototools"
        return 1
    fi

    printf '%s\n' "$version"
}

installed_version() {
    local tool="$1"
    local output

    if ! command_exists "$tool"; then
        return 0
    fi

    output=$("$tool" --version 2>/dev/null) || return $?
    printf '%s\n' "$output" \
        | head -1 \
        | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' \
        || true
}

install_exact_tool() {
    local tool="$1"
    local expected="$2"
    local current

    current=$(installed_version "$tool") || current=""
    if [[ "$current" == "$expected" ]]; then
        success "${tool} ${CORAL}v${current}${RESET} already installed"
        return 0
    fi

    if [[ -n "$current" ]]; then
        info "Upgrading ${CORAL}${tool}${RESET} from v${current} to v${expected}..."
    else
        info "Installing ${CORAL}${tool}${RESET} v${expected}..."
    fi

    proto install "$tool" "$expected" --pin global
    hash -r
    current=$(installed_version "$tool") || current=""
    if [[ "$current" != "$expected" ]]; then
        error "${tool} v${expected} was installed but v${current:-missing} resolves on PATH"
        exit 1
    fi

    success "${tool} ${CORAL}v${current}${RESET} installed"
}

check_os() {
    case "$(uname -s)" in
        Darwin) OS="macos" ;;
        Linux) OS="linux" ;;
        *) error "Unsupported OS: $(uname -s)"; exit 1 ;;
    esac
}

# ═══════════════════════════════════════════════════════════════════════════════
# Banner
# ═══════════════════════════════════════════════════════════════════════════════

print_banner() {
    echo
    echo -e "         ${CORAL}✦${RESET}"
    echo -e "      ${GRAD_1}╔═╗${GRAD_2}╦${GRAD_3}╔╗ ${GRAD_4}╦ ╦${GRAD_5}╦${RESET}"
    echo -e "      ${GRAD_1}╚═╗${GRAD_2}║${GRAD_3}╠╩╗${GRAD_4}╚╦╝${GRAD_5}║${RESET}"
    echo -e "      ${GRAD_1}╚═╝${GRAD_2}╩${GRAD_3}╚═╝ ${GRAD_4}╩ ${GRAD_5}╩═╝${RESET}"
    echo -e "      ${DIM}${ELECTRIC_PURPLE}─────────────────${RESET}"
    echo -e "      ${DIM}${ITALIC}${NEON_CYAN}collective intelligence runtime${RESET}"
    echo
}

# ═══════════════════════════════════════════════════════════════════════════════
# Proto Installation
# ═══════════════════════════════════════════════════════════════════════════════

install_verified_proto() {
    local expected="$1"

    if [[ "$expected" != "$PROTO_INSTALLER_VERSION" ]]; then
        error "No verified proto installer is pinned for v${expected}"
        return 1
    fi
    if ! (
        installer=$(mktemp "${TMPDIR:-/tmp}/sibyl-proto-installer.XXXXXX") || exit 1
        trap 'rm -f "$installer"' EXIT
        curl -fsSL "$PROTO_INSTALLER_URL" -o "$installer" || exit 1
        actual_sha256=$(calculate_sha256 "$installer") || exit 1
        if [[ "$actual_sha256" != "$PROTO_INSTALLER_SHA256" ]]; then
            error "proto installer checksum mismatch"
            exit 1
        fi
        bash "$installer"
    ); then
        error "Verified proto v${expected} installation failed"
        return 1
    fi
}

install_proto() {
    local expected
    local current
    expected=$(required_version proto) || return 1

    export PROTO_HOME="${PROTO_HOME:-$HOME/.proto}"
    export PATH="$PROTO_HOME/bin:$PROTO_HOME/shims:$PATH"
    current=$(installed_version proto) || current=""
    if [[ "$current" == "$expected" ]]; then
        success "proto ${CORAL}v${current}${RESET} already installed"
        return 0
    fi

    if [[ -n "$current" ]]; then
        info "Upgrading proto from v${current} to v${expected}..."
        if ! proto upgrade "$expected"; then
            warn "proto self-upgrade failed; using the verified installer"
        fi
        hash -r
        current=$(installed_version proto) || current=""
        if [[ "$current" != "$expected" ]]; then
            warn "proto self-upgrade did not activate v${expected}; using the verified installer"
            install_verified_proto "$expected"
        fi
    else
        info "Installing proto v${expected} (toolchain version manager)..."
        install_verified_proto "$expected"
    fi

    hash -r
    current=$(installed_version proto) || current=""

    if [[ "$current" != "$expected" ]]; then
        error "proto v${expected} was installed but v${current:-missing} resolves on PATH"
        exit 1
    fi

    success "proto ${CORAL}v${current}${RESET} installed"
}

# ═══════════════════════════════════════════════════════════════════════════════
# Moon Installation
# ═══════════════════════════════════════════════════════════════════════════════

install_moon() {
    local expected
    expected=$(required_version moon) || return 1
    install_exact_tool moon "$expected"
}

# ═══════════════════════════════════════════════════════════════════════════════
# Toolchain Installation (via proto)
# ═══════════════════════════════════════════════════════════════════════════════

install_toolchain() {
    header "Toolchain"

    if [[ ! -f "$REPO_ROOT/.prototools" ]]; then
        error ".prototools not found - are you in the sibyl directory?"
        exit 1
    fi

    # Make sure proto-managed shims are visible if proto was just installed
    export PROTO_HOME="${PROTO_HOME:-$HOME/.proto}"
    export PATH="$PROTO_HOME/shims:$PROTO_HOME/bin:$PATH"

    info "Resolving toolchain from ${CORAL}.prototools${RESET}..."

    # Reconcile tools sequentially so pnpm always observes the pinned Node.
    local tools=("node" "pnpm" "python" "uv")
    local expected
    for tool in "${tools[@]}"; do
        expected=$(required_version "$tool") || return 1
        install_exact_tool "$tool" "$expected"
    done
}

# ═══════════════════════════════════════════════════════════════════════════════
# Docker Check
# ═══════════════════════════════════════════════════════════════════════════════

check_docker() {
    header "Docker"

    if ! command_exists docker; then
        warn "Docker not installed"
        if [[ "$OS" == "macos" ]]; then
            echo -e "${DIM}Install Docker Desktop: https://docs.docker.com/desktop/install/mac-install/${RESET}"
        else
            echo -e "${DIM}Install Docker: https://docs.docker.com/engine/install/${RESET}"
        fi
        echo -e "${DIM}Docker is required for SurrealDB (and the legacy FalkorDB + PostgreSQL stack)${RESET}"
        return 1
    fi

    if ! docker info &>/dev/null; then
        warn "Docker daemon not running"
        echo -e "${DIM}Start Docker Desktop or run: sudo systemctl start docker${RESET}"
        return 1
    fi

    success "Docker is running"
    return 0
}

# ═══════════════════════════════════════════════════════════════════════════════
# Dependencies Installation
# ═══════════════════════════════════════════════════════════════════════════════

install_dependencies() {
    header "Dependencies"

    # Python dependencies (via uv)
    info "Installing Python dependencies..."
    uv sync --all-groups
    success "Python dependencies installed"

    # Node dependencies (via pnpm)
    info "Installing Node dependencies..."
    pnpm install
    success "Node dependencies installed"
}

# ═══════════════════════════════════════════════════════════════════════════════
# Pre-commit Hooks
# ═══════════════════════════════════════════════════════════════════════════════

setup_precommit() {
    header "Git Hooks"

    if [[ -f .pre-commit-config.yaml ]]; then
        info "Installing pre-commit hooks..."
        uv run pre-commit install
        success "Pre-commit hooks installed"
    else
        info "No pre-commit config found, skipping hooks"
    fi
}

# ═══════════════════════════════════════════════════════════════════════════════
# CLI Installation
# ═══════════════════════════════════════════════════════════════════════════════

verify_cli() {
    header "Sibyl CLI"

    # CLI tools are installed in .venv/bin/ - verify they exist
    if [[ -x ".venv/bin/sibyl" ]] && [[ -x ".venv/bin/sibyld" ]]; then
        success "CLI tools installed: ${NEON_CYAN}sibyl${RESET}, ${NEON_CYAN}sibyld${RESET}"
        echo -e "${DIM}Run via: uv run sibyl ... or uv run sibyld ...${RESET}"
    else
        warn "CLI tools not found in .venv/bin/"
        echo -e "${DIM}Try: uv sync --all-groups${RESET}"
    fi
}

# ═══════════════════════════════════════════════════════════════════════════════
# Summary
# ═══════════════════════════════════════════════════════════════════════════════

print_summary() {
    header "Setup Complete"

    echo -e "${NEON_CYAN}Quick Start:${RESET}"
    echo -e "  ${DIM}Start infrastructure:${RESET}  moon run dev"
    echo -e "  ${DIM}Stop infrastructure:${RESET}   moon run stop"
    echo -e "  ${DIM}Run tests:${RESET}             moon run :test"
    echo -e "  ${DIM}Run linting:${RESET}           moon run :lint"
    echo ""
    echo -e "${NEON_CYAN}Ports:${RESET}"
    echo -e "  ${DIM}API + MCP:${RESET}    ${CORAL}3334${RESET}"
    echo -e "  ${DIM}Frontend:${RESET}     ${CORAL}3337${RESET}"
    echo -e "  ${DIM}SurrealDB:${RESET}    ${CORAL}8000${RESET}    ${DIM}(default)${RESET}"
    echo -e "  ${DIM}FalkorDB:${RESET}     ${CORAL}6380${RESET}    ${DIM}(legacy)${RESET}"
    echo -e "  ${DIM}Postgres:${RESET}     ${CORAL}5433${RESET}    ${DIM}(legacy)${RESET}"
    echo ""

    if ! check_docker 2>/dev/null; then
        echo -e "${ELECTRIC_YELLOW}Note:${RESET} Docker required for databases. Install and start it."
    fi
}

# ═══════════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════════

main() {
    print_banner
    check_os

    # Change to script directory
    cd "$REPO_ROOT"

    header "Environment: ${OS}"

    install_proto
    install_moon
    install_toolchain
    check_docker || true  # Don't fail if Docker missing
    install_dependencies
    setup_precommit
    verify_cli

    print_summary
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
