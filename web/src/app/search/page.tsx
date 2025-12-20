'use client';

import { useSearchParams } from 'next/navigation';
import { Suspense, useEffect, useRef, useState } from 'react';

import { SearchInput, Input } from '@/components/ui/input';
import { Button } from '@/components/ui/button';
import { FilterChip } from '@/components/ui/toggle';
import { LoadingState } from '@/components/ui/spinner';
import { EmptyState, ErrorState } from '@/components/ui/tooltip';
import { PageHeader } from '@/components/layout/page-header';
import { SearchResultCard } from '@/components/search/search-result';
import type { SearchResult } from '@/lib/api';
import { useSearch, useStats } from '@/lib/hooks';
import { ENTITY_TYPES, TASK_STATUS_CONFIG, TASK_STATUSES } from '@/lib/constants';

// Curated searchable entity types
const SEARCHABLE_TYPES = [
  'pattern',
  'rule',
  'template',
  'task',
  'episode',
  'topic',
  'document',
] as const;

function SearchPageContent() {
  const searchParams = useSearchParams();
  const initialQuery = searchParams.get('q') || '';

  const [query, setQuery] = useState(initialQuery);
  const [submittedQuery, setSubmittedQuery] = useState(initialQuery);
  const [selectedTypes, setSelectedTypes] = useState<string[]>([]);
  const [selectedStatus, setSelectedStatus] = useState<string | null>(null);
  const [sinceDate, setSinceDate] = useState<string>('');
  const inputRef = useRef<HTMLInputElement>(null);

  const { data: stats } = useStats();

  // Check if task type is selected to show status filter
  const showStatusFilter = selectedTypes.includes('task');

  const {
    data: results,
    isLoading,
    error,
  } = useSearch(
    {
      query: submittedQuery,
      types: selectedTypes.length > 0 ? selectedTypes : undefined,
      limit: 50,
    },
    submittedQuery.length > 0
  );

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    setSubmittedQuery(query);
  };

  const toggleType = (type: string) => {
    setSelectedTypes((prev) => {
      const newTypes = prev.includes(type)
        ? prev.filter((t) => t !== type)
        : [...prev, type];
      // Clear status filter if task is deselected
      if (type === 'task' && prev.includes('task')) {
        setSelectedStatus(null);
      }
      return newTypes;
    });
  };

  const toggleStatus = (status: string) => {
    setSelectedStatus((prev) => (prev === status ? null : status));
  };

  // Filter results by status if selected
  const filteredResults = results?.results.filter((result: SearchResult) => {
    if (selectedStatus && result.type === 'task') {
      const taskStatus = (result as any).metadata?.status;
      return taskStatus === selectedStatus;
    }
    return true;
  });

  // Get type counts from stats
  const getTypeCount = (type: string) => stats?.entity_counts[type] ?? 0;

  return (
    <div className="space-y-6 animate-fade-in">
      <PageHeader
        title="Search Knowledge"
        description="Semantic search across all entities"
        meta={submittedQuery && results ? `${filteredResults?.length ?? 0} results` : undefined}
      />

      {/* Search Form */}
      <form onSubmit={handleSubmit} className="space-y-4">
        <div className="flex gap-3">
          <div className="flex-1">
            <SearchInput
              ref={inputRef}
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search for patterns, rules, templates..."
              onSubmit={() => setSubmittedQuery(query)}
            />
          </div>
          <Button type="submit" size="lg" disabled={!query.trim()}>
            Search
          </Button>
        </div>

        {/* Filters Section */}
        <div className="bg-sc-bg-base border border-sc-fg-subtle/20 rounded-xl p-4 space-y-4">
          {/* Entity Type Filters */}
          <div className="space-y-2">
            <div className="flex items-center gap-2">
              <span className="text-sc-fg-muted text-sm font-medium">Entity Type</span>
              {selectedTypes.length > 0 && (
                <button
                  type="button"
                  onClick={() => {
                    setSelectedTypes([]);
                    setSelectedStatus(null);
                  }}
                  className="text-xs text-sc-purple hover:underline"
                >
                  Clear
                </button>
              )}
            </div>
            <div className="flex flex-wrap gap-2">
              {SEARCHABLE_TYPES.map((type) => {
                const count = getTypeCount(type);
                return (
                  <FilterChip
                    key={type}
                    active={selectedTypes.includes(type)}
                    onClick={() => toggleType(type)}
                  >
                    {type.replace(/_/g, ' ')}
                    {count > 0 && (
                      <span className="ml-1 text-[10px] opacity-70">({count})</span>
                    )}
                  </FilterChip>
                );
              })}
            </div>
          </div>

          {/* Task Status Filter (shown when task type selected) */}
          {showStatusFilter && (
            <div className="space-y-2 pt-2 border-t border-sc-fg-subtle/10">
              <div className="flex items-center gap-2">
                <span className="text-sc-fg-muted text-sm font-medium">Task Status</span>
                {selectedStatus && (
                  <button
                    type="button"
                    onClick={() => setSelectedStatus(null)}
                    className="text-xs text-sc-purple hover:underline"
                  >
                    Clear
                  </button>
                )}
              </div>
              <div className="flex flex-wrap gap-2">
                {TASK_STATUSES.map((status) => {
                  const config = TASK_STATUS_CONFIG[status];
                  return (
                    <FilterChip
                      key={status}
                      active={selectedStatus === status}
                      onClick={() => toggleStatus(status)}
                    >
                      <span className={selectedStatus === status ? '' : config.textClass}>
                        {config.icon}
                      </span>
                      <span className="ml-1">{config.label}</span>
                    </FilterChip>
                  );
                })}
              </div>
            </div>
          )}

          {/* Date Range Filter */}
          <div className="space-y-2 pt-2 border-t border-sc-fg-subtle/10">
            <div className="flex items-center gap-2">
              <span className="text-sc-fg-muted text-sm font-medium">Created Since</span>
              {sinceDate && (
                <button
                  type="button"
                  onClick={() => setSinceDate('')}
                  className="text-xs text-sc-purple hover:underline"
                >
                  Clear
                </button>
              )}
            </div>
            <div className="flex flex-wrap gap-2">
              <button
                type="button"
                onClick={() => {
                  const d = new Date();
                  d.setDate(d.getDate() - 7);
                  setSinceDate(d.toISOString().split('T')[0]);
                }}
                className={`text-xs px-2 py-1 rounded border transition-colors ${
                  sinceDate && new Date(sinceDate) >= new Date(Date.now() - 7 * 24 * 60 * 60 * 1000)
                    ? 'bg-sc-purple/20 border-sc-purple/40 text-sc-purple'
                    : 'border-sc-fg-subtle/20 text-sc-fg-muted hover:border-sc-fg-subtle/40'
                }`}
              >
                Last 7 days
              </button>
              <button
                type="button"
                onClick={() => {
                  const d = new Date();
                  d.setMonth(d.getMonth() - 1);
                  setSinceDate(d.toISOString().split('T')[0]);
                }}
                className={`text-xs px-2 py-1 rounded border transition-colors ${
                  sinceDate && new Date(sinceDate) >= new Date(Date.now() - 30 * 24 * 60 * 60 * 1000) &&
                  new Date(sinceDate) < new Date(Date.now() - 7 * 24 * 60 * 60 * 1000)
                    ? 'bg-sc-purple/20 border-sc-purple/40 text-sc-purple'
                    : 'border-sc-fg-subtle/20 text-sc-fg-muted hover:border-sc-fg-subtle/40'
                }`}
              >
                Last 30 days
              </button>
              <input
                type="date"
                value={sinceDate}
                onChange={(e) => setSinceDate(e.target.value)}
                className="text-xs px-2 py-1 rounded border border-sc-fg-subtle/20 bg-sc-bg-elevated text-sc-fg-primary focus:outline-none focus:border-sc-purple/40"
              />
            </div>
          </div>
        </div>
      </form>

      {/* Results */}
      {submittedQuery ? (
        <div className="space-y-4">
          {isLoading ? (
            <LoadingState message="Searching knowledge base..." />
          ) : error ? (
            <ErrorState title="Search failed" message={error.message} />
          ) : filteredResults && filteredResults.length > 0 ? (
            <>
              <div className="flex items-center justify-between">
                <div className="text-sc-fg-muted text-sm">
                  Found {filteredResults.length} results for "{submittedQuery}"
                  {selectedTypes.length > 0 && (
                    <span className="text-sc-fg-subtle">
                      {' '}in {selectedTypes.join(', ')}
                    </span>
                  )}
                  {selectedStatus && (
                    <span className="text-sc-fg-subtle"> with status {selectedStatus}</span>
                  )}
                </div>
              </div>
              <div className="space-y-3">
                {filteredResults.map((result: SearchResult) => (
                  <SearchResultCard key={result.id} result={result} />
                ))}
              </div>
            </>
          ) : (
            <EmptyState
              icon="∅"
              title="No results found"
              description="Try different keywords or remove some filters"
            />
          )}
        </div>
      ) : (
        <EmptyState
          icon="⌕"
          title="Enter a search query to find knowledge"
          description="Semantic search finds related concepts, not just exact matches"
        />
      )}
    </div>
  );
}

export default function SearchPage() {
  return (
    <Suspense fallback={<LoadingState />}>
      <SearchPageContent />
    </Suspense>
  );
}
