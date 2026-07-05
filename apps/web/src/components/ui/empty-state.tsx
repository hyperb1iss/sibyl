'use client';

import Link from 'next/link';
import type { ReactNode } from 'react';
import {
  AlertTriangle,
  BarChart3,
  Check,
  CheckCircle2,
  Cube,
  Database,
  Flash,
  Globe,
  KanbanBoard,
  List,
  PlusCircle,
  Search,
  WifiOff,
} from '@/components/ui/icons';

interface EmptyStateAction {
  label: string;
  onClick?: () => void;
  href?: string;
  variant?: 'primary' | 'secondary';
}

interface EnhancedEmptyStateProps {
  icon: React.ReactNode;
  title: string;
  description: string;
  actions?: EmptyStateAction[];
  variant?: 'default' | 'success' | 'filtered';
}

export function EnhancedEmptyState({
  icon,
  title,
  description,
  actions,
  variant = 'default',
}: EnhancedEmptyStateProps) {
  const iconVariants = {
    default: 'text-sc-fg-subtle',
    success: 'text-sc-green',
    filtered: 'text-sc-yellow',
  };

  return (
    <div className="flex flex-col items-center justify-center py-16 px-4 text-center animate-fade-in">
      {/* Icon with glow effect */}
      <div
        className={`
          text-5xl mb-4 p-4 rounded-xl
          ${variant === 'success' ? 'bg-sc-green/10 shadow-lg shadow-sc-green/20' : ''}
          ${variant === 'filtered' ? 'bg-sc-yellow/10' : ''}
          ${variant === 'default' ? 'bg-sc-bg-highlight' : ''}
          ${iconVariants[variant]}
        `}
      >
        {icon}
      </div>

      <h3 className="text-lg font-semibold text-sc-fg-primary mb-2">{title}</h3>
      <p className="text-sc-fg-muted max-w-md mb-6">{description}</p>

      {/* Actions */}
      {actions && actions.length > 0 && (
        <div className="flex items-center gap-3 flex-wrap justify-center">
          {actions.map(action => {
            const isPrimary = action.variant !== 'secondary';
            const className = isPrimary
              ? 'px-4 py-2 bg-sc-purple hover:bg-sc-purple/80 text-sc-on-accent rounded-lg font-medium transition-colors'
              : 'px-4 py-2 bg-sc-bg-highlight hover:bg-sc-bg-elevated text-sc-fg-muted rounded-lg transition-colors';

            if (action.href) {
              return (
                <Link key={action.label} href={action.href} className={className}>
                  {action.label}
                </Link>
              );
            }

            return (
              <button
                key={action.label}
                type="button"
                onClick={action.onClick}
                className={className}
              >
                {action.label}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

// Pre-built empty states for common scenarios
export function TasksEmptyState({ onCreateTask }: { onCreateTask?: () => void }) {
  return (
    <EnhancedEmptyState
      icon={<List width={40} height={40} className="text-sc-fg-subtle" />}
      title="No tasks found"
      description="No tasks match your current filters. Press C or use the button below to create a new task."
      actions={[
        ...(onCreateTask ? [{ label: 'Create Task', onClick: onCreateTask }] : []),
        { label: 'View Projects', href: '/projects', variant: 'secondary' },
      ]}
    />
  );
}

export function ProjectsEmptyState({ onCreateProject }: { onCreateProject?: () => void }) {
  return (
    <EnhancedEmptyState
      icon={<KanbanBoard width={40} height={40} className="text-sc-cyan" />}
      title="No projects yet"
      description="Projects help you organize related tasks and track progress. Start by creating your first project."
      actions={[
        ...(onCreateProject ? [{ label: 'Create Project', onClick: onCreateProject }] : []),
      ]}
    />
  );
}

export function AllCaughtUpState() {
  return (
    <EnhancedEmptyState
      icon={<Check width={40} height={40} className="text-sc-green" />}
      title="You're all caught up!"
      description="All tasks are complete. Great work! Take a break or start something new."
      variant="success"
      actions={[{ label: 'View Completed', href: '/tasks?status=done', variant: 'secondary' }]}
    />
  );
}

export function SearchEmptyState({ query, onClear }: { query?: string; onClear?: () => void }) {
  if (query) {
    return (
      <EnhancedEmptyState
        icon={<Search width={40} height={40} className="text-sc-yellow" />}
        title="No results found"
        description={`No matches for "${query}". Try a different search term or browse entities.`}
        variant="filtered"
        actions={[
          ...(onClear ? [{ label: 'Clear search', onClick: onClear }] : []),
          { label: 'Browse Entities', href: '/entities', variant: 'secondary' },
        ]}
      />
    );
  }

  return (
    <EnhancedEmptyState
      icon={<Search width={40} height={40} className="text-sc-cyan" />}
      title="Search your knowledge"
      description="Enter a query to search across all entities, projects, and tasks."
    />
  );
}

export function SourcesEmptyState({ onAddSource }: { onAddSource?: () => void }) {
  return (
    <EnhancedEmptyState
      icon={<Globe width={40} height={40} className="text-sc-cyan" />}
      title="No documentation sources"
      description="Add external documentation sources to search alongside your knowledge graph. Crawl API docs, guides, and references."
      actions={[...(onAddSource ? [{ label: 'Add Source', onClick: onAddSource }] : [])]}
    />
  );
}

export function EntitiesEmptyState({
  entityType,
  searchQuery,
  onClearFilter,
}: {
  entityType?: string;
  searchQuery?: string;
  onClearFilter?: () => void;
}) {
  // Search with no results
  if (searchQuery) {
    const filterContext = entityType ? ` in "${entityType}" entities` : '';
    return (
      <EnhancedEmptyState
        icon={<Search width={40} height={40} className="text-sc-yellow" />}
        title="No matches found"
        description={`No entities matching "${searchQuery}"${filterContext}. Try a different search term.`}
        variant="filtered"
        actions={[...(onClearFilter ? [{ label: 'Clear filters', onClick: onClearFilter }] : [])]}
      />
    );
  }

  // Type filter with no results
  if (entityType) {
    return (
      <EnhancedEmptyState
        icon={<Cube width={40} height={40} className="text-sc-yellow" />}
        title={`No ${entityType} entities`}
        description={`No entities of type "${entityType}" found. Try a different filter or add some knowledge.`}
        variant="filtered"
        actions={[
          ...(onClearFilter ? [{ label: 'Clear filter', onClick: onClearFilter }] : []),
          { label: 'Add Knowledge', href: '/entities', variant: 'secondary' },
        ]}
      />
    );
  }

  return (
    <EnhancedEmptyState
      icon={<Cube width={40} height={40} className="text-sc-fg-subtle" />}
      title="No entities yet"
      description="Your knowledge graph is empty. Start by adding patterns, learnings, or importing documentation."
      actions={[
        { label: 'Add Entity', href: '/entities' },
        { label: 'Import Docs', href: '/sources', variant: 'secondary' },
      ]}
    />
  );
}

export function GraphEmptyState() {
  return (
    <EnhancedEmptyState
      icon={<Database width={40} height={40} className="text-sc-purple" />}
      title="Graph is empty"
      description="Add some knowledge to see your connections visualized. Entities and their relationships will appear here."
      actions={[
        { label: 'Add Knowledge', href: '/memory' },
        { label: 'Browse Entities', href: '/entities', variant: 'secondary' },
      ]}
    />
  );
}

export function EpicsEmptyState({
  onCreateEpic,
  isFiltered,
}: {
  onCreateEpic?: () => void;
  isFiltered?: boolean;
}) {
  if (isFiltered) {
    return (
      <EnhancedEmptyState
        icon={<Flash width={40} height={40} className="text-sc-yellow" />}
        title="No epics match your filters"
        description="Try adjusting your search or project filter to find what you're looking for."
        variant="filtered"
      />
    );
  }

  return (
    <EnhancedEmptyState
      icon={<Flash width={40} height={40} className="text-sc-coral" />}
      title="No epics yet"
      description="Epics group related tasks into larger initiatives. Create an epic to organize your feature work."
      actions={[
        ...(onCreateEpic ? [{ label: 'Create Epic', onClick: onCreateEpic }] : []),
        { label: 'View Tasks', href: '/tasks', variant: 'secondary' },
      ]}
    />
  );
}

// Basic empty state for when there's no data - with personality.
// (Relocated from tooltip.tsx; tooltip.tsx re-exports these for compatibility.)
interface EmptyStateProps {
  icon?: ReactNode;
  title: string;
  description?: string;
  action?: ReactNode;
  variant?: 'default' | 'search' | 'data' | 'create';
}

const EMPTY_STATE_DEFAULTS = {
  search: {
    icon: <Search width={48} height={48} className="text-sc-cyan" />,
    floatingClass: 'animate-float',
  },
  data: {
    icon: <BarChart3 width={48} height={48} className="text-sc-purple" />,
    floatingClass: 'animate-wiggle',
  },
  create: {
    icon: <PlusCircle width={48} height={48} className="text-sc-yellow" />,
    floatingClass: 'animate-bounce-in',
  },
  default: {
    icon: <Cube width={48} height={48} className="text-sc-coral" />,
    floatingClass: 'animate-float',
  },
};

export function EmptyState({
  icon,
  title,
  description,
  action,
  variant = 'default',
}: EmptyStateProps) {
  const defaults = EMPTY_STATE_DEFAULTS[variant];
  const displayIcon = icon ?? defaults.icon;

  return (
    <div className="text-center py-16 animate-fade-in">
      {displayIcon && (
        <div className={`text-6xl mb-4 opacity-80 ${defaults.floatingClass}`}>{displayIcon}</div>
      )}
      <p className="text-sc-fg-muted text-lg font-medium">{title}</p>
      {description && (
        <p className="text-sc-fg-subtle text-sm mt-2 max-w-md mx-auto">{description}</p>
      )}
      {action && <div className="mt-6 animate-slide-up">{action}</div>}
    </div>
  );
}

// Error state component - friendly and helpful
interface ErrorStateProps {
  title?: string;
  message: string;
  action?: ReactNode;
  variant?: 'error' | 'warning' | 'offline';
}

const ERROR_VARIANTS = {
  error: {
    icon: <AlertTriangle width={32} height={32} className="text-sc-red" />,
    title: 'Oops, something went sideways',
    color: 'text-sc-red',
    iconClass: 'animate-wiggle',
  },
  warning: {
    icon: <Flash width={32} height={32} className="text-sc-yellow" />,
    title: 'Heads up',
    color: 'text-sc-yellow',
    iconClass: 'animate-pulse',
  },
  offline: {
    icon: <WifiOff width={32} height={32} className="text-sc-coral" />,
    title: 'Connection lost',
    color: 'text-sc-coral',
    iconClass: 'animate-float',
  },
};

export function ErrorState({ title, message, action, variant = 'error' }: ErrorStateProps) {
  const variantConfig = ERROR_VARIANTS[variant];
  const displayTitle = title ?? variantConfig.title;

  return (
    <div className="text-center py-12 animate-fade-in">
      <div className={`text-4xl mb-4 ${variantConfig.iconClass}`}>{variantConfig.icon}</div>
      <p className={`text-lg font-medium ${variantConfig.color}`}>{displayTitle}</p>
      <p className="text-sc-fg-muted text-sm mt-1 max-w-md mx-auto">{message}</p>
      {action && <div className="mt-4 animate-slide-up">{action}</div>}
    </div>
  );
}

// Success celebration component
interface SuccessStateProps {
  title: string;
  message?: string;
  action?: ReactNode;
  celebratory?: boolean;
}

export function SuccessState({ title, message, action, celebratory = true }: SuccessStateProps) {
  return (
    <div className="text-center py-12 animate-bounce-in">
      <div className={`mb-4 flex justify-center ${celebratory ? 'animate-glow-pulse' : ''}`}>
        <CheckCircle2 width={48} height={48} className="text-sc-green" />
      </div>
      <p className="text-sc-green text-xl font-semibold gradient-text">{title}</p>
      {message && <p className="text-sc-fg-muted text-sm mt-2 max-w-md mx-auto">{message}</p>}
      {action && <div className="mt-6 animate-slide-up">{action}</div>}
    </div>
  );
}
