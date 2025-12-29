# Sibyl Web UI

Next.js 16 admin interface for the Sibyl knowledge graph. Built with React 19, React Query, and the
SilkCircuit design system.

## Overview

The web UI provides a complete admin interface for Sibyl:

- **Dashboard** — Stats overview, recent activity, quick actions
- **Tasks** — Kanban-style workflow, filters, bulk operations
- **Projects** — Project management, task grouping, progress tracking
- **Epics** — Feature-level organization spanning multiple tasks
- **Entities** — Browse all knowledge types with rich filtering
- **Graph** — Interactive visualization of entity relationships
- **Search** — Semantic search with type and status filters
- **Sources** — Documentation source configuration and crawl management
- **Settings** — Organization, API keys, user preferences

**Note:** This is an admin interface, not a chat UI. It's for managing knowledge, not conversing.

## Stack

- **Framework:** Next.js 16 with App Router
- **UI:** React 19, Tailwind CSS v4
- **State:** React Query (TanStack Query) for server state
- **Real-time:** WebSocket for live updates
- **Design:** SilkCircuit design system (OKLCH-based)
- **Tooling:** Biome (lint/format), Vitest (testing), TypeScript

## Structure

```
src/
├── app/                      # Next.js App Router
│   ├── layout.tsx            # Root layout + providers
│   ├── globals.css           # SilkCircuit design tokens
│   ├── (main)/               # Authenticated routes
│   │   ├── page.tsx          # Dashboard
│   │   ├── tasks/            # Task management
│   │   ├── projects/         # Project views
│   │   ├── epics/            # Epic management
│   │   ├── entities/         # Entity browser
│   │   ├── graph/            # Graph visualization
│   │   ├── search/           # Search interface
│   │   ├── sources/          # Source management
│   │   └── settings/         # Settings pages
│   └── login/                # Login page
│
├── components/
│   ├── ui/                   # Base components (Button, Card, Dialog, etc.)
│   ├── layout/               # Header, Sidebar, Breadcrumb, etc.
│   ├── tasks/                # Task-specific components
│   ├── entities/             # Entity display components
│   ├── epics/                # Epic components
│   ├── graph/                # Graph visualization
│   ├── search/               # Search components
│   ├── sources/              # Source management
│   ├── editable/             # Inline editing components
│   ├── onboarding/           # First-run wizard
│   └── metrics/              # Charts and stats
│
└── lib/
    ├── api.ts                # Client-side API (fetch wrapper)
    ├── api-server.ts         # Server-side API (with caching)
    ├── hooks.ts              # React Query hooks (25+)
    ├── constants.ts          # Entity configs, colors, enums
    ├── websocket.ts          # WebSocket client
    └── theme.tsx             # Theme provider
```

## Development

### moonrepo Tasks

```bash
moon run web:dev              # Start dev server on :3337
moon run web:build            # Production build
moon run web:start            # Start production server
moon run web:lint             # Biome check
moon run web:lint-fix         # Biome fix
moon run web:typecheck        # TypeScript check
moon run web:test             # Run Vitest
moon run web:test-watch       # Vitest watch mode
moon run web:storybook        # Start Storybook on :6006
moon run web:generate-types   # Generate API types from OpenAPI
```

### Direct Commands

```bash
pnpm dev                      # Start dev server
pnpm build                    # Production build
pnpm start                    # Start production server
pnpm lint                     # Biome check
pnpm lint:fix                 # Biome fix
pnpm typecheck                # TypeScript check
pnpm test                     # Run tests
pnpm test:watch               # Test watch mode
pnpm storybook                # Start Storybook
pnpm generate:types           # Generate API types
```

## Configuration

### Environment

```bash
# .env.local
NEXT_PUBLIC_API_URL=http://localhost:3334  # Sibyl API server
```

The API URL is rewritten via `next.config.ts`:

- `/api/*` → proxied to backend
- Direct calls to `NEXT_PUBLIC_API_URL` for client-side fetches

### Port

Dev server runs on **port 3337** (not 3000) to avoid conflicts.

## SilkCircuit Design System

### Color Palette

```css
/* Core Neon Palette */
--sc-purple: #e135ff; /* Primary actions, importance */
--sc-cyan: #80ffea; /* Interactions, highlights */
--sc-coral: #ff6ac1; /* Secondary, data */
--sc-yellow: #f1fa8c; /* Warnings, attention */
--sc-green: #50fa7b; /* Success states */
--sc-red: #ff6363; /* Errors, danger */

/* Background Hierarchy */
--sc-bg-dark: #0a0812; /* Main background */
--sc-bg-base: #12101a; /* Cards, elevated surfaces */
--sc-bg-highlight: #1a162a; /* Hover states */
--sc-bg-elevated: #221e30; /* Modals, dropdowns */
```

### Theme Variants

SilkCircuit supports multiple intensity levels:

