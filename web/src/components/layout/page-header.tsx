import type { ReactNode } from 'react';

interface PageHeaderProps {
  title: string;
  description?: string;
  action?: ReactNode;
  meta?: ReactNode;
}

export function PageHeader({ title, description, action, meta }: PageHeaderProps) {
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
