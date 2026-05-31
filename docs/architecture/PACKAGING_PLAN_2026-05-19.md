# Sibyl Install & Packaging Plan

**Date:** 2026-05-19 **Status:** Proposal — v3, validated against repo state and package registries
**Scope:** 1.0 launch readiness. How Sibyl reaches every audience. **Related:**
`AUDIT_2026-05-05.md`, `PACKAGING_RESEARCH_2026-05-19.md`, `decision_41e6cd8782b2` (Shared SurrealDB
topology)

---

## Goals

Make Sibyl trivially installable across the right shapes. Three product front doors plus one
developer mode, with one CLI binary that adapts via contexts.

- **Friendly** — every front door is a single command that just works. No "read the .env, install
  Docker, run the setup script, edit YAML, then..."
- **Honest** — when something fails, the tool says exactly what to do next. No silent broken paths.
- **Slim by default** — installing the CLI should not pull headless Chromium.
- **Production-true** — Docker + K8s paths are tagged, reproducible, configurable without
  hand-editing YAML.
- **CLI is THE surface** — the CLI is how agents talk to Sibyl. MCP is a fallback for clients that
  only speak MCP, not the headline.

## Validation summary (v3)

Validated on 2026-05-20 against local repo state, PyPI JSON, GHCR manifests, the Homebrew tap, and
current `uv`/`uv_build` behavior.

- `moon run python-package-build` succeeds and produces `sibyl-core`, `sibyl-dev`, and `sibyld`
  1.0.0rc1 sdists/wheels from the current workspace.
- PyPI has `sibyl-dev` 0.10.0 and `sibyl-core` 0.10.0; `sibyld` is still unpublished.
- GHCR does publish public multi-arch `sibyl-api` and `sibyl-web` images for `0.10.0` and `latest`.
  `1.0.0-rc.1` image tags do not exist yet.
- `hyperb1iss/homebrew-tap` exists and contains `git-iris.rb` and `unifly.rb`, not `sibyl.rb`.
- The local CLI already has partial contexts under `~/.sibyl/config.toml` and auth tokens under
  `~/.sibyl/auth.json`, keyed by API URL. The plan now treats contexts as hardening/migration work,
  not greenfield work.
- `sibyl-ai` is occupied on PyPI by an unrelated package. Use `sibyl-llm` or `sibyl-providers` for
  the provider split.
- `uv_build` is a good future backend for pure static-metadata packages, but the current workspace
  uses Hatchling dynamic versions from `VERSION`. Keep Hatchling until we either generate static
  versions at release time or accept per-package static versions.
- The web app currently builds as Next.js `standalone`, not static export, and uses server-side
  cookie-aware fetches. Treat FastAPI-served static web UI as an optional spike, not a launch
  dependency.

## Codex review summary (folded into v2)

Codex reviewed v1 and pushed back on six things. All folded in.

1. **Door taxonomy was muddled** — v1 had "five doors plus a sixth surface." v2 has three product
   doors + one developer mode. Local-with-server and remote-CLI-only are the same brew formula and
   same binary; what differs is the context the user picks on first run.
2. **Audit fixes ship FIRST.** The 1.0 audit lists release-block work — typed archive restore,
   fail-closed RBAC, MCP-scope fix, `surrealkv://` production guard. Packaging glamour on top of
   leaky RBAC is bad launch energy.
3. **`sibyl doctor` is a launch dependency, not a polish item.** Pulled forward in the sequence.
4. **No-config defaults to remote = trapdoor.** Fresh installs force `sibyl init` before any
   mutating command runs.
5. **Per-context-per-org tokens, not host-scoped.** A hosted instance with multiple orgs needs
   distinct credentials per `(context, org)`.
6. **Embedded-Surreal lock at driver open, not at CLI level.** The daemon owns the DB. CLI commands
   never bypass HTTP to write directly. Stale-lock recovery is explicit.

Codex also flagged the worst-day-one user story (daemon dies silently, agents write nowhere or to
the wrong org). Mitigations are baked into the implementation sequence.

---

## Design principle: remote-first CLI with contexts

