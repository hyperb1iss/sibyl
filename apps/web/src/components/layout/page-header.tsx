import type { ReactNode } from 'react';

interface PageHeaderProps {
  /** Page title - only use when it provides context beyond the breadcrumb */
  title?: string;
  /** Description text shown below title or as standalone */
  description?: string;
  /** Metadata shown on the right (e.g., counts, status) */
  meta?: ReactNode;
  /** Action buttons shown on the right */
  action?: ReactNode;
  /** Whether this is a compact header (no title, just description + actions) */
  compact?: boolean;
}

/**
 * Page header component for consistent page layouts.
 *
 * Use cases:
 * - `compact` mode: Description + actions bar (most list pages)
 * - Full mode: Title + description + actions (detail pages with custom titles)
 */
export function PageHeader({ title, description, action, meta, compact }: PageHeaderProps) {
  // Compact mode: just description and actions in a bar
  if (compact || !title) {
    if (!description && !action && !meta) return null;

    return (
      <div className="flex flex-col xs:flex-row xs:items-center justify-between gap-2 sm:gap-3 mb-3 sm:mb-4">
        {description && (
          <p className="text-xs sm:text-sm text-sc-fg-muted line-clamp-1">{description}</p>
        )}
        <div className="flex items-center gap-2 sm:gap-3 shrink-0">
          {meta && <div className="text-sc-fg-subtle text-xs sm:text-sm">{meta}</div>}
          {action}
        </div>
      </div>
    );
  }

  // Full mode with title
  return (
    <div className="flex items-center justify-between gap-3 sm:gap-4 mb-3 sm:mb-6">
      <div className="min-w-0">
        <h1 className="text-lg sm:text-2xl font-bold text-sc-fg-primary truncate">{title}</h1>
        {description && (
          <p className="text-xs sm:text-base text-sc-fg-muted mt-0.5 sm:mt-1 line-clamp-1 sm:line-clamp-2">
            {description}
          </p>
        )}
      </div>
      <div className="flex items-center gap-2 sm:gap-3 shrink-0">
        {meta && <div className="hidden sm:block text-sc-fg-subtle text-sm">{meta}</div>}
        {action}
      </div>
    </div>
  );
}
