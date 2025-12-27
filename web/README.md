# Sibyl Web UI

Admin interface for managing the Sibyl knowledge graph. Built with Next.js 16 and the SilkCircuit
design system.

## Purpose

This is **not** a chat interface. The web UI provides:

- **Knowledge Graph Explorer** — Visualize and navigate entities and relationships
- **Task Management** — Create, organize, and track tasks with workflow states
- **Project Dashboard** — Monitor project progress and task dependencies
- **Source Management** — Configure and monitor documentation sources
- **Search Interface** — Semantic search across all knowledge types

## Stack

- **Framework**: Next.js 16 with App Router
- **Styling**: Tailwind CSS v4 with SilkCircuit design tokens
- **State**: React Query for server state
- **API**: REST endpoints from the Sibyl Python backend

## Design System

Uses [SilkCircuit](../../conventions/shared/STYLE_GUIDE.md) — an OKLCH-based color system with five
variants:

- **Neon** (default) — Full intensity for dark environments
- **Vibrant** — High energy, slightly tamed
- **Soft** — Reduced chroma for extended use
- **Glow** — Maximum contrast for accessibility
- **Dawn** — Light theme for bright environments

Design tokens are defined in `src/app/globals.css`.

## Getting Started

```bash
# Install dependencies
pnpm install

# Start dev server (requires Sibyl backend running)
pnpm dev

# Build for production
pnpm build

# Type check
pnpm typecheck
```

## Structure

```
src/
├── app/                  # Next.js App Router pages
│   ├── page.tsx          # Dashboard
│   ├── search/           # Search interface
│   ├── tasks/            # Task management
│   ├── projects/         # Project views
│   ├── sources/          # Source configuration
│   └── graph/            # Graph visualization
├── components/           # Reusable UI components
│   ├── ui/               # Base components (Button, Card, etc.)
│   └── features/         # Feature-specific components
├── lib/                  # Utilities and API client
└── hooks/                # Custom React hooks
```

## Environment

```bash
# .env.local
NEXT_PUBLIC_API_URL=http://localhost:3334  # Sibyl backend
```