The CLI is one binary in every mode. What changes per audience is the **context** — which endpoint
the CLI talks to and whether it owns the server lifecycle.

```
sibyl init                                         # interactive first-run; required before anything else
sibyl context list
sibyl context use local                            # embedded Surreal + in-process api
sibyl context use hyperbliss                       # https://sibyl.hyperbliss.tech
sibyl context create work --server https://sibyl.work.internal --org acme
sibyl context                                      # prints active endpoint + org + project
```

Current implementation stores contexts in `~/.sibyl/config.toml` (`contexts`, `active_context`) and
tokens in `~/.sibyl/auth.json` (mode 0600). Keep that path for 1.0 unless we do an explicit
compatibility migration; an XDG move is polish, not a launch blocker. Each context should record
`server_url`, local/remote mode, org/project defaults, and credential reference. Token material must
move from host/API-URL scoping to `(context_name, org_id)` scoping so a hosted instance with
multiple orgs gets distinct credentials per org per context.

This is the flyctl / `gh` shape, not the Ollama shape. Ollama assumes localhost. Sibyl assumes
nothing — local is just one context that happens to be `http://127.0.0.1:3334` and ships with an
embedded daemon. The same `sibyl task list` works against any endpoint.

Cross-cutting consequence: `sibyl serve` is a verb the user runs _inside_ the local context — it
starts the embedded daemon that the local context points at. In a remote context, `sibyl serve`
errors with a helpful message. No silent fallbacks.

**No-config behavior.** A fresh install with no config does NOT silently write to localhost or
remote. It refuses mutating commands and points at `sibyl init` (backed by today's
`sibyl config init` wizard until the top-level alias lands). Read-only commands (`sibyl --help`,
`sibyl version`) work.

**Mutating-write safety rail.** Every command that writes (e.g., `sibyl remember`,
`sibyl task create`, `sibyl add`) prints the active context, org, and project on its first
invocation per shell session, and on every invocation when `SIBYL_VERBOSE=1` or `--verbose`. This is
the "are you sure you're writing to the right place" gate.

---

## Audience Map

| Audience                             | Front Door         | Install                                                                |
| ------------------------------------ | ------------------ | ---------------------------------------------------------------------- |
| Solo dev: "I want persistent memory" | (1) Brew (local)   | `brew install hyperb1iss/tap/sibyl && sibyl init && sibyl serve`       |
| Anyone talking to a remote instance  | (2) Brew (remote)  | `brew install hyperb1iss/tap/sibyl && sibyl init --remote https://...` |
| Self-hosted on a VPS                 | (3) Docker         | `sibyl docker init && sibyl docker up`                                 |
| Org at scale                         | (4) Kubernetes     | `helm install sibyl hyperb1iss/sibyl ...`                              |
| Contributor working _on_ Sibyl       | (D) Developer mode | `./setup-dev.sh && moon run dev`                                       |

Front doors (1) and (2) share the **same brew formula and same binary**. The difference is which
context the user creates first. Today's `install.sh` collapses (1) and (3) into a confusing hybrid:
it installs a thin Python CLI then secretly orchestrates Docker containers. v2 splits them cleanly.

---

## Current state (one paragraph)

PyPI has `sibyl-dev` 0.10.0 and `sibyl-core` 0.10.0 published. `sibyld` builds locally at `1.0.0rc1`
but is **not published** despite the README telling users to `uv tool install sibyld`. `VERSION` is
`1.0.0-rc.1`. `Formula/sibyl.rb` is 0.2.0 with `PLACEHOLDER_SHA256` on every resource; its Apache-2.0 license
now matches the relicensed tree. `install.sh` hard-requires Docker, then installs only the CLI and
orchestrates containers pulled from `ghcr.io/hyperb1iss/sibyl-{api,web}:latest`. GHCR has public
multi-arch `0.10.0`/`latest` images, but no `1.0.0-rc.1` tags yet. `setup-dev.sh` still references
FalkorDB and PostgreSQL as "required databases." Three artifact sources, three different versions.

**PyPI name reality:**

