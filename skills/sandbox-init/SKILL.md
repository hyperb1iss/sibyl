---
name: sandbox-init
description:
  Analyze project infrastructure and generate sandbox environment configurations. Detects existing
  Dockerfiles, CI configs, package managers, and Kubernetes manifests, then generates devcontainer
  and Docker Compose configs pre-wired with the Sibyl runner for distributed agent execution.
allowed-tools: Bash, Read, Grep, Glob, Write
---

# Sandbox Environment Generator

Generate sandbox and development environment configurations for a project by analyzing its existing
infrastructure.

## Workflow

### 1. Analyze Project Infrastructure

Scan the project root for existing infrastructure configuration. Check each category and note what
you find:

- **Container configs**: `Dockerfile`, `docker-compose.yml`, `docker-compose.yaml`,
  `.devcontainer/devcontainer.json`
- **CI/CD pipelines**: `.github/workflows/*.yml`, `.gitlab-ci.yml`, `Jenkinsfile`,
  `.circleci/config.yml`
- **Kubernetes**: `charts/`, `k8s/`, any YAML files containing `apiVersion:`
- **Package managers**: `pyproject.toml`, `package.json`, `Cargo.toml`, `go.mod`, `Gemfile`,
  `build.gradle`, `pom.xml`
- **Runtime version pinning**: `.nvmrc`, `.python-version`, `.tool-versions`, `rust-toolchain.toml`,
  `.ruby-version`
- **Environment files**: `.env.example`, `.env.template` (never read `.env` directly)

Use Glob to find these files quickly:

```bash
# Container configs
**/{Dockerfile,docker-compose.yml,docker-compose.yaml,.devcontainer/devcontainer.json}

# CI/CD
**/.github/workflows/*.yml
**/{.gitlab-ci.yml,Jenkinsfile,.circleci/config.yml}

# Package managers
**/{pyproject.toml,package.json,Cargo.toml,go.mod,Gemfile}

# Runtime versions
**/{.nvmrc,.python-version,.tool-versions,rust-toolchain.toml}
```

**Report what you found before proceeding.** Give the user a summary like:

> Found: Dockerfile (Python 3.12), docker-compose.yml (postgres, redis), pyproject.toml (uv
> project), .github/workflows/ci.yml

### 2. Detect Base Image

Select the base image using this priority order. Stop at the first match:

