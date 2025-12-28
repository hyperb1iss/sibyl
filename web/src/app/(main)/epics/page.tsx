'use client';

import { useRouter, useSearchParams } from 'next/navigation';
import { Suspense, useCallback, useMemo, useState } from 'react';
import { EpicList } from '@/components/epics';
import { Breadcrumb } from '@/components/layout/breadcrumb';
import { Search, X } from '@/components/ui/icons';
import { LoadingState } from '@/components/ui/spinner';
import { FilterChip } from '@/components/ui/toggle';
import { ErrorState } from '@/components/ui/tooltip';
import type { EpicStatus } from '@/lib/api';
import { EPIC_STATUS_CONFIG } from '@/lib/constants';
import { useEpics, useProjects } from '@/lib/hooks';

const EPIC_STATUSES: EpicStatus[] = ['planning', 'in_progress', 'blocked', 'completed', 'archived'];

function EpicsPageContent() {
  const router = useRouter();
  const searchParams = useSearchParams();

  const projectFilter = searchParams.get('project') || undefined;
  const statusFilter = (searchParams.get('status') as EpicStatus) || undefined;
  const [searchQuery, setSearchQuery] = useState('');

  const {
    data: epicsData,
    isLoading,
    error,
  } = useEpics({ project: projectFilter, status: statusFilter });
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

  // Filter epics by search query
  const epics = useMemo(() => {
    if (!searchQuery.trim()) return allEpics;
    const query = searchQuery.toLowerCase();
    return allEpics.filter(epic => {
      const name = epic.name?.toLowerCase() ?? '';
      const description = epic.description?.toLowerCase() ?? '';
      return name.includes(query) || description.includes(query);
    });
  }, [allEpics, searchQuery]);

  const handleProjectFilter = useCallback(
    (projectId: string | null) => {
      const params = new URLSearchParams(searchParams);
      if (projectId) {
        params.set('project', projectId);
      } else {
        params.delete('project');
      }
      router.push(`/epics?${params.toString()}`);
    },
    [router, searchParams]
  );

  const handleStatusFilter = useCallback(
    (status: EpicStatus | null) => {
      const params = new URLSearchParams(searchParams);
      if (status) {
        params.set('status', status);
      } else {
        params.delete('status');
      }
      router.push(`/epics?${params.toString()}`);
    },
    [router, searchParams]
  );

  // Find current project name for filtered view
  const currentProjectName = projectFilter
    ? projects.find(p => p.id === projectFilter)?.name
    : undefined;

  return (
    <div className="space-y-4 animate-fade-in">
      <Breadcrumb />

      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold text-sc-fg-primary flex items-center gap-2">
            <span className="text-[#ffb86c]">◈</span>
            Epics
            {currentProjectName && (
              <span className="text-sc-fg-muted font-normal">in {currentProjectName}</span>
            )}
          </h1>
          <p className="text-sm text-sc-fg-muted mt-1">
            Feature initiatives that group related tasks
          </p>
        </div>
      </div>

      {/* Search + Filters */}
      <div className="space-y-3">
        {/* Search Input */}
        <div className="relative">
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

        {/* Project Filter */}
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-xs text-sc-fg-subtle font-medium">Project:</span>
          <FilterChip active={!projectFilter} onClick={() => handleProjectFilter(null)}>
            All
          </FilterChip>
          {projects.map(project => (
            <FilterChip
              key={project.id}
              active={projectFilter === project.id}
              onClick={() => handleProjectFilter(project.id)}
            >
              {project.name}
            </FilterChip>
          ))}
        </div>

        {/* Status Filter */}
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-xs text-sc-fg-subtle font-medium">Status:</span>
          <FilterChip active={!statusFilter} onClick={() => handleStatusFilter(null)}>
            All
          </FilterChip>
          {EPIC_STATUSES.map(status => {
            const config = EPIC_STATUS_CONFIG[status];
            return (
              <FilterChip
                key={status}
                active={statusFilter === status}
                onClick={() => handleStatusFilter(status)}
              >
                <span className="flex items-center gap-1">
                  <span>{config?.icon}</span>
                  {config?.label ?? status}
                </span>
              </FilterChip>
            );
          })}
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
          emptyMessage={
            searchQuery
              ? 'No epics match your search'
              : projectFilter
                ? `No epics in ${currentProjectName || 'this project'}`
                : 'No epics yet'
          }
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
