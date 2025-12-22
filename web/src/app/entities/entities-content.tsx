'use client';

import { useRouter, useSearchParams } from 'next/navigation';
import { useCallback, useState } from 'react';
import { EntityCard } from '@/components/entities/entity-card';
import { Breadcrumb } from '@/components/layout/breadcrumb';
import { PageHeader } from '@/components/layout/page-header';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { LoadingState } from '@/components/ui/spinner';
import { EntityTypeChip, FilterChip } from '@/components/ui/toggle';
import { EmptyState, ErrorState } from '@/components/ui/tooltip';
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

  const entityTypes = stats ? Object.keys(stats.entity_counts) : [];

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
        await deleteEntity.mutateAsync(id);
      }
    },
    [deleteEntity]
  );

  const filteredEntities =
    data?.entities.filter(
      e =>
        !searchQuery ||
        e.name.toLowerCase().includes(searchQuery.toLowerCase()) ||
        e.description?.toLowerCase().includes(searchQuery.toLowerCase())
    ) || [];

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
        <EmptyState
          icon="▣"
          title="No entities found"
          description={
            searchQuery
              ? 'Try a different search term'
              : 'Ingest some documents to populate entities'
          }
        />
      ) : (
        <>
          {/* Entity Grid */}
          <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 gap-3 sm:gap-4">
            {filteredEntities.map(entity => (
              <EntityCard key={entity.id} entity={entity} onDelete={handleDelete} />
            ))}
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
