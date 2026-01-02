'use client';

import Link from 'next/link';
import { memo } from 'react';
import { EntityBadge } from '@/components/ui/badge';
import { EntityIcon } from '@/components/ui/entity-icon';
import { ENTITY_COLORS, type EntityType } from '@/lib/constants';

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

export const EntityCard = memo(function EntityCard({
  entity,
  onDelete,
  showActions = true,
}: EntityCardProps) {
  const color = ENTITY_COLORS[entity.entity_type as EntityType] ?? '#8b85a0';

  // Use CSS custom properties for dynamic colors with CSS hover states
  const cardStyle = {
    '--entity-color': color,
    '--entity-color-40': `${color}40`,
    '--entity-color-60': `${color}60`,
    '--entity-glow': `0 4px 8px oklch(0% 0 0 / 0.3), 0 8px 20px oklch(0% 0 0 / 0.2), 0 0 24px ${color}30, inset 0 1px 0 oklch(100% 0 0 / 0.05)`,
    background: `linear-gradient(135deg, ${color}18 0%, var(--sc-bg-base) 50%, var(--sc-bg-base) 100%)`,
    borderColor: `${color}40`,
  } as React.CSSProperties;

  return (
    <article
      className="relative overflow-hidden rounded-xl transition-all duration-200 group hover:-translate-y-0.5 border shadow-card hover:shadow-[var(--entity-glow)] hover:border-[var(--entity-color-60)]"
      style={cardStyle}
    >
      {/* Accent bar - entity type color */}
      <div className="absolute left-0 top-0 bottom-0 w-1" style={{ backgroundColor: color }} />

      <div className="pl-4 pr-3 py-4">
        <div className="flex items-start justify-between gap-3">
          <div className="flex-1 min-w-0">
            {/* Header: Icon + Badge */}
            <div className="flex items-center gap-2 mb-2.5">
              <span style={{ color }}>
                <EntityIcon type={entity.entity_type} size={18} />
              </span>
              <EntityBadge type={entity.entity_type} />
            </div>

            {/* Title */}
            <Link href={`/entities/${entity.id}`} className="block group/link">
              <h3 className="text-base font-semibold text-sc-fg-primary truncate transition-colors group-hover/link:text-white">
                {entity.name}
              </h3>
            </Link>

            {/* Description */}
            {entity.description && (
              <p className="text-sc-fg-muted text-sm mt-1.5 line-clamp-2 leading-relaxed">
                {entity.description}
              </p>
            )}

            {/* Source file */}
            {entity.source_file && (
              <p className="text-sc-fg-subtle text-xs mt-2.5 font-mono truncate flex items-center gap-1.5">
                <EntityIcon type="document" size={12} className="opacity-60" />
                {entity.source_file}
              </p>
            )}
          </div>

          {/* Actions */}
          {showActions && (
            <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity shrink-0">
              <Link
                href={`/entities/${entity.id}`}
                className="p-2 text-sc-fg-muted hover:text-sc-cyan rounded-lg hover:bg-sc-cyan/10 transition-colors"
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
    </article>
  );
});

export function EntityCardSkeleton() {
  return (
    <div className="relative bg-sc-bg-base rounded-xl overflow-hidden border border-sc-fg-subtle/30 shadow-card animate-pulse">
      <div className="absolute left-0 top-0 bottom-0 w-1 bg-sc-fg-subtle/20" />
      <div className="pl-4 pr-3 py-4">
        <div className="flex items-center gap-2 mb-2.5">
          <div className="w-5 h-5 bg-sc-bg-elevated rounded" />
          <div className="h-5 w-16 bg-sc-bg-elevated rounded" />
        </div>
        <div className="h-5 w-3/4 bg-sc-bg-elevated rounded mb-2" />
        <div className="h-4 w-full bg-sc-bg-elevated rounded mb-1" />
        <div className="h-4 w-2/3 bg-sc-bg-elevated rounded" />
      </div>
    </div>
  );
}