- `sibyl` — squatted (unmaintained "sibyl forecaster" v0.1).
- `sibyl-cli` — squatted (unrelated Codex/tmux tool, v0.2.3).
- `sibyl-dev`, `sibyl-core` — ours.
- `sibyl-app`, `sibyl-mcp`, `sibyl-server`, `sibyl-graph`, `sibyl-crawler`, `sibyld` — available as
  of validation.
- `sibyl-ai` — occupied by an unrelated AutoML wrapper. Do not use.

`hyperb1iss/homebrew-tap` exists with `git-iris.rb` and `unifly.rb` — no Sibyl formula yet. Publish
workflow builds and tags `sibyl-api`/`sibyl-web` as `<VERSION>` and `latest`; the remaining fix is
wiring consumers away from `latest` and proving each release tag exists before announcing it.

---

## Package & binary naming

The Unix split. `sibyl` is the CLI binary. `sibyld` is the daemon binary. Two binaries, one brew
formula.

| Layer        | CLI                                                                            | Daemon                 | Shared library           |
| ------------ | ------------------------------------------------------------------------------ | ---------------------- | ------------------------ |
| Binary       | `sibyl`                                                                        | `sibyld`               | n/a                      |
| PyPI package | `sibyl-dev` (published)                                                        | `sibyld` (publish new) | `sibyl-core` (published) |
| Brew formula | `brew install hyperb1iss/tap/sibyl` (bundles both binaries + embedded Surreal) | —                      | resource in formula      |

**Why this shape:**

- `sibyl` and `sibyl-cli` PyPI names are both squatted. Reclaim is a parallel track; don't block.
- We control brew naming via our tap. `brew install hyperb1iss/tap/sibyl` is clean regardless of the
  PyPI bricolage.
- `sibyl` (CLI) / `sibyld` (daemon) follows the Unix convention (`ssh`/`sshd`, `docker`/`dockerd`).
  It reads naturally and gives power users a clear mental model.
- One brew formula at launch — installs CLI + daemon + embedded Surreal. No separate `sibyl-cli`
  formula. Codex called out that two formulae add confusion for marginal benefit; the slim CLI
  install only matters to users counting MB on a remote-only deployment, and they have
  `uv tool install sibyl-dev` as the slim path.

**Dependency graph after the `sibyl-core` split:**

```
sibyl-core        # models, base utils, JWT, HTTP helpers              ~10 MB    (published)
sibyl-graph       # surreal client + ops + retrieval                   ~30 MB    (NEW)
sibyl-crawler     # crawl4ai + Playwright deps                         optional  (NEW)
sibyl-llm         # pydantic-ai providers                              optional  (NEW; name available)
sibyl-server      # FastAPI + orchestration                            (NEW)
sibyl-dev         # the CLI (sibyl binary)                             ~20 MB    (published)
sibyld            # the daemon (sibyld binary)                         (publish)
```

After split:

- `uv tool install sibyl-dev` → ~20MB, no Playwright, no crawl4ai. Slim path for remote-only.
- `uv tool install sibyld` → full daemon. Add `[crawler]` for Chromium when needed.
- `brew install hyperb1iss/tap/sibyl` → bundles `sibyl-dev` + `sibyld` in one venv. Both binaries on
  PATH. The headline path.

---

## Domain & install-script hosting

`sibyl.dev` is gone (Cloudflare-fronted, registered). Many likely TLDs also gone: `.app`, `.ai`,
`.is`, `.io`, `.run`, `.tools`, `.fyi`, `.systems`.

**Likely available right now** (WHOIS/DNS quick scan; verify with the registrar before committing):

- `sibyl.lol` — irreverent, memorable. Works with `install.sibyl.lol`.
- `sibyl.gg` — gamer/electric feel, on-brand for the SilkCircuit aesthetic.
- `sibyl.so` — startup-y, clean.
- `sibyl.computer` — brutalist meta. `install.sibyl.computer` is a vibe.
- `sibyl.wtf`, `sibyl.cool`, `sibyl.fun` — more irreverent.

