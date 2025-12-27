---
name: sibyl-frontend
description:
  Next.js 16 frontend development for Sibyl including React Query hooks, SilkCircuit design system,
  server/client components, and WebSocket integration. Use when building UI components, pages, or
  data fetching.
---

# Sibyl Frontend Development

## Architecture

```
web/src/
├── app/              # Next.js 16 App Router
│   ├── layout.tsx    # Root layout with providers
│   ├── page.tsx      # Dashboard
│   └── */page.tsx    # Feature pages
├── components/
│   ├── ui/           # Primitives (button, card, spinner)
│   ├── layout/       # Sidebar, header, breadcrumb
│   └── */            # Domain components
└── lib/
    ├── api.ts        # Client-side fetcher
    ├── api-server.ts # Server-side with caching
    ├── hooks.ts      # React Query hooks
    └── constants.ts  # Colors, entity configs
```

## Server/Client Boundary

```tsx
// app/entities/page.tsx (Server Component)
export default async function EntitiesPage() {
  const data = await serverFetch<EntityList>('/entities');
  return <EntitiesContent initialData={data} />;
}

// components/entities/entities-content.tsx (Client)
'use client'
export function EntitiesContent({ initialData }) {
  const { data } = useEntities(initialData); // Hydrates
  return <Grid>{data.entities.map(...)}</Grid>;
}
```

## SilkCircuit Colors

```css
/* globals.css */
--sc-purple: #e135ff; /* Primary actions, keywords */
--sc-cyan: #80ffea; /* Interactions, highlights */
--sc-coral: #ff6ac1; /* Data, secondary */
--sc-yellow: #f1fa8c; /* Warnings */
--sc-green: #50fa7b; /* Success */
--sc-red: #ff6363; /* Errors */
```

```tsx
// Tailwind usage
<button className="bg-[var(--sc-purple)] hover:bg-[var(--sc-cyan)]">Action</button>
```

## React Query Hooks

```tsx
import { useEntities, useEntity, useCreateEntity } from "@/lib/hooks";

// List with filters
const { data, isLoading } = useEntities({ type: "task", status: "todo" });

// Single entity
const { data: entity } = useEntity(id);

// Mutations
const { mutate } = useCreateEntity();
mutate({ name: "New Entity", entity_type: "episode" });
```

## WebSocket Real-time Updates

```tsx
// Automatic invalidation setup in providers.tsx
useRealtimeUpdates(); // Invalidates queries on WS events

// Manual subscription
const ws = useWebSocket();
useEffect(() => {
  return ws.on("entity_created", (data) => {
    // Handle new entity
  });
}, []);
```

## Component Patterns

### Loading States

```tsx
<Suspense fallback={<Skeleton variant="card" count={6} />}>
  <AsyncContent />
</Suspense>
```

### Error Boundaries

```tsx
<ErrorBoundary level="section" fallback={<ErrorCard />}>
  <RiskyComponent />
</ErrorBoundary>
```

### Entity Cards

```tsx
<Card variant="entity" color={ENTITY_COLORS[entity.entity_type]}>
  <CardHeader title={entity.name} badge={entity.entity_type} />
  <CardContent>{entity.description}</CardContent>
</Card>
```

## API Fetching

```tsx
// Client-side (hooks use this)
const data = await fetchApi<Entity>(`/entities/${id}`);

// Server-side (in page.tsx)
const data = await serverFetch<Stats>("/admin/stats", {
  next: { revalidate: 60, tags: ["stats"] },
});
```

## Status Colors (from constants.ts)

```tsx
const STATUS_COLORS = {
  backlog: "gray",
  todo: "cyan",
  doing: "purple",
  blocked: "red",
  review: "yellow",
  done: "green",
};
```
