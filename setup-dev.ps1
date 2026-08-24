#!/usr/bin/env pwsh
# Sibyl Development Environment Setup (Windows / PowerShell)
# Ensures all toolchain dependencies are installed and configured.
#
# Requires PowerShell 7+ (pwsh). Run from the repo root:
#   pwsh -File .\setup-dev.ps1
# or, with an interactive pwsh already open:
#   .\setup-dev.ps1

#Requires -Version 7.0

param([switch]$SkipMain)

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

$PROTO_INSTALLER_VERSION = '0.61.1'
$PROTO_INSTALLER_SHA256 = '2dbe76851e4740517bb60ee0957f9f2de33c03c7ae68f2b5abc89ec8f4f0e862'
$PROTO_INSTALLER_URL = "https://github.com/moonrepo/proto/releases/download/v${PROTO_INSTALLER_VERSION}/proto_cli-installer.ps1"

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

function Get-ApplicationCommand ([string]$Name) {
    $extensions = if ($IsWindows) {
        @($env:PATHEXT -split ';' | Where-Object { $_ })
    } else {
        @('')
    }
    foreach ($pathDir in ($env:Path -split [IO.Path]::PathSeparator)) {
        if (-not $pathDir) { continue }
        $pathDir = $pathDir.Trim().Trim('"')
        foreach ($extension in $extensions) {
            $candidate = Join-Path $pathDir "${Name}${extension}"
            if (-not (Test-Path $candidate -PathType Leaf)) { continue }
            $command = Get-Command $candidate -CommandType Application -ErrorAction SilentlyContinue
            if ($command) { return $command }
        }
    }
}

function Test-Command ([string]$Name) {
    [bool](Get-ApplicationCommand -Name $Name)
}

function Get-VersionString {
    param([string]$Tool)
    try {
        $command = Get-ApplicationCommand -Name $Tool
        if (-not $command) { return 'unknown' }
        $output = @(& $command.Source --version 2>$null)
        if ($LASTEXITCODE -ne 0) { return 'unknown' }
        $raw = $output | Select-Object -First 1
        if ($raw -match '\d+\.\d+\.\d+') { return $Matches[0] }
        return ($raw ?? 'unknown').Trim()
    } catch {
        return 'unknown'
    }
}

function Get-RequiredVersion {
    param([string]$Tool)

    $toolPattern = [Regex]::Escape($Tool)
    $pattern = '^\s*{0}\s*=\s*"([^"]+)"' -f $toolPattern
    foreach ($line in Get-Content (Join-Path $PSScriptRoot '.prototools')) {
        if ($line -match $pattern) {
            $version = $Matches[1]
            if ($version -notmatch '^\d+\.\d+\.\d+$') {
                Write-Err "Expected an exact $Tool version in .prototools"
                exit 1
            }
            return $version
        }
    }

    Write-Err "Missing exact $Tool version in .prototools"
    exit 1
}

function Set-ProtoPath ([string]$ProtoHome) {
    $prefix = @(
        (Join-Path $ProtoHome 'shims')
        (Join-Path $ProtoHome 'bin')
    )
    $protoPaths = [Collections.Generic.HashSet[string]]::new(
        [StringComparer]::OrdinalIgnoreCase
    )
    foreach ($dir in $prefix) {
        [void]$protoPaths.Add([IO.Path]::TrimEndingDirectorySeparator($dir))
    }

    $remaining = foreach ($part in ($env:Path -split [IO.Path]::PathSeparator)) {
        if (-not $part) { continue }
        $normalized = [IO.Path]::TrimEndingDirectorySeparator($part)
        if (-not $protoPaths.Contains($normalized)) {
            $part
        }
    }
    $env:Path = (@($prefix) + @($remaining)) -join [IO.Path]::PathSeparator
}