- **Neon** (default) — Full intensity for dark environments
- **Vibrant** — High energy, slightly tamed
- **Soft** — Reduced chroma for extended use
- **Glow** — Maximum contrast for accessibility
- **Dawn** — Light theme for bright environments

Design tokens are defined in `src/app/globals.css`.

## API Integration

### Server-Side Fetching

```typescript
// In server components
import { serverFetch } from "@/lib/api-server";

const stats = await serverFetch<Stats>("/admin/stats", {
  next: { revalidate: 60, tags: ["stats"] },
});
```

### Client-Side Fetching

```typescript
// Using React Query hooks
import { useStats, useTasks, useEntities } from "@/lib/hooks";

function Dashboard() {
  const { data: stats } = useStats();
  const { data: tasks } = useTasks({ status: "doing" });
  return <>{/* render */}</>;
}
```

### Available Hooks

| Hook               | Purpose                      |
| ------------------ | ---------------------------- |
| `useStats`         | Dashboard statistics         |
| `useTasks`         | Task list with filters       |
| `useTask`          | Single task                  |
| `useTaskMutations` | Start, complete, block, etc. |
| `useProjects`      | Project list                 |
| `useProject`       | Single project               |
| `useEpics`         | Epic list                    |
| `useEntities`      | Entity list with filters     |
| `useEntity`        | Single entity                |
| `useSearch`        | Semantic search              |
| `useSources`       | Documentation sources        |
| `useGraph`         | Graph data for visualization |
| `useCommunities`   | Community clusters           |
| `useOrganizations` | User's organizations         |
| `useCurrentUser`   | Authenticated user           |
| `useApiKeys`       | API key management           |

### WebSocket Updates

Real-time updates via WebSocket invalidate React Query caches:

```typescript
import { useWebSocket } from "@/lib/websocket";

// In providers.tsx
useWebSocket({
  onEntityCreated: () => queryClient.invalidateQueries(["entities"]),
  onTaskUpdated: () => queryClient.invalidateQueries(["tasks"]),
});
```

## Component Patterns

### Page Structure

```typescript
// Server component for initial data
export default async function TasksPage() {
  const tasks = await serverFetch<Task[]>("/tasks");
  return <TaskList initialData={tasks} />;
}

// Client component with hydration
"use client";
function TaskList({ initialData }) {
  const { data } = useTasks({ initialData });
  return <>{/* render with data */}</>;
}
```

### Editable Fields

Inline editing with optimistic updates:

```typescript
import { EditableText, EditableSelect } from "@/components/editable";

<EditableText
  value={task.name}
  onSave={(value) => updateTask({ name: value })}
/>

<EditableSelect
  value={task.status}
  options={statusOptions}
  onSave={(value) => updateTask({ status: value })}
/>
```

### UI Components

Base components in `components/ui/`:

- `Button`, `IconButton`
- `Card`, `Panel`
- `Dialog`, `Sheet`
- `Select`, `Input`, `Textarea`
- `Tabs`, `Accordion`
- `Badge`, `Avatar`
- `Tooltip`, `Popover`
- `DataTable` (with sorting, filtering)
- `Skeleton` (loading states)

## Testing

### Running Tests

```bash
pnpm test                     # Run all tests
pnpm test:watch               # Watch mode
pnpm test:coverage            # With coverage
pnpm test:ui                  # Vitest UI
```

### Test Patterns

```typescript
import { render, screen } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";

test("renders task card", () => {
  render(
    <QueryClientProvider client={queryClient}>
      <TaskCard task={mockTask} />
    </QueryClientProvider>
  );
  expect(screen.getByText(mockTask.name)).toBeInTheDocument();
});
```

## Type Generation

Generate TypeScript types from the API's OpenAPI spec:

```bash
# Start API server first, then:
moon run web:generate-types
# or
pnpm generate:types
```

This updates `src/lib/api-types.ts` with types matching the API schema.

## Storybook

Component documentation and visual testing:

```bash
moon run web:storybook        # Start on :6006
moon run web:build-storybook  # Build static site
```

Stories are co-located with components: `Button.stories.tsx` next to `Button.tsx`.

## Key Files

| File                       | Purpose                          |
| -------------------------- | -------------------------------- |
| `app/layout.tsx`           | Root layout, providers           |
| `app/globals.css`          | SilkCircuit design tokens        |
| `lib/api.ts`               | Client-side API wrapper          |
| `lib/api-server.ts`        | Server-side API with caching     |
| `lib/hooks.ts`             | All React Query hooks            |
| `lib/constants.ts`         | Entity configs, colors, enums    |
| `lib/websocket.ts`         | WebSocket client                 |
| `components/providers.tsx` | Query + WebSocket providers      |
| `next.config.ts`           | API rewrites, experimental flags |

## Browser Automation

For testing UI flows, use the Next.js DevTools MCP:

```typescript
// Initialize context
mcp__next_devtools__init({ project_path: "/path/to/sibyl/apps/web" });

// Evaluate in browser
mcp__next_devtools__browser_eval({
  action: "evaluate",
  script: "document.title",
});
```
