# Sibyl Web UI

Next.js 16 admin interface for Sibyl. Built with React 19, React Query, and the SilkCircuit design system.

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

- **Dashboard** — Stats, activity, onboarding checklist
- **Tasks** — Kanban workflow with inline editing
- **Projects & Epics** — Plan work across larger efforts
- **Graph** — Interactive D3 visualization
- **Search** — Semantic search with filters
- **Sources** — Documentation crawl management
- **Settings** — Org, API keys, preferences

## Stack

- **Framework:** Next.js 16 (App Router)
- **UI:** React 19, Tailwind CSS v4
- **State:** React Query + WebSocket
- **Design:** SilkCircuit (OKLCH-based)
- **Tooling:** Biome, Vitest, TypeScript

## Key Directories

```
src/
├── app/(main)/       # Authenticated routes
│   ├── tasks/        # Task workflow
│   ├── graph/        # Visualization
│   └── ...
├── components/
│   ├── graph/        # D3 visualization
│   └── ui/           # Base components
└── lib/
    ├── hooks.ts      # React Query hooks
    └── websocket.ts  # Real-time client
```

## Configuration

```bash
# .env.local
NEXT_PUBLIC_API_URL=http://localhost:3334
```

Port: **3337** (not 3000, to avoid conflicts)

## SilkCircuit Palette

```css
--sc-purple: #e135ff;   /* Primary */
--sc-cyan: #80ffea;     /* Interactions */
--sc-coral: #ff6ac1;    /* Secondary */
--sc-green: #50fa7b;    /* Success */
--sc-red: #ff6363;      /* Errors */
```

Themes: Neon (default), Vibrant, Soft, Glow, Dawn (light)

## Testing

```bash
pnpm test              # Vitest
pnpm storybook         # Component stories on :6006
```
