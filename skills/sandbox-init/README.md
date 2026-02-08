# sandbox-init

A Claude Code skill that analyzes project infrastructure and generates sandbox environment
configurations for use with Sibyl's distributed execution system.

## What it does

When invoked, this skill:

1. **Scans** the project for existing Dockerfiles, CI configs, package managers, and k8s manifests
2. **Detects** the appropriate base image, install commands, ports, and toolchain requirements
3. **Generates** `.devcontainer/devcontainer.json` with sibyl-runner pre-wired
4. **Optionally generates** `docker-compose.sandbox.yml` overlay for sidecar deployment
5. **Presents** results for user review before writing any files

## Usage

Invoke in Claude Code:

```
/sandbox-init
```

Or describe what you need:

```
"Set up a sandbox environment for this project"
"Generate devcontainer config with Sibyl runner"
```

## Generated Files

| File                              | Purpose                                    |
| --------------------------------- | ------------------------------------------ |
| `.devcontainer/devcontainer.json` | VS Code / Codespaces dev container config  |
| `docker-compose.sandbox.yml`      | Docker Compose overlay with runner sidecar |

## How it works

The skill walks through a detection pipeline:

1. **Infrastructure scan** -- finds Dockerfiles, compose files, CI configs, package managers
2. **Base image selection** -- picks the right image from existing configs or language detection
3. **Install command** -- determines the correct dependency install command
4. **Port detection** -- extracts exposed ports from compose/Dockerfile/conventions
5. **Extension mapping** -- maps detected languages to VS Code extensions
6. **Config generation** -- produces devcontainer.json and optional compose overlay
7. **User review** -- presents everything before writing files

## Requirements

- Project should have at least one recognizable infrastructure or package manager file
- Sibyl API running at the configured URL (for runner registration)

## See Also

- [Runner Documentation](../../apps/runner/README.md)
- [Sandbox Dockerfile](../../apps/runner/Dockerfile.sandbox)
