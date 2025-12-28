'use client';

import { AnimatePresence, motion } from 'motion/react';
import { useRouter, useSearchParams } from 'next/navigation';
import { useCallback, useState } from 'react';
import { toast } from 'sonner';
import { EntityCard } from '@/components/entities/entity-card';
import { Breadcrumb } from '@/components/layout/breadcrumb';
import { PageHeader } from '@/components/layout/page-header';
import { Button } from '@/components/ui/button';
import { EntitiesEmptyState } from '@/components/ui/empty-state';
import { Input } from '@/components/ui/input';
import { LoadingState } from '@/components/ui/spinner';
import { EntityTypeChip, FilterChip } from '@/components/ui/toggle';
import { ErrorState } from '@/components/ui/tooltip';
import type { EntityListResponse, StatsResponse } from '@/lib/api';
import { useDeleteEntity, useEntities, useStats } from '@/lib/hooks';

interface EntitiesContentProps {
  initialEntities: EntityListResponse;
  initialStats: StatsResponse;
  typeFilter?: string;
  page: number;
}

export function EntitiesContent({
  initialEntities,
  initialStats,
  typeFilter,
  page,
}: EntitiesContentProps) {
  const router = useRouter();
  const searchParams = useSearchParams();
  const limit = 20;

  const [searchQuery, setSearchQuery] = useState('');

  // Hydrate from server data, then use client cache
  const { data, isLoading, error } = useEntities(
    { entity_type: typeFilter, page, page_size: limit },
    initialEntities
  );

  const { data: stats } = useStats(initialStats);
  const deleteEntity = useDeleteEntity();

  const entityTypes = stats ? Object.keys(stats.entity_counts).sort() : [];

  const handleTypeFilter = useCallback(
    (type: string | null) => {
      const params = new URLSearchParams(searchParams);
      if (type) {
        params.set('type', type);
      } else {
        params.delete('type');
      }
      params.set('page', '1');
      router.push(`/entities?${params.toString()}`);
    },
    [router, searchParams]
  );

  const handlePageChange = useCallback(
    (newPage: number) => {
      const params = new URLSearchParams(searchParams);
      params.set('page', newPage.toString());
      router.push(`/entities?${params.toString()}`);
    },
    [router, searchParams]
  );

  const handleDelete = useCallback(
    async (id: string) => {
      if (confirm('Are you sure you want to delete this entity?')) {
        try {
          await deleteEntity.mutateAsync(id);
          toast.success('Entity deleted');
        } catch (_err) {
          toast.error('Failed to delete entity');
        }
      }
    },
    [deleteEntity]
  );

  // Deduplicate entities by ID (API may return duplicates) and filter by search
  const filteredEntities = (() => {
    if (!data?.entities) return [];
    const seen = new Set<string>();
    return data.entities.filter(e => {
      if (seen.has(e.id)) return false;
      seen.add(e.id);
      if (!searchQuery) return true;
      const query = searchQuery.toLowerCase();
      return e.name.toLowerCase().includes(query) || e.description?.toLowerCase().includes(query);
    });
  })();

  const totalPages = data ? Math.ceil(data.total / limit) : 0;

  return (
    <div className="space-y-4 animate-fade-in">
      <Breadcrumb />

      <PageHeader
        description="Browse and manage knowledge entities"
        meta={`${data?.total ?? 0} total`}
      />

      {/* Filters */}
      <div className="flex flex-col gap-3 sm:gap-4">
        <div className="flex-1 sm:max-w-md">
          <Input
            type="text"
            placeholder="Filter entities..."
            value={searchQuery}
            onChange={e => setSearchQuery(e.target.value)}
            icon="⌕"
          />
        </div>

        {/* Type Filter - scrollable on mobile */}
        <div className="flex flex-wrap gap-1.5 sm:gap-2">
          <FilterChip active={!typeFilter} onClick={() => handleTypeFilter(null)}>
            All
          </FilterChip>
          {entityTypes.map(type => (
            <EntityTypeChip
              key={type}
              entityType={type}
              active={typeFilter === type}
              onClick={() => handleTypeFilter(type)}
              count={stats?.entity_counts[type]}
            />
          ))}
        </div>
      </div>

      {/* Content */}
      {isLoading ? (
        <LoadingState />
      ) : error ? (
        <ErrorState title="Failed to load entities" message={error.message} />
      ) : filteredEntities.length === 0 ? (
        <EntitiesEmptyState
          entityType={typeFilter}
          onClearFilter={typeFilter ? () => handleTypeFilter(null) : undefined}
        />
      ) : (
        <>
          {/* Entity Grid */}
          <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 gap-3 sm:gap-4">
            <AnimatePresence mode="popLayout">
              {filteredEntities.map((entity, index) => (
                <motion.div
                  key={entity.id}
                  layout
                  initial={{ opacity: 0, scale: 0.95 }}
                  animate={{ opacity: 1, scale: 1 }}
                  exit={{ opacity: 0, scale: 0.95 }}
                  transition={{
                    layout: { type: 'spring', stiffness: 350, damping: 30 },
                    opacity: { duration: 0.2, delay: index * 0.02 },
                    scale: { duration: 0.2, delay: index * 0.02 },
                  }}
                >
                  <EntityCard entity={entity} onDelete={handleDelete} />
                </motion.div>
              ))}
            </AnimatePresence>
          </div>

          {/* Pagination */}
          {totalPages > 1 && (
            <div className="flex items-center justify-center gap-1.5 sm:gap-2">
              <Button
                variant="secondary"
                onClick={() => handlePageChange(page - 1)}
                disabled={page <= 1}
              >
                <span className="hidden xs:inline">←</span> Prev
              </Button>
              <span className="px-2 sm:px-4 py-2 text-xs sm:text-sm text-sc-fg-muted">
                {page}/{totalPages}
              </span>
              <Button
                variant="secondary"
                onClick={() => handlePageChange(page + 1)}
                disabled={page >= totalPages}
              >
                Next <span className="hidden xs:inline">→</span>
              </Button>
            </div>
          )}
        </>
      )}
    </div>
  );
}
