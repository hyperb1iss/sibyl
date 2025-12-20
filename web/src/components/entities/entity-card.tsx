'use client';

import Link from 'next/link';
import { EntityBadge } from '@/components/ui/badge';
import { getEntityStyles } from '@/lib/constants';

interface Entity {
  id: string;
  entity_type: string;
  name: string;
  description?: string | null;
  source_file?: string | null;
}

interface EntityCardProps {
  entity: Entity;
  onDelete?: (id: string) => void;
  showActions?: boolean;
}

export function EntityCard({ entity, onDelete, showActions = true }: EntityCardProps) {
  const styles = getEntityStyles(entity.entity_type);

  return (
    <div
      className={`
        bg-sc-bg-base border border-sc-fg-subtle/20 rounded-xl p-4
        transition-all duration-200 group
        hover:shadow-lg
        ${styles.card}
      `}
    >
      <div className="flex items-start justify-between gap-4">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 mb-2">
            <EntityBadge type={entity.entity_type} />
          </div>
          <Link
            href={`/entities/${entity.id}`}
            className="block group-hover:text-sc-purple transition-colors"
          >
            <h3 className="text-lg font-semibold text-sc-fg-primary truncate">
              {entity.name}
            </h3>
          </Link>
          {entity.description && (
            <p className="text-sc-fg-muted text-sm mt-1 line-clamp-2">
              {entity.description}
            </p>
          )}
          {entity.source_file && (
            <p className="text-sc-fg-subtle text-xs mt-2 font-mono truncate">
              {entity.source_file}
            </p>
          )}
        </div>
        {showActions && (
          <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
            <Link
              href={`/entities/${entity.id}`}
              className="p-2 text-sc-fg-muted hover:text-sc-cyan rounded-lg hover:bg-sc-bg-highlight transition-colors"
              title="View details"
              aria-label={`View details for ${entity.name}`}
            >
              <span aria-hidden="true">⧉</span>
            </Link>
            {onDelete && (
              <button
                type="button"
                onClick={() => onDelete(entity.id)}
                className="p-2 text-sc-fg-muted hover:text-sc-red rounded-lg hover:bg-sc-red/10 transition-colors"
                title="Delete"
                aria-label={`Delete ${entity.name}`}
              >
                <span aria-hidden="true">✕</span>
              </button>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
