import { Suspense, type ReactNode } from 'react';
import { Skeleton, SkeletonCard, SkeletonList, LoadingState } from '@/components/ui/spinner';

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
        <SkeletonCard key={i} />
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
