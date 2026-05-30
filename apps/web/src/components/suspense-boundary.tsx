import { type ReactNode, Suspense } from 'react';
import { LoadingState, Skeleton, SkeletonCard, SkeletonList } from '@/components/ui/spinner';

// =============================================================================
// Suspense Boundary Types
// =============================================================================

type BoundaryVariant = 'page' | 'section' | 'card' | 'list' | 'inline';

interface SuspenseBoundaryProps {
  children: ReactNode;
  /** Pre-built fallback variant */
  variant?: BoundaryVariant;
  /** Custom fallback (overrides variant) */
  fallback?: ReactNode;
  /** Name for debugging in React DevTools */
  name?: string;
}

// =============================================================================
// Pre-built Fallback Components
// =============================================================================

/** Full page loading state */
function PageFallback() {
  return (
    <div className="min-h-[60vh] flex items-center justify-center">
      <LoadingState size="xl" variant="orbital" playful />
    </div>
  );
}

/** Section loading state */
function SectionFallback() {
  return <LoadingState size="lg" variant="orbital" />;
}

/** Card grid skeleton */
function CardFallback() {
  return (
    <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
      {Array.from({ length: 6 }).map((_, i) => (
        <SkeletonCard key={`skeleton-${i}`} />
      ))}
    </div>
  );
}

/** List skeleton */
function ListFallback() {
  return <SkeletonList count={5} />;
}

/** Inline loading (for small areas) */
function InlineFallback() {
  return (
    <div className="flex items-center gap-2 py-2">
      <Skeleton className="h-4 w-4 rounded-full" />
      <Skeleton className="h-4 w-24" />
    </div>
  );
}

const FALLBACKS: Record<BoundaryVariant, ReactNode> = {
  page: <PageFallback />,
  section: <SectionFallback />,
  card: <CardFallback />,
  list: <ListFallback />,
  inline: <InlineFallback />,
};

// =============================================================================
// Suspense Boundary Component
// =============================================================================

/**
 * Consistent Suspense wrapper with pre-built fallback variants.
 *
 * @example
 * // Page-level loading
 * <SuspenseBoundary variant="page">
 *   <AsyncPageContent />
 * </SuspenseBoundary>
 *
 * @example
 * // Card grid loading
 * <SuspenseBoundary variant="card">
 *   <EntityGrid entities={entities} />
 * </SuspenseBoundary>
 *
 * @example
 * // Custom fallback
 * <SuspenseBoundary fallback={<MyCustomLoader />}>
 *   <Content />
 * </SuspenseBoundary>
 */
export function SuspenseBoundary({
  children,
  variant = 'section',
  fallback,
  name,
}: SuspenseBoundaryProps) {
  const fallbackElement = fallback ?? FALLBACKS[variant];

  return (
    <Suspense fallback={fallbackElement} name={name}>
      {children}
    </Suspense>
  );
}

// =============================================================================
// Specialized Boundary Exports
// =============================================================================

/** Page-level suspense with full-page loading animation */
export function PageSuspense({ children, name }: { children: ReactNode; name?: string }) {
  return (
    <SuspenseBoundary variant="page" name={name}>
      {children}
    </SuspenseBoundary>
  );
}

/** Section-level suspense with centered loader */
export function SectionSuspense({ children, name }: { children: ReactNode; name?: string }) {
  return (
    <SuspenseBoundary variant="section" name={name}>
      {children}
    </SuspenseBoundary>
  );
}

/** Card grid suspense with skeleton cards */
export function CardGridSuspense({ children, name }: { children: ReactNode; name?: string }) {
  return (
    <SuspenseBoundary variant="card" name={name}>
      {children}
    </SuspenseBoundary>
  );
}

/** List suspense with skeleton rows */
export function ListSuspense({ children, name }: { children: ReactNode; name?: string }) {
  return (
    <SuspenseBoundary variant="list" name={name}>
      {children}
    </SuspenseBoundary>
  );
}

// =============================================================================
// Page-Specific Skeleton Components (for export)
// =============================================================================

