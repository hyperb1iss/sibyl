'use client';

import { useRouter, useSearchParams } from 'next/navigation';
import { Suspense, useCallback, useEffect, useMemo, useState } from 'react';
import { EpicList } from '@/components/epics';
import { Breadcrumb } from '@/components/layout/breadcrumb';
import { ChevronDown, Search, X } from '@/components/ui/icons';
import { LoadingState } from '@/components/ui/spinner';
import { FilterChip } from '@/components/ui/toggle';
import { ErrorState } from '@/components/ui/tooltip';
import type { EpicStatus } from '@/lib/api';
import { EPIC_STATUS_CONFIG } from '@/lib/constants';
import { useEpics, useProjects } from '@/lib/hooks';
import { useProjectFilter } from '@/lib/project-context';
import { readStorage, writeStorage } from '@/lib/storage';

const EPIC_STATUSES: EpicStatus[] = ['planning', 'in_progress', 'blocked', 'completed', 'archived'];

type SortOption = 'name_asc' | 'name_desc' | 'updated_desc' | 'priority' | 'progress';
const SORT_OPTIONS: { value: SortOption; label: string }[] = [
  { value: 'updated_desc', label: 'Recently Updated' },
  { value: 'name_asc', label: 'Name (A–Z)' },
  { value: 'name_desc', label: 'Name (Z–A)' },
  { value: 'priority', label: 'Priority' },
  { value: 'progress', label: 'Progress' },
];

// Priority order for sorting (lower = higher priority)
const PRIORITY_ORDER: Record<string, number> = {
  critical: 0,
  high: 1,
  medium: 2,
  low: 3,
};

