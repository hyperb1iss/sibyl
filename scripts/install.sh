#!/usr/bin/env bash
# ============================================================================
# Sibyl Installer
# One-line install: curl -fsSL https://get.sibyl.dev | bash
# ============================================================================
set -euo pipefail

# SilkCircuit color palette
PURPLE='\033[38;2;225;53;255m'
CYAN='\033[38;2;128;255;234m'
CORAL='\033[38;2;255;106;193m'
YELLOW='\033[38;2;241;250;140m'
GREEN='\033[38;2;80;250;123m'
RED='\033[38;2;255;99;99m'
DIM='\033[2m'
BOLD='\033[1m'
RESET='\033[0m'

# Configuration
SIBYL_DIR="${SIBYL_DIR:-$HOME/sibyl}"
COMPOSE_URL="https://raw.githubusercontent.com/hyperb1iss/sibyl/main/docker-compose.quickstart.yml"
ENV_URL="https://raw.githubusercontent.com/hyperb1iss/sibyl/main/.env.quickstart.example"

# ============================================================================
# Helper Functions
# ============================================================================

print_banner() {
    echo -e "${PURPLE}"
    cat << 'EOF'
   _____ _ __          __
  / ___/(_) /_  __  __/ /
  \__ \/ / __ \/ / / / /
 ___/ / / /_/ / /_/ / /
/____/_/_.___/\__, /_/
             /____/
EOF
    echo -e "${RESET}"
    echo -e "${DIM}Collective Intelligence Runtime${RESET}"
    echo
}

info() {
    echo -e "${CYAN}→${RESET} $1"
}

success() {
    echo -e "${GREEN}✓${RESET} $1"
}

warn() {
    echo -e "${YELLOW}⚠${RESET} $1"
}

error() {
    echo -e "${RED}✗${RESET} $1"
}

# ============================================================================
# Checks
# ============================================================================

check_docker() {
    info "Checking Docker..."

    if ! command -v docker &> /dev/null; then
        error "Docker is not installed"
        echo
        echo "Install Docker from: https://docs.docker.com/get-docker/"
        exit 1
    fi

    if ! docker info &> /dev/null; then
        error "Docker daemon is not running"
        echo
        echo "Start Docker and try again."
        exit 1
    fi

    DOCKER_VERSION=$(docker version --format '{{.Server.Version}}' 2>/dev/null || echo "unknown")
    success "Docker ${DOCKER_VERSION} is ready"
}

check_compose() {
    info "Checking Docker Compose..."

    if docker compose version &> /dev/null; then
        COMPOSE_VERSION=$(docker compose version --short 2>/dev/null || echo "unknown")
        success "Docker Compose ${COMPOSE_VERSION} is ready"
    elif command -v docker-compose &> /dev/null; then
        warn "Using legacy docker-compose (consider upgrading to Docker Compose V2)"
    else
        error "Docker Compose is not available"
        exit 1
    fi
}

# ============================================================================
# Setup
# ============================================================================

create_directory() {
    info "Creating ${SIBYL_DIR}..."

    if [[ -d "$SIBYL_DIR" ]]; then
        warn "Directory already exists"
        if [[ -f "$SIBYL_DIR/.env" ]]; then
            echo
            echo -e "${YELLOW}Sibyl is already installed.${RESET}"
            echo
            echo "To start:  cd $SIBYL_DIR && docker compose -f docker-compose.quickstart.yml up -d"
            echo "To update: cd $SIBYL_DIR && docker compose -f docker-compose.quickstart.yml pull"
            echo
            read -p "Reinstall? This will overwrite existing config. [y/N] " -n 1 -r
            echo
            if [[ ! $REPLY =~ ^[Yy]$ ]]; then
                exit 0
            fi
        fi
    else
        mkdir -p "$SIBYL_DIR"
    fi

    success "Directory ready"
}

download_files() {
    info "Downloading configuration files..."

    cd "$SIBYL_DIR"

    if command -v curl &> /dev/null; then
        curl -fsSL "$COMPOSE_URL" -o docker-compose.quickstart.yml
        curl -fsSL "$ENV_URL" -o .env.example
    elif command -v wget &> /dev/null; then
        wget -q "$COMPOSE_URL" -O docker-compose.quickstart.yml
        wget -q "$ENV_URL" -O .env.example
    else
        error "Neither curl nor wget found"
        exit 1
    fi

    success "Downloaded docker-compose.quickstart.yml"
}

# ============================================================================
# Configuration
# ============================================================================

