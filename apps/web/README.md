# Sibyl Web UI

Next.js 16 admin interface for Sibyl. Built with React 19, React Query, and the SilkCircuit design
system.

## Quick Reference

```bash
# Development
moon run web:dev              # Start on :3337
moon run web:build            # Production build
moon run web:lint             # Biome check
moon run web:typecheck        # TypeScript check

# Generate API types from OpenAPI
moon run web:generate-types
```

## Features

- **Dashboard:** Stats, activity, onboarding checklist
- **Tasks:** Kanban workflow with inline editing
- **Projects & Epics:** Plan work across larger efforts
- **Graph:** Interactive force-directed visualization
- **Search:** Semantic search with filters
- **Memory:** The memory workspace, raw captures, source imports, and synthesis
- **Sources:** Documentation crawl management and document inspection
- **Settings:** Organizations, teams, API keys, security, language models, embeddings, backups, and
  preferences

## Stack

- **Framework:** Next.js 16 (App Router)
- **UI:** React 19, Tailwind CSS v4
- **State:** React Query + WebSocket
- **Design:** SilkCircuit (OKLCH-based)
- **Tooling:** Biome, Vitest, Storybook, TypeScript

## Key Directories

```
src/
├── app/
│   ├── (main)/       # Authenticated routes
│   │   ├── tasks/    # Task workflow
│   │   ├── graph/    # Visualization
│   │   ├── memory/   # Memory workspace, captures, imports, synthesize
│   │   ├── search/   # Semantic search
│   │   ├── sources/  # Crawl sources and documents
│   │   └── settings/ # Org, AI, security, backups, preferences
│   ├── login/        # Authentication
│   └── setup/        # First-run setup
├── components/
│   ├── graph/        # Force-directed visualization
│   ├── memory/       # Memory workspace components
│   ├── settings/     # AI model/provider controls
│   └── ui/           # Base components
└── lib/
    ├── api.ts        # API client (talks to /api, proxied to the backend)
    ├── hooks.ts      # React Query hooks
    └── websocket.ts  # Real-time client
```

## Configuration

The web app calls the backend through the relative `/api` base. `proxy.ts` rewrites `/api/*` to the
API server, so the browser and server share one origin. No public API URL env var is required for
the default setup.

```bash
SIBYL_WEB_PORT=3337           # Dev/start port (default 3337, set in scripts)
SIBYL_SERVER_PORT=3334        # Backend port used by generate:types
```

Port: **3337** (not 3000, to avoid conflicts).

## AI Settings

The admin AI settings page puts language model controls beside the existing embedding and API-key
controls. Language model settings are instance-wide and expose three surfaces:

- `default`: shared fallback
- `crawler`: structured extraction for crawled document chunks
- `synthesis`: generated summaries, drafts, and synthesis artifacts

Each field shows its active source: `env`, `db`, or `default`. Environment-backed fields are
disabled because the API rejects writes with `409 LOCKED_BY_ENV`. Operators can select curated
registry models, open an advanced custom-model disclosure, and run per-surface tests that show
latency, token counts, and structured failures.

Custom models are saved only after explicit confirmation and show an unverified warning. Per-org
language model overrides are a planned follow-up; this UI configures the whole instance.

## SilkCircuit Palette

```css
--sc-purple: #e135ff; /* Primary */
--sc-cyan: #80ffea; /* Interactions */
--sc-coral: #ff6ac1; /* Secondary */
--sc-green: #50fa7b; /* Success */
--sc-red: #ff6363; /* Errors */
```

Themes: Neon (default, dark), Dawn (light). See [`DESIGN_SYSTEM.md`](DESIGN_SYSTEM.md) for the full
design system.

## Testing

```bash
moon run web:test      # Vitest
pnpm storybook         # Component stories on :6006
```