**Recommendation for 1.0:** `sibyl.gg`. Short, likely available, easy to type. Use
`install.sibyl.gg` as the curl-script URL once DNS lands. Fall back to
`raw.githubusercontent.com/hyperb1iss/sibyl/main/install.sh` until then.

`sibyl.hyperbliss.tech` stays as Bliss's hosted instance. `sibyl.gg` (or whichever) is the project
domain — landing page, docs, install script.

If we want a more "serious" alternative, `sibyl.so` or `sibyl.computer` reads better in enterprise
contexts. `.gg` is the playful pick.

---

## The Front Doors

### Front Door 1 + 2: Brew install

**Audience (1):** solo dev who wants persistent memory for their coding agents. **Audience (2):**
anyone connecting to a remote Sibyl (team-hosted, Bliss-hosted, etc.). **Status:** does not exist
yet. Highest leverage piece of work.

**Same brew install, two contexts.** First-run wizard asks the user which one they want.

```
brew install hyperb1iss/tap/sibyl      # one-time, same for both audiences
sibyl init                              # new top-level alias; wraps expanded config init

# Audience 1 (local bundled):
sibyl serve                             # starts embedded daemon, embedded SurrealDB
sibyl serve --web                       # also serves the web UI on :3337
sibyl doctor                            # health
sibyl stop                              # stop the daemon, data persists

# Audience 2 (remote CLI):
sibyl init --remote https://sibyl.hyperbliss.tech
                                        # device-flow auth (RFC 8628, already implemented)
sibyl auth status                       # whoami alias can come later
sibyl task list                         # routed to the remote
```

**What runs (local context):**

- Two binaries on PATH: `sibyl` (CLI) and `sibyld` (daemon).
- **Embedded SurrealDB** in RocksDB mode at `~/.sibyl/data/`. No Surreal binary required.
- API + worker in-process inside `sibyld`. `SIBYL_COORDINATION_BACKEND=local`.
- Web UI optional. Current app is Next.js `standalone` with server-side auth fetches. A
  FastAPI-served static export needs a focused spike before it becomes the brew default.
- No Docker. No Chromium. No Postgres.

**Embedded-Surreal safety model (Codex pushback folded in):**

Per `AUDIT_2026-05-05.md` item 2, `surrealkv://` silently corrupts under multi-writer access. The
audit fix forbids it in production. For the brew-install local path, the design is:

- **`sibyld` owns the DB.** Lock acquired at SurrealDB driver open, not at CLI level. Held for the
  daemon's lifetime.
- **CLI never writes directly.** All `sibyl remember`, `sibyl add`, `sibyl task create` go over HTTP
  to localhost `sibyld`. No "direct embedded" code path in the CLI.
- **Stale-lock recovery is explicit.** If `sibyld` crashed, `sibyl serve` detects the stale lock,
  verifies no live process holds it (PID check + age check), and re-acquires. Never silently
  override an active lock.
- **`SIBYL_ALLOW_EMBEDDED_SINGLE_WRITER=1`** is set by `sibyld` itself, not by the user. The
  production guard from audit item 2 stays in place for actual production deploys.
- **`sibyl doctor`** explicitly checks: lockfile state, daemon liveness, write-test against
  localhost. The worst-day-one story (daemon died, writes silently failing) is detected here.

**Brew install mechanism:**

Three ways to ship `brew install`:

| Approach                                                                                            | Pro                                                              | Con                                                                                |
| --------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------- | ---------------------------------------------------------------------------------- |
| **Homebrew Python virtualenv** — formula declares Python + resources, brew builds a venv at install | Proven for Python tools, Python toolchain visible, easy to debug | Tied to Homebrew's Python; resource list to maintain                               |
| **Native single binary** (PyApp / PyOxidizer / Nuitka)                                              | True single binary, no Python required                           | PyApp viable but unproven; PyOxidizer/Nuitka still rough; build matrix per OS/arch |
| **Shim binary** (Rust/Go vendors `uv` on first run)                                                 | One executable, no Homebrew Python coupling                      | New maintenance burden; not our stack                                              |

