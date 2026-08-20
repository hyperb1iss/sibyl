'use client';

import Link from 'next/link';
import { memo, useMemo, useState } from 'react';
import { EntityBadge, RelationshipBadge } from '@/components/ui/badge';
import { ChevronDown, ChevronUp, Network } from '@/components/ui/icons';
import { Markdown } from '@/components/ui/markdown';
import { LoadingState } from '@/components/ui/spinner';
import type { RelatedEntitySummary } from '@/lib/api/graph';
import { useEntity } from '@/lib/hooks/graph';

interface RelatedEntity extends RelatedEntitySummary {}

interface EntityDetailPanelProps {
  entityId: string;
  onClose: () => void;
  onNavigate?: (entityId: string) => void;
  variant?: 'sidebar' | 'sheet';
  queryMode?: 'default' | 'graph';
  relatedEntities?: RelatedEntity[] | null;
}

function diversifyRelatedEntities(entities: RelatedEntity[]): RelatedEntity[] {
  if (entities.length <= 2) return entities;

  const buckets = new Map<string, RelatedEntity[]>();
  for (const entity of entities) {
    const bucket = buckets.get(entity.entity_type) ?? [];
    bucket.push(entity);
    buckets.set(entity.entity_type, bucket);
  }

  const orderedBuckets = [...buckets.entries()]
    .sort(([, left], [, right]) => right.length - left.length)
    .map(([, bucket]) => [...bucket]);

  const diversified: RelatedEntity[] = [];
  while (orderedBuckets.some(bucket => bucket.length > 0)) {
    for (const bucket of orderedBuckets) {
      const next = bucket.shift();
      if (next) diversified.push(next);
    }
  }

  return diversified;
}

// Collapsible section component
function Section({
  title,
  count,
  defaultOpen = true,
  children,
}: {
  title: string;
  count?: number;
  defaultOpen?: boolean;
  children: React.ReactNode;
}) {
  const [isOpen, setIsOpen] = useState(defaultOpen);

  return (
    <div className="border-t border-sc-fg-subtle/20 pt-3">
      <button
        type="button"
        onClick={() => setIsOpen(!isOpen)}
        className="w-full flex items-center justify-between text-xs font-medium text-sc-fg-muted hover:text-sc-fg-primary transition-colors mb-2"
      >
        <span>
          {title}
          {count !== undefined && <span className="ml-1.5 text-sc-fg-subtle">({count})</span>}
        </span>
        {isOpen ? <ChevronUp width={14} height={14} /> : <ChevronDown width={14} height={14} />}
      </button>
      {isOpen && children}
    </div>
  );
}