function Install-ExactTool {
    param(
        [string]$Tool,
        [string]$Expected
    )

    $current = if (Test-Command $Tool) { Get-VersionString -Tool $Tool } else { '' }
    if ($current -eq $Expected) {
        Write-Success "$Tool ${CORAL}v${current}${RESET} already installed"
        return
    }

    if ($current) {
        Write-Info "Upgrading ${CORAL}${Tool}${RESET} from v$current to v$Expected..."
    } else {
        Write-Info "Installing ${CORAL}${Tool}${RESET} v$Expected..."
    }

    $proto = Get-ApplicationCommand -Name 'proto'
    if (-not $proto) {
        Write-Err "proto is not an executable on PATH"
        exit 1
    }
    & $proto.Source install $Tool $Expected --pin global
    if ($LASTEXITCODE -ne 0) {
        Write-Err "$Tool v$Expected installation failed"
        exit 1
    }

    $current = if (Test-Command $Tool) { Get-VersionString -Tool $Tool } else { '' }
    if ($current -ne $Expected) {
        $resolved = if ($current) { "v$current" } else { 'missing' }
        Write-Err "$Tool v$Expected was installed but $resolved resolves on PATH"
        exit 1
    }

    Write-Success "$Tool ${CORAL}v${current}${RESET} installed"
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

function Install-VerifiedProto ([string]$Expected) {
    if ($Expected -ne $PROTO_INSTALLER_VERSION) {
        Write-Err "No verified proto installer is pinned for v$Expected"
        exit 1
    }
    $installer = Join-Path ([IO.Path]::GetTempPath()) (
        "proto-install-$([guid]::NewGuid()).ps1"
    )
    try {
        Invoke-WebRequest `
            -Uri $PROTO_INSTALLER_URL `
            -OutFile $installer `
            -UseBasicParsing `
            -ErrorAction Stop
        $actualSha256 = (Get-FileHash -Path $installer -Algorithm SHA256).Hash
        if (-not [String]::Equals(
            $actualSha256,
            $PROTO_INSTALLER_SHA256,
            [StringComparison]::OrdinalIgnoreCase
        )) {
            throw 'proto installer checksum mismatch'
        }
        & $installer
        $installerSucceeded = $?
        if (-not $installerSucceeded) {
            throw "Verified proto v$Expected installation failed"
        }
    }
    finally {
        if (Test-Path $installer) {
            Remove-Item $installer -Force -ErrorAction SilentlyContinue
        }
    }
}

function Install-Proto {
    # Make sure proto's default install dir is on PATH for this session before
    # the existence check — a previous install may not have updated this shell.
    $protoHome = if ($env:PROTO_HOME) { $env:PROTO_HOME } else { Join-Path $HOME '.proto' }
    Set-ProtoPath -ProtoHome $protoHome

    $expected = Get-RequiredVersion -Tool 'proto'
    $current = if (Test-Command proto) { Get-VersionString -Tool 'proto' } else { '' }
    if ($current -eq $expected) {
        Write-Success "proto ${CORAL}v${current}${RESET} already installed"
        return
    }

    if ($current) {
        Write-Info "Upgrading proto from v$current to v$expected..."
        $proto = Get-ApplicationCommand -Name 'proto'
        & $proto.Source upgrade $expected
        $upgradeSucceeded = $LASTEXITCODE -eq 0
        Set-ProtoPath -ProtoHome $protoHome
        $current = if (Test-Command proto) { Get-VersionString -Tool 'proto' } else { '' }
        if (-not $upgradeSucceeded -or $current -ne $expected) {
            Write-Warn "proto self-upgrade did not activate v$expected; using the verified installer"
            Install-VerifiedProto -Expected $expected
        }
    } else {
        Write-Info "Installing proto v$expected (toolchain version manager)..."
        Install-VerifiedProto -Expected $expected
    }

    # Refresh PATH for the bin we just dropped on disk.
    Set-ProtoPath -ProtoHome $protoHome

    if (-not (Test-Command proto)) {
        Write-Err "proto installation failed"
        exit 1
    }

    $current = Get-VersionString -Tool 'proto'
    if ($current -ne $expected) {
        Write-Err "proto v$expected was installed but v$current resolves on PATH"
        exit 1
    }

    # The Windows installer doesn't modify PATH or shell profiles itself; setup
    # does that. Run it so future pwsh sessions find proto without us patching
    # $PROFILE by hand. --yes accepts defaults non-interactively.
    Write-Info "Configuring proto shell integration (proto setup)..."
    try {
        $proto = Get-ApplicationCommand -Name 'proto'
        & $proto.Source setup --yes 2>&1 | Out-Host
    } catch {
        Write-Warn "proto setup reported an issue; PATH may need a new pwsh session to pick up."
    }

    Write-Success "proto ${CORAL}v${current}${RESET} installed"
}

# ═══════════════════════════════════════════════════════════════════════════════
# Moon Installation
# ═══════════════════════════════════════════════════════════════════════════════

function Install-Moon {
    Install-ExactTool -Tool 'moon' -Expected (Get-RequiredVersion -Tool 'moon')
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
    Set-ProtoPath -ProtoHome $protoHome

    Write-Info "Resolving toolchain from ${CORAL}.prototools${RESET}..."

    # Reconcile tools sequentially so pnpm always observes the pinned Node.
    $tools = @('node', 'pnpm', 'python', 'uv')
    foreach ($tool in $tools) {
        Install-ExactTool -Tool $tool -Expected (Get-RequiredVersion -Tool $tool)
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

if (-not $SkipMain) {
    Main
}
