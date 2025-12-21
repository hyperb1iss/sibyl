'use client';

import { useRouter, useSearchParams } from 'next/navigation';
import { useCallback, useState } from 'react';

import { Input } from '@/components/ui/input';
import { Button } from '@/components/ui/button';
import { FilterChip } from '@/components/ui/toggle';
import { LoadingState } from '@/components/ui/spinner';
import { EmptyState, ErrorState } from '@/components/ui/tooltip';
import { PageHeader } from '@/components/layout/page-header';
import { EntityCard } from '@/components/entities/entity-card';
import { useDeleteEntity, useEntities, useStats } from '@/lib/hooks';
import type { EntityListResponse, StatsResponse } from '@/lib/api';

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
      (e) =>
        !searchQuery ||
        e.name.toLowerCase().includes(searchQuery.toLowerCase()) ||
        e.description?.toLowerCase().includes(searchQuery.toLowerCase())
    ) || [];

  const totalPages = data ? Math.ceil(data.total / limit) : 0;

  return (
    <div className="space-y-6 animate-fade-in">
      <PageHeader
        title="Entities"
        description="Browse and manage knowledge entities"
        meta={`${data?.total ?? 0} total entities`}
      />

      {/* Filters */}
      <div className="flex flex-col md:flex-row gap-4">
        <div className="flex-1">
          <Input
            type="text"
            placeholder="Filter entities..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            icon="⌕"
          />
        </div>

        {/* Type Filter */}
        <div className="flex flex-wrap gap-2">
          <FilterChip active={!typeFilter} onClick={() => handleTypeFilter(null)}>
            All
          </FilterChip>
          {entityTypes.map((type) => (
            <FilterChip
              key={type}
              active={typeFilter === type}
              onClick={() => handleTypeFilter(type)}
            >
              {type.replace(/_/g, ' ')}
            </FilterChip>
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
          <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
            {filteredEntities.map((entity) => (
              <EntityCard key={entity.id} entity={entity} onDelete={handleDelete} />
            ))}
          </div>

          {/* Pagination */}
          {totalPages > 1 && (
            <div className="flex items-center justify-center gap-2">
              <Button
                variant="secondary"
                onClick={() => handlePageChange(page - 1)}
                disabled={page <= 1}
              >
                ← Prev
              </Button>
              <span className="px-4 py-2 text-sc-fg-muted">
                Page {page} of {totalPages}
              </span>
              <Button
                variant="secondary"
                onClick={() => handlePageChange(page + 1)}
                disabled={page >= totalPages}
              >
                Next →
              </Button>
            </div>
          )}
        </>
      )}
    </div>
  );
}
