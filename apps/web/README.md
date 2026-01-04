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

- **Dashboard** — Stats, activity feed, onboarding checklist
- **Agents** — Spawn, monitor, chat with AI agents in real-time
- **Tasks** — Kanban workflow with inline editing
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
│   ├── agents/       # Agent management + chat
│   ├── tasks/        # Task workflow
│   ├── graph/        # Visualization
│   └── ...
├── components/
│   ├── agents/       # Chat system (18 components)
│   ├── graph/        # D3 visualization
│   └── ui/           # Base components
└── lib/
    ├── hooks.ts      # React Query hooks (30+)
    └── websocket.ts  # Real-time client
```

## Agent Chat Architecture

```
AgentPage
├── AgentList (sidebar)
└── ChatPanel
    ├── ChatHeader
    ├── ChatMessages
    │   ├── ToolMessage (collapsible output)
    │   ├── ApprovalRequestMessage (approve/deny)
    │   └── UserQuestionMessage (answer options)
    └── ChatInput
```

Real-time via WebSocket → invalidates React Query caches.

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
