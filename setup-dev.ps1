#!/usr/bin/env pwsh
# Sibyl Development Environment Setup (Windows / PowerShell)
# Ensures all toolchain dependencies are installed and configured.
#
# Requires PowerShell 7+ (pwsh). Run from the repo root:
#   pwsh -File .\setup-dev.ps1
# or, with an interactive pwsh already open:
#   .\setup-dev.ps1

#Requires -Version 7.0

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# Ensure box-drawing chars in the banner render correctly.
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

# ═══════════════════════════════════════════════════════════════════════════════
# SilkCircuit Neon Palette
# ═══════════════════════════════════════════════════════════════════════════════

$ESC = [char]27
$ELECTRIC_PURPLE = "$ESC[38;2;225;53;255m"
$NEON_CYAN       = "$ESC[38;2;128;255;234m"
$CORAL           = "$ESC[38;2;255;106;193m"
$ELECTRIC_YELLOW = "$ESC[38;2;241;250;140m"
$SUCCESS_GREEN   = "$ESC[38;2;80;250;123m"
$ERROR_RED       = "$ESC[38;2;255;99;99m"
$DIM             = "$ESC[2m"
$ITALIC          = "$ESC[3m"
$BOLD            = "$ESC[1m"
$RESET           = "$ESC[0m"

# Banner gradient (electric purple → neon cyan)
$GRAD_1 = "$ESC[38;2;225;53;255m"
$GRAD_2 = "$ESC[38;2;201;88;247m"
$GRAD_3 = "$ESC[38;2;176;130;241m"
$GRAD_4 = "$ESC[38;2;152;172;238m"
$GRAD_5 = "$ESC[38;2;128;255;234m"

# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════

function Write-Info    ([string]$msg) { Write-Host "${NEON_CYAN}→${RESET} $msg" }
function Write-Success ([string]$msg) { Write-Host "${SUCCESS_GREEN}✓${RESET} $msg" }
function Write-Warn    ([string]$msg) { Write-Host "${ELECTRIC_YELLOW}!${RESET} $msg" }
function Write-Err     ([string]$msg) { [Console]::Error.WriteLine("${ERROR_RED}✗${RESET} $msg") }
function Write-Header  ([string]$msg) { Write-Host "`n${ELECTRIC_PURPLE}${BOLD}═══ $msg ═══${RESET}`n" }

function Test-Command ([string]$name) {
    [bool](Get-Command $name -ErrorAction SilentlyContinue)
}

function Get-VersionString {
    param([string]$Tool)
    try {
        $raw = & $Tool --version 2>$null | Select-Object -First 1
        if ($raw -match '\d+\.\d+\.\d+') { return $Matches[0] }
        return ($raw ?? 'unknown').Trim()
    } catch {
        return 'unknown'
    }
}

function Add-PathOnce ([string]$Dir) {
    if (-not $Dir -or -not (Test-Path $Dir)) { return }
    $parts = $env:Path -split [IO.Path]::PathSeparator
    if ($parts -notcontains $Dir) {
        $env:Path = "$Dir$([IO.Path]::PathSeparator)$env:Path"
    }
}

# ═══════════════════════════════════════════════════════════════════════════════
# Banner
# ═══════════════════════════════════════════════════════════════════════════════

function Show-Banner {
    Write-Host ""
    Write-Host "         ${CORAL}✦${RESET}"
    Write-Host "      ${GRAD_1}╔═╗${GRAD_2}╦${GRAD_3}╔╗ ${GRAD_4}╦ ╦${GRAD_5}╦${RESET}"
    Write-Host "      ${GRAD_1}╚═╗${GRAD_2}║${GRAD_3}╠╩╗${GRAD_4}╚╦╝${GRAD_5}║${RESET}"
    Write-Host "      ${GRAD_1}╚═╝${GRAD_2}╩${GRAD_3}╚═╝ ${GRAD_4}╩ ${GRAD_5}╩═╝${RESET}"
    Write-Host "      ${DIM}${ELECTRIC_PURPLE}─────────────────${RESET}"
    Write-Host "      ${DIM}${ITALIC}${NEON_CYAN}collective intelligence runtime${RESET}"
    Write-Host ""
}

# ═══════════════════════════════════════════════════════════════════════════════
# Environment check
# ═══════════════════════════════════════════════════════════════════════════════

function Assert-Windows {
    if (-not $IsWindows) {
        Write-Err "setup-dev.ps1 is for Windows. Use ./setup-dev.sh on macOS/Linux."
        exit 1
    }
}

# ═══════════════════════════════════════════════════════════════════════════════
# Proto Installation
# ═══════════════════════════════════════════════════════════════════════════════

