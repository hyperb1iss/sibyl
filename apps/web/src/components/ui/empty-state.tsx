'use client';

import Link from 'next/link';
import { Check, Cube, Database, Globe, KanbanBoard, List, Search } from '@/components/ui/icons';

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
          text-5xl mb-4 p-4 rounded-2xl
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
              ? 'px-4 py-2 bg-sc-purple hover:bg-sc-purple/80 text-white rounded-lg font-medium transition-colors'
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
export function TasksEmptyState({
  projectName,
  onCreateTask,
  onClearFilter,
}: {
  projectName?: string;
  onCreateTask?: () => void;
  onClearFilter?: () => void;
}) {
  if (projectName) {
    return (
      <EnhancedEmptyState
        icon={<List width={40} height={40} className="text-sc-yellow" />}
        title={`No tasks in ${projectName}`}
        description="This project doesn't have any tasks yet. Create one to start tracking work."
        variant="filtered"
        actions={[
          ...(onCreateTask ? [{ label: `Add task to ${projectName}`, onClick: onCreateTask }] : []),
          ...(onClearFilter
            ? [{ label: 'View all tasks', onClick: onClearFilter, variant: 'secondary' as const }]
            : []),
        ]}
      />
    );
  }

  return (
    <EnhancedEmptyState
      icon={<List width={40} height={40} className="text-sc-fg-subtle" />}
      title="No tasks yet"
      description="Tasks help you track work across your projects. Press C or use the button below to create your first task."
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
  onClearFilter,
}: {
  entityType?: string;
  onClearFilter?: () => void;
}) {
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
        { label: 'Add Knowledge', href: '/entities' },
        { label: 'Browse Entities', href: '/entities', variant: 'secondary' },
      ]}
    />
  );
}