export const EntityDetailPanel = memo(function EntityDetailPanel({
  entityId,
  onClose,
  onNavigate,
  variant = 'sidebar',
  queryMode = 'default',
  relatedEntities: providedRelatedEntities,
}: EntityDetailPanelProps) {
  const {
    data: entity,
    isLoading,
    error,
  } = useEntity(
    entityId,
    undefined,
    queryMode === 'graph' ? { include_summary: false, related_limit: 0 } : undefined
  );

  const isSheet = variant === 'sheet';

  // Group related entities by direction
  const groupedRelated = useMemo(() => {
    const entities = providedRelatedEntities ?? entity?.related ?? [];
    if (entities.length === 0) return { incoming: [], outgoing: [] };

    const seen = new Set<string>();
    const unique = entities.filter(rel => {
      if (seen.has(rel.id)) return false;
      seen.add(rel.id);
      return true;
    });

    return {
      incoming: diversifyRelatedEntities(unique.filter(r => r.direction === 'incoming')),
      outgoing: diversifyRelatedEntities(unique.filter(r => r.direction === 'outgoing')),
    };
  }, [entity?.related, providedRelatedEntities]);

  // Extract learnings from metadata if present
  const learnings = useMemo(() => {
    if (!entity?.metadata) return null;
    const meta = entity.metadata as Record<string, unknown>;
    return meta.learnings as string | undefined;
  }, [entity]);

  // Handle clicking a related entity
  const handleEntityClick = (id: string) => {
    if (onNavigate) {
      onNavigate(id);
    }
  };

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
          <div className="space-y-3">
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

            {/* Tags */}
            {entity.tags && entity.tags.length > 0 && (
              <div className="flex flex-wrap gap-1">
                {entity.tags.slice(0, 5).map(tag => (
                  <span
                    key={tag}
                    className="px-1.5 py-0.5 text-[10px] bg-sc-bg-highlight text-sc-fg-muted rounded"
                  >
                    {tag}
                  </span>
                ))}
                {entity.tags.length > 5 && (
                  <span className="px-1.5 py-0.5 text-[10px] text-sc-fg-subtle">
                    +{entity.tags.length - 5}
                  </span>
                )}
              </div>
            )}

            {/* Content Preview */}
            {entity.content && (
              <Section title="Content" defaultOpen={!entity.description}>
                <div className="max-h-48 overflow-y-auto text-xs bg-sc-bg-highlight/50 rounded-lg p-3">
                  <Markdown
                    content={
                      entity.content.length > 500
                        ? `${entity.content.slice(0, 500)}...`
                        : entity.content
                    }
                  />
                </div>
              </Section>
            )}

            {/* Learnings */}
            {learnings && (
              <Section title="Learnings" defaultOpen>
                <div className="text-xs text-sc-fg-secondary bg-sc-purple/10 border border-sc-purple/20 rounded-lg p-3">
                  <Markdown content={learnings} />
                </div>
              </Section>
            )}

            {/* Outgoing Relationships */}
            {groupedRelated.outgoing.length > 0 && (
              <Section title="Connected To" count={groupedRelated.outgoing.length} defaultOpen>
                <div className="space-y-1.5">
                  {groupedRelated.outgoing.slice(0, isSheet ? 6 : 4).map(rel => (
                    <RelatedEntityRow key={rel.id} entity={rel} onNavigate={handleEntityClick} />
                  ))}
                  {groupedRelated.outgoing.length > (isSheet ? 6 : 4) && (
                    <Link
                      href={`/entities/${entityId}#related`}
                      className="block text-xs text-sc-fg-subtle hover:text-sc-purple px-2 py-1 transition-colors"
                    >
                      +{groupedRelated.outgoing.length - (isSheet ? 6 : 4)} more
                    </Link>
                  )}
                </div>
              </Section>
            )}

            {/* Incoming Relationships */}
            {groupedRelated.incoming.length > 0 && (
              <Section
                title="Referenced By"
                count={groupedRelated.incoming.length}
                defaultOpen={groupedRelated.outgoing.length === 0}
              >
                <div className="space-y-1.5">
                  {groupedRelated.incoming.slice(0, isSheet ? 6 : 4).map(rel => (
                    <RelatedEntityRow key={rel.id} entity={rel} onNavigate={handleEntityClick} />
                  ))}
                  {groupedRelated.incoming.length > (isSheet ? 6 : 4) && (
                    <Link
                      href={`/entities/${entityId}#related`}
                      className="block text-xs text-sc-fg-subtle hover:text-sc-purple px-2 py-1 transition-colors"
                    >
                      +{groupedRelated.incoming.length - (isSheet ? 6 : 4)} more
                    </Link>
                  )}
                </div>
              </Section>
            )}

            {/* Metadata */}
            <Section title="Metadata" defaultOpen={false}>
              <div className="text-xs space-y-1.5">
                {entity.source_file && (
                  <div className="flex items-start gap-2">
                    <span className="text-sc-fg-subtle shrink-0">Source:</span>
                    <span className="text-sc-cyan font-mono truncate">{entity.source_file}</span>
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
                {entity.category && (
                  <div className="flex items-center gap-2">
                    <span className="text-sc-fg-subtle">Category:</span>
                    <span className="text-sc-fg-muted">{entity.category}</span>
                  </div>
                )}
              </div>
            </Section>

            {/* Actions */}
            <div className="pt-3 border-t border-sc-fg-subtle/20">
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

// Related entity row component
function RelatedEntityRow({
  entity,
  onNavigate,
}: {
  entity: RelatedEntity;
  onNavigate?: (id: string) => void;
}) {
  return (
    <div className="group flex items-center gap-2 p-2 bg-sc-bg-highlight/50 hover:bg-sc-bg-highlight rounded-lg transition-colors">
      <EntityBadge type={entity.entity_type} size="sm" />

      {onNavigate ? (
        <button
          type="button"
          onClick={() => onNavigate(entity.id)}
          className="flex-1 text-left text-xs text-sc-fg-muted hover:text-sc-fg-primary truncate transition-colors"
          title={entity.name}
        >
          {entity.name}
        </button>
      ) : (
        <Link
          href={`/entities/${entity.id}`}
          className="flex-1 text-xs text-sc-fg-muted hover:text-sc-fg-primary truncate transition-colors"
          title={entity.name}
        >
          {entity.name}
        </Link>
      )}

      <RelationshipBadge type={entity.relationship} direction={entity.direction} size="xs" />

      <Link
        href={`/graph?selected=${entity.id}`}
        className="p-1 text-sc-fg-subtle hover:text-sc-purple opacity-0 group-hover:opacity-100 transition-all"
        title="View in graph"
      >
        <Network width={14} height={14} />
      </Link>
    </div>
  );
}