prompt_api_keys() {
    echo
    echo -e "${PURPLE}${BOLD}API Configuration${RESET}"
    echo -e "${DIM}Sibyl needs API keys for semantic search and entity extraction.${RESET}"
    echo

    # OpenAI API Key
    echo -e "${CYAN}OpenAI API Key${RESET} ${DIM}(for embeddings - https://platform.openai.com/api-keys)${RESET}"
    read -p "  Enter key: " OPENAI_KEY

    if [[ -z "$OPENAI_KEY" ]]; then
        error "OpenAI API key is required"
        exit 1
    fi

    # Validate OpenAI key format
    if [[ ! "$OPENAI_KEY" =~ ^sk- ]]; then
        warn "OpenAI key should start with 'sk-'"
    fi

    echo

    # Anthropic API Key
    echo -e "${CYAN}Anthropic API Key${RESET} ${DIM}(for entity extraction - https://console.anthropic.com/settings/keys)${RESET}"
    read -p "  Enter key: " ANTHROPIC_KEY

    if [[ -z "$ANTHROPIC_KEY" ]]; then
        error "Anthropic API key is required"
        exit 1
    fi

    # Validate Anthropic key format
    if [[ ! "$ANTHROPIC_KEY" =~ ^sk-ant- ]]; then
        warn "Anthropic key should start with 'sk-ant-'"
    fi

    success "API keys configured"
}

generate_secrets() {
    info "Generating secure secrets..."

    # Generate JWT secret
    if command -v openssl &> /dev/null; then
        JWT_SECRET=$(openssl rand -hex 32)
    else
        # Fallback for systems without openssl
        JWT_SECRET=$(head -c 32 /dev/urandom | xxd -p | tr -d '\n')
    fi

    success "Generated JWT secret"
}

write_env_file() {
    info "Writing configuration..."

    cat > "$SIBYL_DIR/.env" << EOF
# Sibyl Configuration
# Generated by install.sh on $(date -Iseconds)

# API Keys (REQUIRED)
SIBYL_OPENAI_API_KEY=$OPENAI_KEY
SIBYL_ANTHROPIC_API_KEY=$ANTHROPIC_KEY

# Security
SIBYL_JWT_SECRET=$JWT_SECRET

# Database passwords (change for production)
SIBYL_POSTGRES_PASSWORD=sibyl_quickstart
SIBYL_FALKORDB_PASSWORD=sibyl_quickstart
EOF

    chmod 600 "$SIBYL_DIR/.env"
    success "Configuration saved to $SIBYL_DIR/.env"
}

# ============================================================================
# Service Management
# ============================================================================

start_services() {
    echo
    echo -e "${PURPLE}${BOLD}Starting Sibyl...${RESET}"
    echo

    cd "$SIBYL_DIR"

    info "Pulling Docker images..."
    docker compose -f docker-compose.quickstart.yml pull --quiet

    info "Starting services..."
    docker compose -f docker-compose.quickstart.yml up -d

    echo
    info "Waiting for services to be healthy..."

    # Wait for API to be ready (max 60 seconds)
    for i in {1..60}; do
        if curl -s http://localhost:3334/api/health > /dev/null 2>&1; then
            break
        fi
        printf "."
        sleep 1
    done
    echo

    if curl -s http://localhost:3334/api/health > /dev/null 2>&1; then
        success "Sibyl is running!"
    else
        warn "Services are starting (this may take a moment)"
    fi
}

open_browser() {
    URL="http://localhost:3337"

    echo
    echo -e "${GREEN}${BOLD}🚀 Sibyl is ready!${RESET}"
    echo
    echo -e "  ${CYAN}Web UI:${RESET}    $URL"
    echo -e "  ${CYAN}API:${RESET}       http://localhost:3334"
    echo -e "  ${CYAN}Graph UI:${RESET}  http://localhost:3335"
    echo

    # Try to open browser
    if command -v open &> /dev/null; then
        open "$URL" 2>/dev/null || true
    elif command -v xdg-open &> /dev/null; then
        xdg-open "$URL" 2>/dev/null || true
    fi
}

print_next_steps() {
    echo -e "${PURPLE}${BOLD}Next Steps${RESET}"
    echo
    echo "  1. Complete the setup wizard in your browser"
    echo "  2. Connect Claude Code:"
    echo -e "     ${DIM}claude mcp add sibyl --transport http http://localhost:3334/mcp${RESET}"
    echo
    echo -e "${DIM}Commands:${RESET}"
    echo "  Stop:    cd $SIBYL_DIR && docker compose -f docker-compose.quickstart.yml down"
    echo "  Start:   cd $SIBYL_DIR && docker compose -f docker-compose.quickstart.yml up -d"
    echo "  Logs:    cd $SIBYL_DIR && docker compose -f docker-compose.quickstart.yml logs -f"
    echo "  Update:  cd $SIBYL_DIR && docker compose -f docker-compose.quickstart.yml pull && docker compose -f docker-compose.quickstart.yml up -d"
    echo
}

# ============================================================================
# Main
# ============================================================================

main() {
    print_banner

    check_docker
    check_compose

    echo

    create_directory
    download_files
    prompt_api_keys
    generate_secrets
    write_env_file
    start_services
    open_browser
    print_next_steps
}

main "$@"
