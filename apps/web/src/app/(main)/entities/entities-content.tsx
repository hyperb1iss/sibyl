'use client';

import { AnimatePresence, motion } from 'motion/react';
import { useRouter, useSearchParams } from 'next/navigation';
import { useCallback, useEffect, useRef, useState } from 'react';
import { toast } from 'sonner';
import { EntityCard } from '@/components/entities/entity-card';
import { Breadcrumb } from '@/components/layout/breadcrumb';
import { PageHeader } from '@/components/layout/page-header';
import { Button } from '@/components/ui/button';
import { EntitiesEmptyState } from '@/components/ui/empty-state';
import { Input } from '@/components/ui/input';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { LoadingState } from '@/components/ui/spinner';
import { EntityTypeChip, FilterChip } from '@/components/ui/toggle';
import { ErrorState } from '@/components/ui/tooltip';
import type { EntityListResponse, EntitySortField, SortOrder, StatsResponse } from '@/lib/api';
import { useDeleteEntity, useEntities, useStats } from '@/lib/hooks';
import { useProjectContext } from '@/lib/project-context';

interface EntitiesContentProps {
  initialEntities: EntityListResponse;
  initialStats: StatsResponse;
  typeFilter?: string;
  search: string;
  page: number;
  sortBy: EntitySortField;
  sortOrder: SortOrder;
}

const SORT_OPTIONS: { value: string; label: string; field: EntitySortField; order: SortOrder }[] = [
  { value: 'updated_at-desc', label: 'Recently Updated', field: 'updated_at', order: 'desc' },
  { value: 'updated_at-asc', label: 'Oldest Updated', field: 'updated_at', order: 'asc' },
  { value: 'created_at-desc', label: 'Newest First', field: 'created_at', order: 'desc' },
  { value: 'created_at-asc', label: 'Oldest First', field: 'created_at', order: 'asc' },
  { value: 'name-asc', label: 'Name A-Z', field: 'name', order: 'asc' },
  { value: 'name-desc', label: 'Name Z-A', field: 'name', order: 'desc' },
  { value: 'entity_type-asc', label: 'Type A-Z', field: 'entity_type', order: 'asc' },
];

export function EntitiesContent({
  initialEntities,
  initialStats,
  typeFilter,
  search,
  page,
  sortBy,
  sortOrder,
}: EntitiesContentProps) {
  const router = useRouter();
  const searchParams = useSearchParams();
  const limit = 20;

  // Project context for filtering
  const { selectedProjects, isAll } = useProjectContext();

  // Local state for input (synced from URL, debounced to URL)
  const [searchInput, setSearchInput] = useState(search);
  const debounceRef = useRef<NodeJS.Timeout | null>(null);

  // Sync input when URL search changes (e.g., browser back/forward)
  useEffect(() => {
    setSearchInput(search);
  }, [search]);

  // Debounced search - update URL after 300ms of no typing
  const handleSearchChange = useCallback(
    (value: string) => {
      setSearchInput(value);

      if (debounceRef.current) {
        clearTimeout(debounceRef.current);
      }

      debounceRef.current = setTimeout(() => {
        const params = new URLSearchParams(searchParams);
        if (value.trim()) {
          params.set('search', value.trim());
        } else {
          params.delete('search');
        }
        params.set('page', '1'); // Reset to first page on search
        router.push(`/entities?${params.toString()}`);
      }, 300);
    },
    [router, searchParams]
  );

  // Cleanup debounce on unmount
  useEffect(() => {
    return () => {
      if (debounceRef.current) {
        clearTimeout(debounceRef.current);
      }
    };
  }, []);

  // Hydrate from server data, then use client cache
  const { data, isLoading, error } = useEntities(
    {
      entity_type: typeFilter,
      search: search || undefined,
      project_ids: isAll ? undefined : selectedProjects,
      page,
      page_size: limit,
      sort_by: sortBy,
      sort_order: sortOrder,
    },
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

  const handleClearFilters = useCallback(() => {
    const params = new URLSearchParams(searchParams);
    params.delete('type');
    params.delete('search');
    params.set('page', '1');
    setSearchInput('');
    router.push(`/entities?${params.toString()}`);
  }, [router, searchParams]);

  const handlePageChange = useCallback(
    (newPage: number) => {
      const params = new URLSearchParams(searchParams);
      params.set('page', newPage.toString());
      router.push(`/entities?${params.toString()}`);
    },
    [router, searchParams]
  );

  const handleSortChange = useCallback(
    (value: string) => {
      const option = SORT_OPTIONS.find(o => o.value === value);
      if (!option) return;

      const params = new URLSearchParams(searchParams);
      params.set('sort_by', option.field);
      params.set('sort_order', option.order);
      params.set('page', '1'); // Reset to first page on sort change
      router.push(`/entities?${params.toString()}`);
    },
    [router, searchParams]
  );

  const currentSortValue = `${sortBy}-${sortOrder}`;

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

  // Deduplicate entities by ID (API may return duplicates)
  const entities = (() => {
    if (!data?.entities) return [];
    const seen = new Set<string>();
    return data.entities.filter(e => {
      if (seen.has(e.id)) return false;
      seen.add(e.id);
      return true;
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
        <div className="flex flex-col sm:flex-row gap-3 sm:items-center">
          <div className="flex-1 sm:max-w-md">
            <Input
              type="text"
              placeholder="Search entities..."
              value={searchInput}
              onChange={e => handleSearchChange(e.target.value)}
              icon="⌕"
            />
          </div>
          <div className="flex items-center gap-2">
            <span className="text-xs text-sc-fg-muted whitespace-nowrap">Sort by:</span>
            <Select value={currentSortValue} onValueChange={handleSortChange}>
              <SelectTrigger className="w-[180px]">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {SORT_OPTIONS.map(option => (
                  <SelectItem key={option.value} value={option.value}>
                    {option.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
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
      ) : entities.length === 0 ? (
        <EntitiesEmptyState
          entityType={typeFilter}
          searchQuery={search}
          onClearFilter={typeFilter || search ? () => handleClearFilters() : undefined}
        />
      ) : (
        <>
          {/* Entity Grid */}
          <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 gap-3 sm:gap-4">
            <AnimatePresence mode="popLayout">
              {entities.map((entity, index) => (
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