**Recommendation: Homebrew Python virtualenv first.** Lowest-risk way to land
`brew install hyperb1iss/tap/sibyl` against `VERSION=1.0.0-rc.1`. Native single-binary is a
follow-up via PyApp once the slim-core split lands and the dep surface stops moving. Skip
Nuitka/PyOxidizer — research surveyed them as still painful in 2026.

`uv tool install sibyl-dev` stays as the parallel Python-toolchain install for users who'd rather
not touch Homebrew. Do not migrate the current packages to `uv_build` as part of this plan;
Hatchling is carrying dynamic versioning from root `VERSION`. Revisit `uv_build` only after we
either generate static package versions during release or remove dynamic metadata.

### Front Door 3: Docker

**Audience:** "I have a VPS, I want to host this for my team without Kubernetes." **Status:** works
but rough.

**What runs:** `ghcr.io/hyperb1iss/sibyl-api:<tag>`, `sibyl-web:<tag>`, `surrealdb:v3.0.5`, optional
Valkey + worker profile.

**Fixes:**

- **Pin tags.** Replace `:latest` in `apps/cli/src/sibyl_cli/local.py:57,95,116` and
  `docker-compose.quickstart.yml`. Use `<VERSION>`.
- **Verify publish per version.** `0.10.0` and `latest` exist publicly for both images; `1.0.0-rc.1`
  does not. Release automation must fail if the just-cut `<VERSION>` image tags are missing.
- **`sibyl docker init`.** Interactive bootstrap: pick ports, generate secrets, write `.env` and
  pinned compose to `~/.sibyl/docker/`. No more "edit `.env.example` and pray."
- **Default to no-Redis.** `SIBYL_COORDINATION_BACKEND=local` works in-container for single-host
  deployments. Redis/Valkey is opt-in via `--with-worker`.
- **Shrink the image.** Crawl4ai + Playwright + Chromium add ~600MB. Split into `sibyl-api` (core)
  and `sibyl-api-crawler` (heavy). User opts in via `--with-crawler`.

**Commands:**

```
sibyl docker init       # generate .env + compose in ~/.sibyl/docker
sibyl docker up         # docker compose up -d
sibyl docker logs       # follow
sibyl docker down       # stop
sibyl docker upgrade    # pull new tag, restart
```

### Front Door 4: Kubernetes / Helm

**Audience:** orgs at scale, including `sibyl.hyperbliss.tech`. **Status:** chart exists at
`charts/sibyl`.

**Fixes:**

- Default values that work out of the box for a single-namespace install.
- Documented secrets/ingress story for cert-manager + Kong (mirrors `infra/local`).
- A worked example in `docs/deployment/kubernetes.md` that matches the real `sibyl.hyperbliss.tech`
  topology.
- Helm chart `appVersion` wired to `VERSION` via CI.
- Optional: publish the chart to `hyperb1iss/helm-charts` repo and `helm repo add` it.

### Developer Mode (D): `moon run dev`

**Audience:** people working _on_ Sibyl. **Status:** works today; polish needed. This was "front
door 1" and "front door 3" in v1; they're really one mode.

**What runs:** API + worker on host, Surreal in Docker via `tools/dev/run-surreal-dev.sh`, web on
host, in-process coordination. The default. A `moon run dev --no-docker` mode is future work once
the embedded path is solid; it does not exist today.

**Fixes (small):**

- Strip FalkorDB/Postgres lines from `setup-dev.sh:182,264`. Replace the FalkorDB port summary with
  the current ports.
- Make `verify_cli` actually run `moon run install-dev` or change the prompt to say CLIs are run
  through `uv run`. Today it only verifies existing `.venv/bin/sibyl` and `.venv/bin/sibyld`.
- Add `moon run doctor` — toolchain + Docker + ports + Python version check.

---

## Package architecture changes

### Slim `sibyl-core`