function EpicsPageContent() {
  const router = useRouter();
  const searchParams = useSearchParams();

  // Project filtering is handled by global context (header selector)
  const projectFilter = useProjectFilter();

  // Parse multi-status filter from URL (comma-separated)
  const statusParam = searchParams.get('status');
  const selectedStatuses = useMemo(() => {
    if (!statusParam) return new Set<EpicStatus>();
    return new Set(
      statusParam.split(',').filter(s => EPIC_STATUSES.includes(s as EpicStatus)) as EpicStatus[]
    );
  }, [statusParam]);

  // Sort option from URL
  const sortOption = (searchParams.get('sort') as SortOption) || 'updated_desc';

  const [searchQuery, setSearchQuery] = useState('');
  const [isSortOpen, setIsSortOpen] = useState(false);
  const [hasRestoredFromStorage, setHasRestoredFromStorage] = useState(false);

  // Restore filters from localStorage on mount (only if URL has no filter params)
  useEffect(() => {
    if (hasRestoredFromStorage) return;
    setHasRestoredFromStorage(true);

    // If URL already has params, don't override with stored values
    if (searchParams.get('status') || searchParams.get('sort')) return;

    const stored = readStorage<{ status?: string; sort?: string }>('epics:filters');
    if (stored) {
      const params = new URLSearchParams();
      if (stored.status) params.set('status', stored.status);
      if (stored.sort && stored.sort !== 'updated_desc') params.set('sort', stored.sort);
      if (params.toString()) {
        router.replace(`/epics?${params.toString()}`);
      }
    }
  }, [hasRestoredFromStorage, router, searchParams]);

  // Save filters to localStorage when they change
  useEffect(() => {
    if (!hasRestoredFromStorage) return; // Don't save during initial restore
    const toStore =
      statusParam || sortOption !== 'updated_desc'
        ? {
            status: statusParam || undefined,
            sort: sortOption !== 'updated_desc' ? sortOption : undefined,
          }
        : undefined;
    writeStorage('epics:filters', toStore);
  }, [hasRestoredFromStorage, statusParam, sortOption]);

  // Fetch all epics (filtering done client-side for multi-status)
  const { data: epicsData, isLoading, error } = useEpics({ project: projectFilter });
  const { data: projectsData } = useProjects();

  const projects = projectsData?.entities ?? [];
  const allEpics = epicsData?.entities ?? [];

  // Build project name lookup
  const projectNames = useMemo(() => {
    const lookup: Record<string, string> = {};
    for (const project of projects) {
      lookup[project.id] = project.name;
    }
    return lookup;
  }, [projects]);

  // Filter epics by status and search query, then sort
  const epics = useMemo(() => {
    let filtered = allEpics;

    // Filter by statuses (if any selected)
    if (selectedStatuses.size > 0) {
      filtered = filtered.filter(epic => {
        const status = epic.metadata?.status as EpicStatus | undefined;
        return status && selectedStatuses.has(status);
      });
    }

    // Filter by search query
    if (searchQuery.trim()) {
      const query = searchQuery.toLowerCase();
      filtered = filtered.filter(epic => {
        const name = epic.name?.toLowerCase() ?? '';
        const description = epic.description?.toLowerCase() ?? '';
        return name.includes(query) || description.includes(query);
      });
    }

    // Sort
    return [...filtered].sort((a, b) => {
      switch (sortOption) {
        case 'name_asc':
          return (a.name ?? '').localeCompare(b.name ?? '');
        case 'name_desc':
          return (b.name ?? '').localeCompare(a.name ?? '');
        case 'priority': {
          const aPri = PRIORITY_ORDER[a.metadata?.priority as string] ?? 99;
          const bPri = PRIORITY_ORDER[b.metadata?.priority as string] ?? 99;
          return aPri - bPri;
        }
        case 'progress': {
          const aProgress =
            ((a.metadata?.tasks_done as number) ?? 0) / ((a.metadata?.tasks_total as number) || 1);
          const bProgress =
            ((b.metadata?.tasks_done as number) ?? 0) / ((b.metadata?.tasks_total as number) || 1);
          return bProgress - aProgress; // Higher progress first
        }
        default: {
          // Sort by updated_at, falling back to ID for consistent ordering
          const aTime = new Date((a.metadata?.updated_at as string) ?? 0).getTime();
          const bTime = new Date((b.metadata?.updated_at as string) ?? 0).getTime();
          return bTime - aTime || a.id.localeCompare(b.id);
        }
      }
    });
  }, [allEpics, selectedStatuses, searchQuery, sortOption]);

  // Toggle a status in the multi-select
  const handleStatusToggle = useCallback(
    (status: EpicStatus) => {
      const params = new URLSearchParams(searchParams);
      const newStatuses = new Set(selectedStatuses);

      if (newStatuses.has(status)) {
        newStatuses.delete(status);
      } else {
        newStatuses.add(status);
      }

      if (newStatuses.size > 0) {
        params.set('status', Array.from(newStatuses).join(','));
      } else {
        params.delete('status');
      }
      router.push(`/epics?${params.toString()}`);
    },
    [router, searchParams, selectedStatuses]
  );

  // Clear all status filters
  const handleClearStatuses = useCallback(() => {
    const params = new URLSearchParams(searchParams);
    params.delete('status');
    router.push(`/epics?${params.toString()}`);
  }, [router, searchParams]);

  // Handle sort change
  const handleSortChange = useCallback(
    (sort: SortOption) => {
      const params = new URLSearchParams(searchParams);
      if (sort === 'updated_desc') {
        params.delete('sort'); // Default, no need to persist
      } else {
        params.set('sort', sort);
      }
      router.push(`/epics?${params.toString()}`);
      setIsSortOpen(false);
    },
    [router, searchParams]
  );

  return (
    <div className="space-y-4 animate-fade-in">
      <Breadcrumb />

      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold text-sc-fg-primary flex items-center gap-2">
            <span className="text-[#ffb86c]">◈</span>
            Epics
          </h1>
          <p className="text-sm text-sc-fg-muted mt-1">
            Feature initiatives that group related tasks
          </p>
        </div>
      </div>

      {/* Search + Filters */}
      <div className="space-y-3">
        {/* Search + Sort Row */}
        <div className="flex gap-3">
          {/* Search Input */}
          <div className="relative flex-1">
            <Search
              width={16}
              height={16}
              className="absolute left-3 top-1/2 -translate-y-1/2 text-sc-fg-subtle"
            />
            <input
              type="text"
              value={searchQuery}
              onChange={e => setSearchQuery(e.target.value)}
              placeholder="Search epics..."
              className="w-full pl-9 pr-3 py-2 bg-sc-bg-elevated border border-sc-fg-subtle/20 rounded-lg text-sm text-sc-fg-primary placeholder:text-sc-fg-subtle focus:border-sc-purple focus:outline-none focus:ring-2 focus:ring-sc-purple/10 transition-all"
            />
            {searchQuery && (
              <button
                type="button"
                onClick={() => setSearchQuery('')}
                className="absolute right-3 top-1/2 -translate-y-1/2 text-sc-fg-subtle hover:text-sc-fg-primary"
              >
                <X width={14} height={14} />
              </button>
            )}
          </div>

          {/* Sort Dropdown */}
          <div className="relative">
            <button
              type="button"
              onClick={() => setIsSortOpen(!isSortOpen)}
              className="flex items-center gap-2 px-3 py-2 bg-sc-bg-elevated border border-sc-fg-subtle/20 rounded-lg text-sm text-sc-fg-muted hover:text-sc-fg-primary hover:border-sc-fg-subtle/40 transition-all min-w-[160px]"
            >
              <span className="flex-1 text-left">
                {SORT_OPTIONS.find(o => o.value === sortOption)?.label ?? 'Sort'}
              </span>
              <ChevronDown
                width={14}
                height={14}
                className={`transition-transform ${isSortOpen ? 'rotate-180' : ''}`}
              />
            </button>
            {isSortOpen && (
              <>
                {/* Backdrop to close dropdown */}
                <div
                  className="fixed inset-0 z-10"
                  onClick={() => setIsSortOpen(false)}
                  onKeyDown={e => e.key === 'Escape' && setIsSortOpen(false)}
                />
                <div className="absolute right-0 top-full mt-1 z-20 bg-sc-bg-elevated border border-sc-fg-subtle/20 rounded-lg shadow-lg py-1 min-w-[160px]">
                  {SORT_OPTIONS.map(option => (
                    <button
                      key={option.value}
                      type="button"
                      onClick={() => handleSortChange(option.value)}
                      className={`w-full px-3 py-1.5 text-left text-sm transition-colors ${
                        sortOption === option.value
                          ? 'text-sc-purple bg-sc-purple/10'
                          : 'text-sc-fg-muted hover:text-sc-fg-primary hover:bg-sc-bg-highlight'
                      }`}
                    >
                      {option.label}
                    </button>
                  ))}
                </div>
              </>
            )}
          </div>
        </div>

        {/* Status Filter (Multi-select) */}
        <div className="flex items-center gap-2">
          <div className="flex flex-wrap items-center gap-2 flex-1">
            <span className="text-xs text-sc-fg-subtle font-medium">Status:</span>
            {EPIC_STATUSES.map(status => {
              const config = EPIC_STATUS_CONFIG[status];
              const isActive = selectedStatuses.has(status);
              return (
                <FilterChip
                  key={status}
                  active={isActive}
                  onClick={() => handleStatusToggle(status)}
                >
                  <span className="flex items-center gap-1">
                    <span>{config?.icon}</span>
                    {config?.label ?? status}
                  </span>
                </FilterChip>
              );
            })}
            {selectedStatuses.size === 0 && (
              <span className="text-xs text-sc-fg-subtle italic">All</span>
            )}
          </div>
          {selectedStatuses.size > 0 && (
            <button
              type="button"
              onClick={handleClearStatuses}
              className="text-xs text-sc-fg-muted hover:text-sc-fg-primary flex items-center gap-1 px-2 py-1 rounded bg-sc-bg-elevated hover:bg-sc-bg-highlight transition-colors shrink-0"
            >
              <X width={12} height={12} />
              Clear
            </button>
          )}
        </div>
      </div>

      {/* Epic Grid */}
      {error ? (
        <ErrorState
          title="Failed to load epics"
          message={error instanceof Error ? error.message : 'Unknown error'}
        />
      ) : (
        <EpicList
          epics={epics}
          projectNames={projectNames}
          showProject={!projectFilter}
          isLoading={isLoading}
          isFiltered={!!searchQuery || !!projectFilter || selectedStatuses.size > 0}
        />
      )}
    </div>
  );
}

export default function EpicsPage() {
  return (
    <Suspense fallback={<LoadingState />}>
      <EpicsPageContent />
    </Suspense>
  );
}