/** Dashboard page skeleton */
export function DashboardSkeleton() {
  return (
    <div className="space-y-6 animate-fade-in">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="space-y-2">
          <Skeleton className="h-8 w-48" />
          <Skeleton className="h-4 w-64" />
        </div>
        <Skeleton className="h-6 w-20 rounded-full" />
      </div>

      {/* Stats grid */}
      <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-4">
        {Array.from({ length: 4 }).map((_, i) => (
          <SkeletonCard key={i} />
        ))}
      </div>

      {/* Quick actions */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        {Array.from({ length: 3 }).map((_, i) => (
          <Skeleton key={i} className="h-24 rounded-xl" />
        ))}
      </div>
    </div>
  );
}

/** Entity list page skeleton */
export function EntitiesSkeleton() {
  return (
    <div className="space-y-6 animate-fade-in">
      {/* Header */}
      <div className="space-y-2">
        <Skeleton className="h-8 w-32" />
        <Skeleton className="h-4 w-48" />
      </div>

      {/* Filters */}
      <div className="flex gap-4">
        <Skeleton className="h-10 flex-1 max-w-md rounded-lg" />
        <div className="flex gap-2">
          {Array.from({ length: 4 }).map((_, i) => (
            <Skeleton key={i} className="h-8 w-16 rounded-full" />
          ))}
        </div>
      </div>

      {/* Grid */}
      <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
        {Array.from({ length: 9 }).map((_, i) => (
          <SkeletonCard key={i} />
        ))}
      </div>
    </div>
  );
}

/** Entity detail page skeleton */
export function EntityDetailSkeleton() {
  return (
    <div className="space-y-6 animate-fade-in max-w-4xl">
      {/* Back link */}
      <Skeleton className="h-4 w-24" />

      {/* Title area */}
      <div className="space-y-2">
        <div className="flex items-center gap-3">
          <Skeleton className="h-6 w-20 rounded-full" />
          <Skeleton className="h-8 w-64" />
        </div>
        <Skeleton className="h-5 w-full max-w-md" />
      </div>

      {/* Content */}
      <div className="bg-sc-bg-base border border-sc-fg-subtle/20 rounded-xl p-6 space-y-4">
        <Skeleton className="h-6 w-24" />
        <Skeleton className="h-32 w-full rounded-lg" />
      </div>

      {/* Metadata */}
      <div className="grid grid-cols-2 gap-4">
        {Array.from({ length: 4 }).map((_, i) => (
          <div key={i} className="space-y-1">
            <Skeleton className="h-4 w-20" />
            <Skeleton className="h-5 w-32" />
          </div>
        ))}
      </div>
    </div>
  );
}

/** Search page skeleton */
export function SearchSkeleton() {
  return (
    <div className="space-y-6 animate-fade-in">
      {/* Header + search */}
      <div className="space-y-4">
        <Skeleton className="h-8 w-32" />
        <Skeleton className="h-12 w-full max-w-2xl rounded-lg" />
      </div>

      {/* Filters */}
      <div className="flex gap-2">
        {Array.from({ length: 5 }).map((_, i) => (
          <Skeleton key={i} className="h-8 w-20 rounded-full" />
        ))}
      </div>

      {/* Results */}
      <SkeletonList count={5} />
    </div>
  );
}

/** Projects page skeleton */
export function ProjectsSkeleton() {
  return (
    <div className="grid grid-cols-1 lg:grid-cols-3 gap-6 animate-fade-in">
      {/* Project list */}
      <div className="space-y-4">
        <Skeleton className="h-6 w-24" />
        {Array.from({ length: 4 }).map((_, i) => (
          <SkeletonCard key={i} />
        ))}
      </div>

      {/* Project detail */}
      <div className="lg:col-span-2 space-y-4">
        <Skeleton className="h-8 w-48" />
        <Skeleton className="h-4 w-full max-w-md" />
        <div className="grid grid-cols-3 gap-4">
          {Array.from({ length: 3 }).map((_, i) => (
            <SkeletonCard key={i} />
          ))}
        </div>
      </div>
    </div>
  );
}

/** Tasks page skeleton — header, filters, kanban-shaped columns. */
export function TasksSkeleton() {
  return (
    <div className="space-y-4 animate-fade-in">
      <div className="space-y-2">
        <Skeleton className="h-7 w-32" />
        <Skeleton className="h-9 w-full max-w-xl rounded-lg" />
      </div>
      <div className="flex flex-wrap items-center gap-2">
        {Array.from({ length: 5 }).map((_, i) => (
          <Skeleton key={i} className="h-7 w-20 rounded-full" />
        ))}
      </div>
      <div className="hidden gap-4 md:grid md:grid-cols-3 lg:grid-cols-5">
        {Array.from({ length: 5 }).map((_, col) => (
          <div
            key={`col-${col}`}
            className="space-y-3 rounded-xl border border-sc-fg-subtle/10 bg-sc-bg-base p-3"
          >
            <div className="flex items-center justify-between">
              <Skeleton className="h-4 w-20" />
              <Skeleton className="h-4 w-6 rounded-full" />
            </div>
            {Array.from({ length: col === 0 ? 4 : col === 1 ? 3 : 2 }).map((_, row) => (
              <div
                key={`task-${col}-${row}`}
                className="space-y-2 rounded-lg border border-sc-fg-subtle/10 bg-sc-bg-highlight/30 p-3"
              >
                <Skeleton className="h-3 w-full" />
                <Skeleton className="h-3 w-3/4" />
                <div className="flex items-center gap-2 pt-1">
                  <Skeleton className="h-4 w-12 rounded-full" />
                  <Skeleton className="h-4 w-16 rounded-full" />
                </div>
              </div>
            ))}
          </div>
        ))}
      </div>
      <div className="space-y-2 md:hidden">
        {Array.from({ length: 6 }).map((_, i) => (
          <Skeleton key={i} className="h-16 rounded-lg" />
        ))}
      </div>
    </div>
  );
}

/** Epics page skeleton — header, filters, list of rows with progress bars. */
export function EpicsSkeleton() {
  return (
    <div className="space-y-4 animate-fade-in">
      <div className="flex items-center justify-between">
        <div className="space-y-2">
          <Skeleton className="h-7 w-24" />
          <Skeleton className="h-4 w-48" />
        </div>
        <Skeleton className="h-9 w-28 rounded-lg" />
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <Skeleton className="h-9 flex-1 min-w-[200px] max-w-md rounded-lg" />
        {Array.from({ length: 4 }).map((_, i) => (
          <Skeleton key={i} className="h-7 w-20 rounded-full" />
        ))}
      </div>
      <div className="space-y-3">
        {Array.from({ length: 5 }).map((_, i) => (
          <div
            key={i}
            className="rounded-xl border border-sc-fg-subtle/10 bg-sc-bg-base p-4 space-y-3"
          >
            <div className="flex items-center justify-between gap-3">
              <div className="flex items-center gap-2 min-w-0 flex-1">
                <Skeleton className="h-4 w-16 rounded-full" />
                <Skeleton className="h-5 w-1/3" />
              </div>
              <Skeleton className="h-4 w-16" />
            </div>
            <Skeleton className="h-3 w-full max-w-2xl" />
            <div className="flex items-center gap-3">
              <Skeleton className="h-2 flex-1 max-w-xs rounded-full" />
              <Skeleton className="h-3 w-12" />
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

/** Sources page skeleton — header, filters, card grid. */
export function SourcesSkeleton() {
  return (
    <div className="space-y-6 animate-fade-in">
      <div className="flex items-center justify-between">
        <div className="space-y-2">
          <Skeleton className="h-7 w-28" />
          <Skeleton className="h-4 w-56" />
        </div>
        <Skeleton className="h-9 w-32 rounded-lg" />
      </div>
      <div className="flex flex-col gap-3 sm:flex-row">
        <Skeleton className="h-10 flex-1 max-w-md rounded-lg" />
        <div className="flex gap-2">
          {Array.from({ length: 3 }).map((_, i) => (
            <Skeleton key={i} className="h-10 w-24 rounded-lg" />
          ))}
        </div>
      </div>
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
        {Array.from({ length: 6 }).map((_, i) => (
          <div
            key={i}
            className="rounded-xl border border-sc-fg-subtle/10 bg-sc-bg-base p-5 space-y-4"
          >
            <div className="flex items-start gap-3">
              <Skeleton className="h-10 w-10 rounded-xl" />
              <div className="flex-1 space-y-2">
                <Skeleton className="h-4 w-3/4" />
                <Skeleton className="h-3 w-1/2" />
              </div>
              <Skeleton className="h-6 w-16 rounded-full" />
            </div>
            <Skeleton className="h-3 w-full" />
            <div className="flex gap-2">
              <Skeleton className="h-9 flex-1 rounded-lg" />
              <Skeleton className="h-9 w-10 rounded-lg" />
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

/** Archive / memory captures skeleton — list of timestamped rows. */
export function ArchiveSkeleton() {
  return (
    <div className="space-y-4 animate-fade-in">
      <div className="space-y-2">
        <Skeleton className="h-7 w-32" />
        <Skeleton className="h-4 w-64" />
      </div>
      <div className="space-y-3">
        {Array.from({ length: 8 }).map((_, i) => (
          <div
            key={i}
            className="flex items-start gap-3 rounded-lg border border-sc-fg-subtle/10 bg-sc-bg-base p-4"
          >
            <Skeleton className="h-8 w-8 rounded-full" />
            <div className="flex-1 space-y-2">
              <div className="flex items-center gap-2">
                <Skeleton className="h-4 w-1/3" />
                <Skeleton className="h-3 w-20" />
              </div>
              <Skeleton className="h-3 w-full" />
              <Skeleton className="h-3 w-2/3" />
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

/** Graph page skeleton — chrome around a soft canvas placeholder. */
export function GraphSkeleton() {
  return (
    <div className="space-y-4 animate-fade-in">
      <div className="flex items-center justify-between">
        <div className="space-y-2">
          <Skeleton className="h-7 w-28" />
          <Skeleton className="h-4 w-48" />
        </div>
        <div className="flex gap-2">
          <Skeleton className="h-9 w-24 rounded-lg" />
          <Skeleton className="h-9 w-24 rounded-lg" />
        </div>
      </div>
      <div className="relative h-[calc(100vh-14rem)] rounded-xl border border-sc-fg-subtle/10 bg-sc-bg-base overflow-hidden">
        <div className="absolute inset-0 bg-gradient-to-br from-sc-purple/5 via-sc-bg-base to-sc-cyan/5 animate-pulse" />
        <div className="absolute top-4 left-4 right-4 flex justify-between">
          <Skeleton className="h-9 w-48 rounded-lg" />
          <Skeleton className="h-9 w-32 rounded-lg" />
        </div>
      </div>
    </div>
  );
}

/** Generic page skeleton — header + a list. Used for unknown route fallback. */
export function GenericPageSkeleton() {
  return (
    <div className="space-y-6 animate-fade-in">
      <div className="space-y-2">
        <Skeleton className="h-8 w-48" />
        <Skeleton className="h-4 w-64" />
      </div>
      <SkeletonList count={5} />
    </div>
  );
}

/** Epic detail page skeleton — breadcrumb, hero card, task rows. */
export function EpicDetailSkeleton() {
  return (
    <div className="space-y-6 animate-fade-in">
      <Skeleton className="h-4 w-48" />
      <div className="relative overflow-hidden rounded-xl border border-sc-fg-subtle/10 bg-sc-bg-base">
        <div className="absolute left-0 top-0 bottom-0 w-1 bg-sc-fg-subtle/20" />
        <div className="space-y-4 pl-5 pr-4 py-4">
          <div className="flex items-center gap-2 rounded-lg bg-sc-bg-highlight/40 px-3 py-2">
            <Skeleton className="h-4 w-4 rounded-full" />
            <Skeleton className="h-4 w-40" />
            <Skeleton className="ml-auto h-4 w-10" />
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Skeleton className="h-5 w-16 rounded" />
            <Skeleton className="h-5 w-12 rounded" />
            <Skeleton className="h-5 w-24 rounded" />
          </div>
          <Skeleton className="h-7 w-2/3" />
          <Skeleton className="h-4 w-full max-w-xl" />
          <div className="space-y-1.5">
            <div className="flex items-center justify-between">
              <Skeleton className="h-3 w-32" />
              <Skeleton className="h-3 w-10" />
            </div>
            <Skeleton className="h-2 w-full rounded-full" />
          </div>
        </div>
      </div>
      <div className="space-y-3">
        <Skeleton className="h-5 w-24" />
        <div className="space-y-2">
          {Array.from({ length: 5 }).map((_, i) => (
            <div
              key={i}
              className="flex items-center gap-3 rounded-lg border border-sc-fg-subtle/10 bg-sc-bg-base p-3"
            >
              <Skeleton className="h-4 w-4 rounded-full" />
              <Skeleton className="h-4 flex-1 max-w-md" />
              <Skeleton className="h-4 w-16 rounded-full" />
              <Skeleton className="h-4 w-12 rounded-full" />
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

/** Memory workspace skeleton — hero, action tiles, scope chip, dual-column panels. */
export function MemoryHomeSkeleton() {
  return (
    <div className="space-y-4 animate-fade-in">
      <Skeleton className="h-4 w-40" />
      <div className="rounded-xl border border-sc-fg-subtle/10 bg-sc-bg-base p-6 space-y-3">
        <Skeleton className="h-7 w-48" />
        <Skeleton className="h-4 w-2/3 max-w-xl" />
        <div className="flex flex-wrap gap-3 pt-1">
          {Array.from({ length: 4 }).map((_, i) => (
            <Skeleton key={i} className="h-12 w-28 rounded-lg" />
          ))}
        </div>
      </div>
      <div className="grid gap-3 md:grid-cols-3">
        {Array.from({ length: 3 }).map((_, i) => (
          <Skeleton key={i} className="h-24 rounded-xl" />
        ))}
      </div>
      <Skeleton className="h-9 w-64 rounded-lg" />
      <div className="grid gap-4 xl:grid-cols-[minmax(0,1.5fr)_minmax(320px,1fr)]">
        <div className="space-y-4">
          {Array.from({ length: 2 }).map((_, i) => (
            <div
              key={i}
              className="rounded-xl border border-sc-fg-subtle/10 bg-sc-bg-base p-4 space-y-3"
            >
              <div className="flex items-center justify-between">
                <Skeleton className="h-4 w-32" />
                <Skeleton className="h-4 w-12" />
              </div>
              {Array.from({ length: 3 }).map((_, j) => (
                <div
                  key={j}
                  className="flex items-center gap-3 rounded-lg bg-sc-bg-highlight/30 p-3"
                >
                  <Skeleton className="h-3 w-3 rounded-full" />
                  <div className="flex-1 space-y-1.5">
                    <Skeleton className="h-3 w-2/3" />
                    <Skeleton className="h-2.5 w-1/3" />
                  </div>
                </div>
              ))}
            </div>
          ))}
        </div>
        <div className="space-y-4">
          {Array.from({ length: 3 }).map((_, i) => (
            <div
              key={i}
              className="rounded-xl border border-sc-fg-subtle/10 bg-sc-bg-base p-4 space-y-3"
            >
              <Skeleton className="h-4 w-28" />
              <Skeleton className="h-3 w-full" />
              <Skeleton className="h-3 w-2/3" />
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

/** Memory captures list skeleton. */
export function MemoryCapturesSkeleton() {
  return (
    <div className="space-y-4 animate-fade-in">
      <div className="space-y-2">
        <Skeleton className="h-7 w-32" />
        <Skeleton className="h-4 w-64" />
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <Skeleton className="h-9 flex-1 min-w-[200px] max-w-md rounded-lg" />
        {Array.from({ length: 4 }).map((_, i) => (
          <Skeleton key={i} className="h-8 w-24 rounded-full" />
        ))}
      </div>
      <div className="space-y-2">
        {Array.from({ length: 8 }).map((_, i) => (
          <div
            key={i}
            className="flex items-start gap-3 rounded-lg border border-sc-fg-subtle/10 bg-sc-bg-base p-4"
          >
            <Skeleton className="h-9 w-9 rounded-full" />
            <div className="flex-1 space-y-2">
              <div className="flex items-center gap-2">
                <Skeleton className="h-4 w-1/3" />
                <Skeleton className="h-3 w-16" />
              </div>
              <Skeleton className="h-3 w-full" />
              <Skeleton className="h-3 w-3/4" />
              <div className="flex gap-2 pt-1">
                <Skeleton className="h-5 w-16 rounded-full" />
                <Skeleton className="h-5 w-20 rounded-full" />
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