function Install-Proto {
    # Make sure proto's default install dir is on PATH for this session before
    # the existence check — a previous install may not have updated this shell.
    $protoHome = if ($env:PROTO_HOME) { $env:PROTO_HOME } else { Join-Path $HOME '.proto' }
    Add-PathOnce (Join-Path $protoHome 'shims')
    Add-PathOnce (Join-Path $protoHome 'bin')

    if (Test-Command proto) {
        $version = Get-VersionString -Tool 'proto'
        Write-Success "proto ${CORAL}v${version}${RESET} already installed"
        return
    }

    Write-Info "Installing proto (toolchain version manager)..."

    # The official one-liner is `irm <url> | iex`, but iex can't accept args
    # so we download to a temp file and invoke it directly. That lets us pass
    # flags through to `proto setup` when we need to.
    $installer = Join-Path ([IO.Path]::GetTempPath()) ("proto-install-$([guid]::NewGuid()).ps1")
    try {
        Invoke-WebRequest `
            -Uri 'https://moonrepo.dev/install/proto.ps1' `
            -OutFile $installer `
            -UseBasicParsing `
            -ErrorAction Stop
        & $installer
    }
    finally {
        if (Test-Path $installer) { Remove-Item $installer -Force -ErrorAction SilentlyContinue }
    }

    # Refresh PATH for the bin we just dropped on disk.
    Add-PathOnce (Join-Path $protoHome 'bin')
    Add-PathOnce (Join-Path $protoHome 'shims')

    if (-not (Test-Command proto)) {
        Write-Err "proto installation failed"
        Write-Host "${DIM}Try manually: irm https://moonrepo.dev/install/proto.ps1 | iex${RESET}"
        exit 1
    }

    # The Windows installer doesn't modify PATH or shell profiles itself; setup
    # does that. Run it so future pwsh sessions find proto without us patching
    # $PROFILE by hand. --yes accepts defaults non-interactively.
    Write-Info "Configuring proto shell integration (proto setup)..."
    try {
        & proto setup --yes 2>&1 | Out-Host
    } catch {
        Write-Warn "proto setup reported an issue; PATH may need a new pwsh session to pick up."
    }

    Write-Success "proto installed successfully"
}

# ═══════════════════════════════════════════════════════════════════════════════
# Moon Installation
# ═══════════════════════════════════════════════════════════════════════════════

function Install-Moon {
    if (Test-Command moon) {
        $version = Get-VersionString -Tool 'moon'
        Write-Success "moon ${CORAL}v${version}${RESET} already installed"
        return
    }

    Write-Info "Installing moon (monorepo orchestration)..."
    # moon is built-in to proto v0.45+, no plugin needed.
    & proto install moon
    if ($LASTEXITCODE -ne 0) {
        Write-Err "moon installation failed"
        exit 1
    }

    if (-not (Test-Command moon)) {
        Write-Err "moon not found on PATH after install"
        exit 1
    }
    Write-Success "moon installed successfully"
}

# ═══════════════════════════════════════════════════════════════════════════════
# Toolchain Installation (via proto)
# ═══════════════════════════════════════════════════════════════════════════════

function Install-Toolchain {
    Write-Header "Toolchain"

    if (-not (Test-Path '.prototools')) {
        Write-Err ".prototools not found - are you in the sibyl directory?"
        exit 1
    }

    $protoHome = if ($env:PROTO_HOME) { $env:PROTO_HOME } else { Join-Path $HOME '.proto' }
    Add-PathOnce (Join-Path $protoHome 'shims')
    Add-PathOnce (Join-Path $protoHome 'bin')

    Write-Info "Resolving toolchain from ${CORAL}.prototools${RESET}..."

    # Install tools one at a time. `proto use --yes` runs installs in parallel
    # and can deadlock if any plugin stalls — pnpm waits on node forever when
    # the node WASM plugin trips on a flaky nodejs.org fetch. Sequential
    # installs surface failures cleanly and let us short-circuit on tools
    # that are already present.
    $tools = @('node', 'pnpm', 'python', 'uv')
    foreach ($tool in $tools) {
        if (Test-Command $tool) {
            $version = Get-VersionString -Tool $tool
            Write-Success "${tool} ${CORAL}${version}${RESET}"
            continue
        }

        Write-Info "Installing ${CORAL}${tool}${RESET}..."
        & proto install $tool
        if ($LASTEXITCODE -ne 0) {
            Write-Err "${tool} install failed"
            Write-Host "${DIM}  Retry: ${RESET}proto install $tool"
            Write-Host "${DIM}  If the node plugin keeps failing, clear the cache:${RESET}"
            $pluginsDir = Join-Path $protoHome 'plugins'
            Write-Host "${DIM}    Remove-Item -Recurse -Force '$pluginsDir'; proto install $tool${RESET}"
            exit 1
        }

        if (Test-Command $tool) {
            $version = Get-VersionString -Tool $tool
            Write-Success "${tool} ${CORAL}${version}${RESET} installed"
        } else {
            Write-Err "${tool} not found on PATH after install"
            exit 1
        }
    }
}

# ═══════════════════════════════════════════════════════════════════════════════
# Docker Check
# ═══════════════════════════════════════════════════════════════════════════════

