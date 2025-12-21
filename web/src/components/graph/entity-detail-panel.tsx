'use client';

import Link from 'next/link';
import { memo } from 'react';
import { EntityBadge } from '@/components/ui/badge';
import { LoadingState } from '@/components/ui/spinner';
import { useEntity, useRelatedEntities } from '@/lib/hooks';

interface RelatedEntity {
  id: string;
  name: string;
}

interface EntityDetailPanelProps {
  entityId: string;
  onClose: () => void;
  variant?: 'sidebar' | 'sheet';
}

export const EntityDetailPanel = memo(function EntityDetailPanel({
  entityId,
  onClose,
  variant = 'sidebar',
}: EntityDetailPanelProps) {
  const { data: entity, isLoading, error } = useEntity(entityId);
  const { data: related } = useRelatedEntities(entityId);

  const isSheet = variant === 'sheet';

  return (
    <div
      className={
        isSheet
          ? 'flex flex-col max-h-[calc(70vh-32px)] overflow-hidden'
          : 'w-80 flex-shrink-0 bg-sc-bg-base border border-sc-fg-subtle/20 rounded-xl overflow-hidden flex flex-col'
      }
    >
      {/* Header - only show for sidebar variant */}
      {!isSheet && (
        <div className="p-4 border-b border-sc-fg-subtle/20 flex items-center justify-between">
          <h3 className="text-sm font-semibold text-sc-fg-primary">Entity Details</h3>
          <button
            type="button"
            onClick={onClose}
            className="p-1 text-sc-fg-muted hover:text-sc-fg-primary hover:bg-sc-bg-highlight rounded transition-colors"
            aria-label="Close panel"
          >
            <span aria-hidden="true">✕</span>
          </button>
        </div>
      )}

      {/* Content */}
      <div className={`flex-1 overflow-y-auto ${isSheet ? 'px-4 pb-8' : 'p-4'}`}>
        {isLoading ? (
          <div className="flex justify-center py-8">
            <LoadingState message="Loading..." variant="orbital" />
          </div>
        ) : error ? (
          <div className="text-sm text-sc-red py-4">Failed to load entity</div>
        ) : entity ? (
          <div className="space-y-4">
            {/* Entity type badge */}
            <EntityBadge type={entity.entity_type} />

            {/* Name */}
            <div>
              <h4 className={`font-semibold text-sc-fg-primary ${isSheet ? 'text-xl' : 'text-lg'}`}>
                {entity.name}
              </h4>
            </div>

            {/* Description */}
            {entity.description && (
              <p className="text-sm text-sc-fg-muted leading-relaxed">{entity.description}</p>
            )}

            {/* Metadata */}
            <div className="space-y-2 text-xs">
              {entity.source_file && (
                <div className="flex items-start gap-2">
                  <span className="text-sc-fg-subtle">Source:</span>
                  <span className="text-sc-cyan font-mono truncate flex-1">
                    {entity.source_file}
                  </span>
                </div>
              )}
              {entity.created_at && (
                <div className="flex items-center gap-2">
                  <span className="text-sc-fg-subtle">Created:</span>
                  <span className="text-sc-fg-muted">
                    {new Date(entity.created_at).toLocaleDateString()}
                  </span>
                </div>
              )}
            </div>

            {/* Related entities */}
            {related?.entities && related.entities.length > 0 && (
              <div className="pt-4 border-t border-sc-fg-subtle/20">
                <h5 className="text-xs font-medium text-sc-fg-muted mb-2">
                  Related ({related.entities.length})
                </h5>
                <div className={`${isSheet ? 'flex flex-wrap gap-1.5' : 'space-y-1'}`}>
                  {(related.entities as RelatedEntity[]).slice(0, isSheet ? 8 : 5).map(rel => (
                    <div
                      key={rel.id}
                      className={`text-xs px-2 py-1 bg-sc-bg-highlight rounded truncate text-sc-fg-muted ${
                        isSheet ? 'inline-block' : ''
                      }`}
                    >
                      {rel.name}
                    </div>
                  ))}
                  {related.entities.length > (isSheet ? 8 : 5) && (
                    <div className="text-xs text-sc-fg-subtle px-2">
                      +{related.entities.length - (isSheet ? 8 : 5)} more
                    </div>
                  )}
                </div>
              </div>
            )}

            {/* Actions */}
            <div className="pt-4 border-t border-sc-fg-subtle/20">
              <Link
                href={`/entities/${entityId}`}
                className={`block w-full text-center py-2.5 bg-sc-purple/20 text-sc-purple rounded-lg text-sm font-medium hover:bg-sc-purple/30 transition-colors ${
                  isSheet ? 'py-3' : 'px-4 py-2'
                }`}
              >
                View Full Details
              </Link>
            </div>
          </div>
        ) : null}
      </div>
    </div>
  );
});