1. **Existing Dockerfile** -- extract the final `FROM` image (handle multi-stage builds; use the
   last stage's base)
2. **devcontainer.json** -- extract the `image` field
3. **CI workflow** -- extract container images from `jobs.*.container.image` or `services.*.image`
4. **Language detection** -- infer from the primary package manager found:

   | Package Manager            | Base Image               |
   | -------------------------- | ------------------------ |
   | `pyproject.toml`           | `python:3.12-slim`       |
   | `package.json`             | `node:22-slim`           |
   | `Cargo.toml`               | `rust:1-slim`            |
   | `go.mod`                   | `golang:1.23-slim`       |
   | `Gemfile`                  | `ruby:3.3-slim`          |
   | `pom.xml` / `build.gradle` | `eclipse-temurin:21-jdk` |

5. **Fallback** -- `ubuntu:24.04`

If a `.python-version`, `.nvmrc`, or `rust-toolchain.toml` exists, use the pinned version instead of
the defaults above.

### 3. Detect Install Command

Determine the post-create install command from the package manager:

| Package Manager                                      | Install Command    |
| ---------------------------------------------------- | ------------------ |
| `pyproject.toml` (with `uv.lock`)                    | `uv sync`          |
| `pyproject.toml` (with `poetry.lock`)                | `poetry install`   |
| `pyproject.toml` (plain)                             | `pip install -e .` |
| `package.json` (with `pnpm-lock.yaml`)               | `pnpm install`     |
| `package.json` (with `yarn.lock`)                    | `yarn install`     |
| `package.json` (with `package-lock.json` or default) | `npm install`      |
| `Cargo.toml`                                         | `cargo build`      |
| `go.mod`                                             | `go mod download`  |
| `Gemfile`                                            | `bundle install`   |

If multiple package managers exist (e.g., monorepo with Python + Node), chain them:
`uv sync && pnpm install`

### 4. Detect Ports

Extract port mappings from infrastructure files:

- **docker-compose.yml**: Parse `services.*.ports` entries (host:container format)
- **Dockerfile**: Parse `EXPOSE` directives
- **Common conventions**: Check for well-known service ports in compose services

| Service              | Default Port           |
| -------------------- | ---------------------- |
| PostgreSQL           | 5432                   |
| MySQL                | 3306                   |
| Redis                | 6379                   |
| FalkorDB             | 6380                   |
| MongoDB              | 27017                  |
| Elasticsearch        | 9200                   |
| HTTP APIs            | 3000, 3334, 8000, 8080 |
| Frontend dev servers | 3000, 3337, 5173       |

### 5. Detect VS Code Extensions

Map detected languages to recommended extensions:

| Language/Tool         | Extensions                                         |
| --------------------- | -------------------------------------------------- |
| Python                | `ms-python.python`, `charliermarsh.ruff`           |
| TypeScript/JavaScript | `dbaeumer.vscode-eslint`, `esbenp.prettier-vscode` |
| Rust                  | `rust-lang.rust-analyzer`                          |
| Go                    | `golang.go`                                        |
| Ruby                  | `shopify.ruby-lsp`                                 |
| Java                  | `redhat.java`                                      |
| Docker                | `ms-azuretools.vscode-docker`                      |
| YAML/Kubernetes       | `redhat.vscode-yaml`                               |

### 6. Generate .devcontainer Configuration

Create `.devcontainer/devcontainer.json` using the detected values:

```jsonc
{
  "name": "${PROJECT_NAME} Sandbox",
  "image": "${DETECTED_IMAGE}",
  "features": {
    "ghcr.io/hyperb1iss/sibyl-runner:latest": {
      "serverUrl": "${SIBYL_SERVER_URL:-http://localhost:3334}",
      "autoRegister": true
    }
  },
  "postCreateCommand": "${INSTALL_COMMAND}",
  "customizations": {
    "vscode": {
      "extensions": ["${DETECTED_EXTENSIONS}"]
    }
  },
  "forwardPorts": [${DETECTED_PORTS}],
  "remoteEnv": {
    "SIBYL_SANDBOX_MODE": "shadow"
  }
}
```

Substitute the detected values:

- `PROJECT_NAME` -- from the project's name field in `package.json`, `pyproject.toml`
  `[project.name]`, `Cargo.toml` `[package.name]`, or the directory name as fallback
- `DETECTED_IMAGE` -- from step 2
- `INSTALL_COMMAND` -- from step 3
- `DETECTED_EXTENSIONS` -- from step 5 (JSON array of strings)
- `DETECTED_PORTS` -- from step 4 (JSON array of integers)

### 7. Generate Docker Compose Sandbox Override (if applicable)

**Only generate this if the project already has a `docker-compose.yml` or `docker-compose.yaml`.**

Create `docker-compose.sandbox.yml` that adds a sibyl-runner sidecar:

```yaml
# Sandbox overlay - run with: docker compose -f docker-compose.yml -f docker-compose.sandbox.yml up
services:
  sibyl-runner:
    image: ghcr.io/hyperb1iss/sibyl-runner:latest
    environment:
      - SIBYL_SERVER_URL=${SIBYL_SERVER_URL:-http://host.docker.internal:3334}
      - SIBYL_SANDBOX_MODE=shadow
    volumes:
      - .:/workspace:cached
    depends_on:
      # List services from the main compose file that the runner needs
```

Populate `depends_on` with services from the existing compose file that look like databases or
backing services (postgres, redis, mysql, mongo, etc.).

### 8. Present Results

Show the user a clear summary:

1. **Infrastructure detected** -- bullet list of what was found
2. **Generated files** -- show each file's contents with brief explanations of key decisions
3. **Next steps** -- how to use the generated configs:
   - For devcontainer: "Open in VS Code > Reopen in Container" or "Use with GitHub Codespaces"
   - For compose overlay: `docker compose -f docker-compose.yml -f docker-compose.sandbox.yml up`
   - Connecting to Sibyl: set `SIBYL_SERVER_URL` if not using localhost

**Ask the user if they want to write the files or adjust the configuration before writing
anything.**

Do NOT write files until the user confirms. Present the generated content and wait for approval.

## Edge Cases

### No infrastructure files found

If the project has no recognizable infrastructure files, tell the user and ask what kind of project
it is. Generate a minimal devcontainer config based on their answer.

### Existing .devcontainer

If `.devcontainer/devcontainer.json` already exists, show a diff of what would change and ask
whether to merge or replace. Never silently overwrite.

### Monorepo with multiple languages

Detect all languages present. Use a multi-feature devcontainer image or suggest a custom Dockerfile.
Chain install commands. Include extensions for all detected languages.

### Private/custom base images

If the Dockerfile uses a private registry image (not Docker Hub or MCR), note this in the output and
ask the user whether the sandbox should use the same image or a public alternative.