`packages/python/sibyl-core/pyproject.toml` currently pins `google-genai`, `crawl4ai`, `mistune`,
`python-louvain`, `surrealdb`, and `pydantic-ai-slim[anthropic,google,openai]` unconditionally. Move
these to extras and/or the new runtime packages, then re-thread imports lazily. The CLI install
should drop from hundreds of MB to a slim HTTP-client footprint. Verify the actual installed-size
target after the split rather than treating `~20MB` as guaranteed.

### Single VERSION truth

`VERSION` at root is the only source. CI bumps it via `git-iris`. Every artifact computes from
there:

- PyPI: `sibyl-core`, `sibyl-graph`, `sibyl-crawler`, `sibyl-llm`, `sibyl-server`, `sibyl-dev`,
  `sibyld` all share `VERSION` once those packages exist.
- GHCR: `sibyl-api:<VERSION>` and `sibyl-web:<VERSION>`. `latest` is a moving alias only, never a
  release target.
- Helm: `chart.yaml` `appVersion` set by CI.
- Homebrew: tap formula generated from `VERSION` + the released sdist SHA256s.

Today: `VERSION=1.0.0-rc.1`, PyPI=0.10.0, GHCR=0.10.0/`latest`, Helm chart `appVersion=0.1.0`,
Formula=0.2.0. Next release lands all artifact versions together.

### Homebrew tap

`hyperb1iss/homebrew-tap` already has `git-iris.rb` and `unifly.rb`. Add `Formula/sibyl.rb`
generated from CI release artifacts. Surfaces as `brew install hyperb1iss/tap/sibyl`. Delete the
broken `Formula/sibyl.rb` in this repo after migrating to the tap.

---

## Worst day-one user story

Codex flagged this. The mitigations are baked into the implementation sequence.

**The story:** Brew install works. `sibyl serve` runs as a background daemon (launchd plist on
macOS, systemd user unit on Linux). The daemon dies silently (OOM, crash, port conflict, lock
acquisition failure). Agent fires `sibyl remember "..."` repeatedly.

**Wrong outcomes that the design must rule out:**

- Silent fallback to writing through embedded Surreal locally → multi-writer corruption.
- Silent fallback to a "remote" context with no auth → 401s the agent ignores.
- CLI exits 0 with no work done → agent thinks the write succeeded.

**Right outcome:** CLI prints a clear "your local daemon is down. Run `sibyl serve` or
`sibyl doctor`." Refuses to write. Exit 1.

**Mitigations bundled into the sequence:**

- `sibyl doctor` (item 4 in sequence) detects daemon-down and stale-lock states.
- Contexts (item 5) make "where am I writing" explicit and printable.
- Embedded mode (item 6) refuses fallback paths. The CLI has no "direct embedded" code path; all
  writes go HTTP-to-localhost.
- Service supervisor: launchd plist (macOS) or systemd user unit (Linux) for daemon lifecycle.
  `sibyl serve --foreground` for users who want a single-process visible thing.

---

## Implementation Sequence

Reordered per Codex feedback: 1.0 audit blockers first, then versioning/publish hygiene, then UX
work.

**0. AUDIT_2026-05-05 release blockers (L, prerequisite).** From the audit's "must ship": typed
archive restore (audit item 3), fail-closed RBAC + sidecar backfill (audit items 1, 2, 7), MCP
scope + API-key UUID/graph-ID fix (audit items 4, 5), production guardrails including `surrealkv://`
rejection (audit item 6). Nothing in this plan ships until these do. Tracked separately under the
`Hardening & Focus` epic.

**1. Slim `sibyl-core` (M).** Move `google-genai`, `crawl4ai`, `mistune`, `python-louvain`,
`pydantic-ai-slim[anthropic,google,openai]`, and `surrealdb` into `[project.optional-dependencies]`
and/or the new runtime packages, then re-thread imports lazily. Verify the actual CLI install size
after the split.

**2. Unify VERSION + pin all `:latest` consumers to tags + verify GHCR publish (S).** Pulled forward
per Codex; cheap, removes launch ambiguity. Coordinate PyPI + GHCR + Helm + Formula version off one
VERSION file. Keep publishing `latest` as a convenience alias, but never use it in release docs,
generated compose files, Helm defaults, or the CLI-managed Docker path.