function Test-Docker {
    Write-Header "Docker"

    if (-not (Test-Command docker)) {
        Write-Warn "Docker not installed"
        Write-Host "${DIM}Install Docker Desktop: https://docs.docker.com/desktop/install/windows-install/${RESET}"
        Write-Host "${DIM}Docker is required for SurrealDB (and the legacy FalkorDB + PostgreSQL stack)${RESET}"
        $script:DockerOk = $false
        return
    }

    & docker info *> $null
    if ($LASTEXITCODE -ne 0) {
        Write-Warn "Docker daemon not running"
        Write-Host "${DIM}Start Docker Desktop from the Start menu${RESET}"
        $script:DockerOk = $false
        return
    }

    Write-Success "Docker is running"
    $script:DockerOk = $true
}

# ═══════════════════════════════════════════════════════════════════════════════
# Dependencies Installation
# ═══════════════════════════════════════════════════════════════════════════════

function Install-Dependencies {
    Write-Header "Dependencies"

    Write-Info "Installing Python dependencies..."
    & uv sync --all-groups
    if ($LASTEXITCODE -ne 0) {
        Write-Err "uv sync failed"
        exit 1
    }
    Write-Success "Python dependencies installed"

    Write-Info "Installing Node dependencies..."
    & pnpm install
    if ($LASTEXITCODE -ne 0) {
        Write-Err "pnpm install failed"
        exit 1
    }
    Write-Success "Node dependencies installed"
}

# ═══════════════════════════════════════════════════════════════════════════════
# Pre-commit Hooks
# ═══════════════════════════════════════════════════════════════════════════════

function Install-PreCommit {
    Write-Header "Git Hooks"

    if (Test-Path '.pre-commit-config.yaml') {
        Write-Info "Installing pre-commit hooks..."
        & uv run pre-commit install
        if ($LASTEXITCODE -ne 0) {
            Write-Warn "pre-commit install reported an issue"
        } else {
            Write-Success "Pre-commit hooks installed"
        }
    } else {
        Write-Info "No pre-commit config found, skipping hooks"
    }
}

# ═══════════════════════════════════════════════════════════════════════════════
# CLI Verification
# ═══════════════════════════════════════════════════════════════════════════════

function Test-Cli {
    Write-Header "Sibyl CLI"

    # On Windows, uv-managed venvs put executables in .venv\Scripts\ with .exe.
    $sibyl  = Join-Path '.venv' 'Scripts\sibyl.exe'
    $sibyld = Join-Path '.venv' 'Scripts\sibyld.exe'

    if ((Test-Path $sibyl) -and (Test-Path $sibyld)) {
        Write-Success "CLI tools installed: ${NEON_CYAN}sibyl${RESET}, ${NEON_CYAN}sibyld${RESET}"
        Write-Host "${DIM}Run via: uv run sibyl ... or uv run sibyld ...${RESET}"
    } else {
        Write-Warn "CLI tools not found in .venv\Scripts\"
        Write-Host "${DIM}Try: uv sync --all-groups${RESET}"
    }
}

# ═══════════════════════════════════════════════════════════════════════════════
# Summary
# ═══════════════════════════════════════════════════════════════════════════════

function Show-Summary {
    Write-Header "Setup Complete"

    Write-Host "${NEON_CYAN}Quick Start:${RESET}"
    Write-Host "  ${DIM}Start infrastructure:${RESET}  moon run dev"
    Write-Host "  ${DIM}Stop infrastructure:${RESET}   moon run stop"
    Write-Host "  ${DIM}Run tests:${RESET}             moon run :test"
    Write-Host "  ${DIM}Run linting:${RESET}           moon run :lint"
    Write-Host ""
    Write-Host "${NEON_CYAN}Ports:${RESET}"
    Write-Host "  ${DIM}API + MCP:${RESET}    ${CORAL}3334${RESET}"
    Write-Host "  ${DIM}Frontend:${RESET}     ${CORAL}3337${RESET}"
    Write-Host "  ${DIM}SurrealDB:${RESET}    ${CORAL}8000${RESET}    ${DIM}(default)${RESET}"
    Write-Host "  ${DIM}FalkorDB:${RESET}     ${CORAL}6380${RESET}    ${DIM}(legacy)${RESET}"
    Write-Host "  ${DIM}Postgres:${RESET}     ${CORAL}5433${RESET}    ${DIM}(legacy)${RESET}"
    Write-Host ""

    if (-not $script:DockerOk) {
        Write-Host "${ELECTRIC_YELLOW}Note:${RESET} Docker required for databases. Install Docker Desktop and start it."
    }

    Write-Host ""
    Write-Host "${DIM}If freshly installed, open a new pwsh session so PATH changes from ${RESET}proto setup${DIM} take effect.${RESET}"
}

# ═══════════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════════

function Main {
    Show-Banner
    Assert-Windows

    # Change to script directory (mirrors `cd "$(dirname "$0")"` in bash).
    Set-Location -Path $PSScriptRoot

    Write-Header "Environment: windows"

    $script:DockerOk = $false

    Install-Proto
    Install-Moon
    Install-Toolchain
    try { Test-Docker } catch {}  # Don't fail if Docker missing
    Install-Dependencies
    Install-PreCommit
    Test-Cli

    Show-Summary
}

Main