**3. Publish `sibyld` to PyPI (S).** Today the package builds but is unpublished. README is lying.
Fix.

**4. `sibyl doctor` (S).** Pulled forward per Codex. Validates contexts file, surreal reachability,
daemon health, port conflicts, embedded-lock state. Must exist before the brew install ships, not
after.

**5. Harden and migrate the contexts system (M).** Existing pieces:
`sibyl context list/show/create/use/update/delete/clear`, `--context`, `SIBYL_CONTEXT`, and
`sibyl config init`. Missing launch pieces: top-level `sibyl init`, local-vs-remote wizard, force
first-run init for mutating commands, write-confirmation rail printing context+org+project, and
per-context-per-org token scoping.

**6. Build the self-contained host mode (L).** `sibyld` embedded SurrealDB (RocksDB), driver-level
lock, stale-lock recovery, daemon-owns-DB invariant, CLI-never-bypasses-HTTP. Service supervisor
(launchd plist, systemd user unit). Web UI optional after deciding between Next.js standalone and a
validated static-export path.

**7. Ship the Homebrew tap (S, after #6).** Real `sibyl.rb` in `hyperb1iss/homebrew-tap`, generated
from CI release artifacts. Bundles CLI + daemon + embedded Surreal. Delete the broken
`Formula/sibyl.rb` in this repo. Update README install section.

**8. `sibyl docker init` UX (M).** Interactive bootstrap; pinned compose; `sibyl docker upgrade`;
`--with-crawler` flag for the heavy image.

**9. CLI auth UX polish (S).** Current surface is `sibyl auth login`; device flow is implemented.
Add top-level aliases only if they sharpen UX: `sibyl login <url>`, `sibyl logout`, `sibyl whoami`.
Add the curl-script alternative at `install.sibyl.gg` (or whichever TLD) with checksums,
Coolify-style.

**10. Helm chart polish (M).** Default values matching `sibyl.hyperbliss.tech`, secrets template,
ingress example, optional helm-charts repo publish.

**11. Native single-binary path via PyApp (M, defer).** Optional follow-up once 1–10 settle. Single
`sibyl` executable for users without a Python toolchain. Ship via GitHub Releases.

**Effort estimate:** Items 1–9 are ~3–4 weeks of focused work after audit-fix prerequisite lands. 10
is ongoing self-host polish. 11 is post-1.0.

---

## What we defer

- PyPI reclaim of bare `sibyl` and `sibyl-cli`. Parallel track via squatter policy; don't block.
- Windows native support. WSL2 first; native Windows is a separate effort.
- Apt/RPM/Nix/AUR packages. Until brew + Docker + PyPI are solid, distro packages add maintenance
  for marginal audience.
- Separate `sibyl-cli` brew formula. One formula at launch; add a slim formula only if user demand
  materializes.
- Native single-binary (PyApp spike) — post-launch.
- `sibyl signup` against the hosted service — v1.1.

---

## Open questions

- **TLD pick.** `.gg`, `.lol`, `.so`, `.computer`, or fall back to `sibyl.hyperbliss.tech`. My pick:
  `.gg`. Awaiting Bliss.
- **Service supervisor on macOS/Linux.** Native (launchd plist + systemd user unit) vs in-process
  foreground vs `nohup`-style. Tailscale and Coolify use native supervisors. Lean toward native.
- **Web UI bundling.** Static export inside the brew formula (~50MB added) vs `sibyl serve --web`
  spawning a Node process. Static export is friendlier; one fewer dependency.
- **First-run wizard scope.** Just context selection, or also: detect coding agents on the box and
  print shell-snippet wiring for each (Claude Code, Codex, Cursor) without trying to auto-edit their
  configs? Bias toward print-don't-edit, per "CLI is THE surface" — Sibyl tells agents how to call
  it; agents don't get magic-wired.
- **`sibyl serve` default lifecycle.** Foreground or background? Foreground is more transparent for
  first-time users ("close the terminal, lose memory"). Background is what production wants.
  Default: foreground; `sibyl serve --background` or `sibyl start` for daemonized.
