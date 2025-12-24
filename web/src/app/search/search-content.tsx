'use client';

import { useRouter, useSearchParams } from 'next/navigation';
import { useCallback, useEffect, useRef, useState } from 'react';
import { Breadcrumb } from '@/components/layout/breadcrumb';
import { PageHeader } from '@/components/layout/page-header';
import { CodeResult } from '@/components/search/code-result';
import { DocResult } from '@/components/search/doc-result';
import { SearchResultCard } from '@/components/search/search-result';
import { Button } from '@/components/ui/button';
import { SearchInput } from '@/components/ui/input';
import { LoadingState } from '@/components/ui/spinner';
import { FilterChip } from '@/components/ui/toggle';
import { EmptyState, ErrorState } from '@/components/ui/tooltip';
import type { SearchResponse, SearchResult, StatsResponse } from '@/lib/api';
import { TASK_STATUS_CONFIG, TASK_STATUSES } from '@/lib/constants';
import { useCodeExamples, useRAGHybridSearch, useSearch, useSources, useStats } from '@/lib/hooks';

// Search modes
type SearchMode = 'knowledge' | 'docs' | 'code';

const SEARCH_MODES: { id: SearchMode; label: string; icon: string; description: string }[] = [
  { id: 'knowledge', label: 'Knowledge', icon: '◇', description: 'Patterns, rules, tasks' },
  { id: 'docs', label: 'Docs', icon: '▤', description: 'Crawled documentation' },
  { id: 'code', label: 'Code', icon: '⟨⟩', description: 'Code examples' },
];

// Curated searchable entity types for knowledge mode
const SEARCHABLE_TYPES = [
  'pattern',
  'rule',
  'template',
  'task',
  'episode',
  'topic',
  'document',
] as const;

// Common programming languages for code filter
const CODE_LANGUAGES = [
  'python',
  'typescript',
  'javascript',
  'rust',
  'go',
  'java',
  'ruby',
  'bash',
  'sql',
] as const;

interface SearchContentProps {
  initialQuery: string;
  initialResults?: SearchResponse;
  initialStats?: StatsResponse;
}

export function SearchContent({ initialQuery, initialResults, initialStats }: SearchContentProps) {
  const router = useRouter();
  const searchParams = useSearchParams();
  const urlQuery = searchParams.get('q') || '';
  const urlMode = (searchParams.get('mode') as SearchMode) || 'knowledge';

  const [mode, setMode] = useState<SearchMode>(urlMode);
  const [query, setQuery] = useState(initialQuery || urlQuery);
  const [submittedQuery, setSubmittedQuery] = useState(initialQuery || urlQuery);
  const inputRef = useRef<HTMLInputElement>(null);

  // Update URL when search params change
  const updateUrl = useCallback(
    (newParams: { q?: string; mode?: string; types?: string[]; status?: string }) => {
      const params = new URLSearchParams(searchParams.toString());

      if (newParams.q !== undefined) {
        if (newParams.q) params.set('q', newParams.q);
        else params.delete('q');
      }
      if (newParams.mode !== undefined) {
        if (newParams.mode !== 'knowledge') params.set('mode', newParams.mode);
        else params.delete('mode');
      }
      if (newParams.types !== undefined) {
        params.delete('types');
        for (const t of newParams.types) params.append('types', t);
      }
      if (newParams.status !== undefined) {
        if (newParams.status) params.set('status', newParams.status);
        else params.delete('status');
      }

      const newUrl = params.toString() ? `/search?${params.toString()}` : '/search';
      router.replace(newUrl, { scroll: false });
    },
    [router, searchParams]
  );

  // Knowledge mode filters
  const [selectedTypes, setSelectedTypes] = useState<string[]>([]);
  const [selectedStatus, setSelectedStatus] = useState<string | null>(null);
  const [sinceDate, setSinceDate] = useState<string>('');

  // Docs/Code mode filters
  const [selectedSource, setSelectedSource] = useState<string>('');
  const [selectedLanguage, setSelectedLanguage] = useState<string>('');
  const [returnMode, setReturnMode] = useState<'chunks' | 'pages'>('chunks');

  const { data: stats } = useStats(initialStats);
  const { data: sourcesData } = useSources();

  // Check if task type is selected to show status filter
  const showStatusFilter = selectedTypes.includes('task');

  // Knowledge search
  const {
    data: knowledgeResults,
    isLoading: knowledgeLoading,
    error: knowledgeError,
  } = useSearch(
    {
      query: submittedQuery,
      types: selectedTypes.length > 0 ? selectedTypes : undefined,
      status: selectedStatus || undefined,
      since: sinceDate || undefined,
      limit: 50,
      include_documents: false, // Knowledge mode searches only the graph
    },
    {
      enabled: mode === 'knowledge' && submittedQuery.length > 0,
      initialData:
        submittedQuery === initialQuery && mode === 'knowledge' ? initialResults : undefined,
    }
  );

  // Documentation search (hybrid for better results)
  const {
    data: docsResults,
    isLoading: docsLoading,
    error: docsError,
  } = useRAGHybridSearch(
    {
      query: submittedQuery,
      source_id: selectedSource || undefined,
      match_count: 20,
      return_mode: returnMode,
      include_context: true,
    },
    {
      enabled: mode === 'docs' && submittedQuery.length > 0,
    }
  );

  // Code examples search
  const {
    data: codeResults,
    isLoading: codeLoading,
    error: codeError,
  } = useCodeExamples(
    {
      query: submittedQuery,
      source_id: selectedSource || undefined,
      language: selectedLanguage || undefined,
      match_count: 20,
    },
    {
      enabled: mode === 'code' && submittedQuery.length > 0,
    }
  );

  // Get current mode's state
  const isLoading =
    mode === 'knowledge' ? knowledgeLoading : mode === 'docs' ? docsLoading : codeLoading;
  const error = mode === 'knowledge' ? knowledgeError : mode === 'docs' ? docsError : codeError;

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    setSubmittedQuery(query);
    updateUrl({ q: query });
  };

  const handleModeChange = (newMode: SearchMode) => {
    setMode(newMode);
    updateUrl({ mode: newMode });
  };

  const toggleType = (type: string) => {
    setSelectedTypes(prev => {
      const newTypes = prev.includes(type) ? prev.filter(t => t !== type) : [...prev, type];
      if (type === 'task' && prev.includes('task')) {
        setSelectedStatus(null);
      }
      return newTypes;
    });
  };

  const toggleStatus = (status: string) => {
    setSelectedStatus(prev => (prev === status ? null : status));
  };

  // Knowledge results are now filtered server-side
  const filteredKnowledgeResults = knowledgeResults?.results;

  // Get type counts from stats
  const getTypeCount = (type: string) => stats?.entity_counts[type] ?? 0;

  // Get sources list for dropdown
  const sources = sourcesData?.entities || [];

  return (
    <div className="space-y-4 animate-fade-in">
      <Breadcrumb />

      <PageHeader
        description="Find knowledge, documentation, and code"
        meta={
          submittedQuery
            ? mode === 'knowledge'
              ? `${filteredKnowledgeResults?.length ?? 0} results`
              : mode === 'docs'
                ? `${docsResults?.total ?? 0} results`
                : `${codeResults?.total ?? 0} results`
            : undefined
        }
      />

      {/* Mode Tabs */}
      <div className="flex gap-1 p-1 bg-sc-bg-base border border-sc-fg-subtle/20 rounded-lg w-fit">
        {SEARCH_MODES.map(m => (
          <button
            key={m.id}
            type="button"
            onClick={() => handleModeChange(m.id)}
            className={`px-4 py-2 text-sm font-medium rounded-md transition-colors ${
              mode === m.id
                ? 'bg-sc-purple text-white'
                : 'text-sc-fg-muted hover:text-sc-fg-primary hover:bg-sc-bg-elevated'
            }`}
          >
            <span className="mr-1.5">{m.icon}</span>
            <span className="hidden sm:inline">{m.label}</span>
            <span className="sm:hidden">{m.label.slice(0, 4)}</span>
          </button>
        ))}
      </div>

      {/* Search Form */}
      <form onSubmit={handleSubmit} className="space-y-3 sm:space-y-4">
        <div className="flex flex-col xs:flex-row gap-2 sm:gap-3">
          <div className="flex-1">
            <SearchInput
              ref={inputRef}
              value={query}
              onChange={e => setQuery(e.target.value)}
              placeholder={
                mode === 'knowledge'
                  ? 'Search patterns, rules, templates...'
                  : mode === 'docs'
                    ? 'Search documentation...'
                    : 'Search code examples...'
              }
              onSubmit={() => setSubmittedQuery(query)}
            />
          </div>
          <Button type="submit" size="lg" disabled={!query.trim()} className="xs:w-auto">
            Search
          </Button>
        </div>

        {/* Mode-specific Filters */}
        <div className="bg-sc-bg-base border border-sc-fg-subtle/20 rounded-lg sm:rounded-xl p-3 sm:p-4 space-y-3 sm:space-y-4">
          {/* Knowledge Mode Filters */}
          {mode === 'knowledge' && (
            <>
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
                  {SEARCHABLE_TYPES.map(type => {
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

              {/* Task Status Filter */}
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
                    {TASK_STATUSES.map(status => {
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
                      sinceDate &&
                      new Date(sinceDate) >= new Date(Date.now() - 7 * 24 * 60 * 60 * 1000)
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
                      sinceDate &&
                      new Date(sinceDate) >= new Date(Date.now() - 30 * 24 * 60 * 60 * 1000) &&
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
                    onChange={e => setSinceDate(e.target.value)}
                    className="text-xs px-2 py-1 rounded border border-sc-fg-subtle/20 bg-sc-bg-elevated text-sc-fg-primary focus:outline-none focus:border-sc-purple/40"
                  />
                </div>
              </div>
            </>
          )}

          {/* Docs Mode Filters */}
          {mode === 'docs' && (
            <div className="flex flex-wrap gap-4">
              {/* Source Filter */}
              <div className="space-y-2 flex-1 min-w-[200px]">
                <span className="text-sc-fg-muted text-sm font-medium block">Source</span>
                <select
                  value={selectedSource}
                  onChange={e => setSelectedSource(e.target.value)}
                  className="w-full px-3 py-2 text-sm rounded-lg border border-sc-fg-subtle/20 bg-sc-bg-elevated text-sc-fg-primary focus:outline-none focus:border-sc-purple/40"
                >
                  <option value="">All sources</option>
                  {sources.map(source => (
                    <option key={source.id} value={source.id}>
                      {source.name}
                    </option>
                  ))}
                </select>
              </div>

              {/* Return Mode */}
              <div className="space-y-2">
                <span className="text-sc-fg-muted text-sm font-medium block">Results as</span>
                <div className="flex gap-1 p-1 bg-sc-bg-elevated rounded-lg">
                  <button
                    type="button"
                    onClick={() => setReturnMode('chunks')}
                    className={`px-3 py-1.5 text-xs font-medium rounded transition-colors ${
                      returnMode === 'chunks'
                        ? 'bg-sc-cyan text-sc-bg-dark'
                        : 'text-sc-fg-muted hover:text-sc-fg-primary'
                    }`}
                  >
                    Chunks
                  </button>
                  <button
                    type="button"
                    onClick={() => setReturnMode('pages')}
                    className={`px-3 py-1.5 text-xs font-medium rounded transition-colors ${
                      returnMode === 'pages'
                        ? 'bg-sc-cyan text-sc-bg-dark'
                        : 'text-sc-fg-muted hover:text-sc-fg-primary'
                    }`}
                  >
                    Pages
                  </button>
                </div>
              </div>
            </div>
          )}

          {/* Code Mode Filters */}
          {mode === 'code' && (
            <div className="flex flex-wrap gap-4">
              {/* Source Filter */}
              <div className="space-y-2 flex-1 min-w-[200px]">
                <span className="text-sc-fg-muted text-sm font-medium block">Source</span>
                <select
                  value={selectedSource}
                  onChange={e => setSelectedSource(e.target.value)}
                  className="w-full px-3 py-2 text-sm rounded-lg border border-sc-fg-subtle/20 bg-sc-bg-elevated text-sc-fg-primary focus:outline-none focus:border-sc-purple/40"
                >
                  <option value="">All sources</option>
                  {sources.map(source => (
                    <option key={source.id} value={source.id}>
                      {source.name}
                    </option>
                  ))}
                </select>
              </div>

              {/* Language Filter */}
              <div className="space-y-2">
                <span className="text-sc-fg-muted text-sm font-medium block">Language</span>
                <div className="flex flex-wrap gap-1.5">
                  <button
                    type="button"
                    onClick={() => setSelectedLanguage('')}
                    className={`px-2 py-1 text-xs rounded border transition-colors ${
                      !selectedLanguage
                        ? 'bg-sc-purple/20 border-sc-purple/40 text-sc-purple'
                        : 'border-sc-fg-subtle/20 text-sc-fg-muted hover:border-sc-fg-subtle/40'
                    }`}
                  >
                    All
                  </button>
                  {CODE_LANGUAGES.map(lang => (
                    <button
                      key={lang}
                      type="button"
                      onClick={() => setSelectedLanguage(lang)}
                      className={`px-2 py-1 text-xs rounded border transition-colors ${
                        selectedLanguage === lang
                          ? 'bg-sc-purple/20 border-sc-purple/40 text-sc-purple'
                          : 'border-sc-fg-subtle/20 text-sc-fg-muted hover:border-sc-fg-subtle/40'
                      }`}
                    >
                      {lang}
                    </button>
                  ))}
                </div>
              </div>
            </div>
          )}
        </div>
      </form>

      {/* Results */}
      {submittedQuery ? (
        <div className="space-y-3 sm:space-y-4">
          {isLoading ? (
            <LoadingState message="Searching..." />
          ) : error ? (
            <ErrorState title="Search failed" message={error.message} />
          ) : mode === 'knowledge' ? (
            // Knowledge Results
            filteredKnowledgeResults && filteredKnowledgeResults.length > 0 ? (
              <>
                <div className="text-sc-fg-muted text-xs sm:text-sm">
                  <span className="font-medium">{filteredKnowledgeResults.length}</span> results
                  <span className="hidden xs:inline"> for "{submittedQuery}"</span>
                  {selectedTypes.length > 0 && (
                    <span className="text-sc-fg-subtle hidden sm:inline">
                      {' '}
                      in {selectedTypes.join(', ')}
                    </span>
                  )}
                </div>
                <div className="space-y-2 sm:space-y-3">
                  {filteredKnowledgeResults.map((result: SearchResult) => (
                    <SearchResultCard key={result.id} result={result} />
                  ))}
                </div>
              </>
            ) : (
              <EmptyState
                icon=":"
                title="No results found"
                description="Try different keywords or remove some filters"
              />
            )
          ) : mode === 'docs' ? (
            // Docs Results
            docsResults && docsResults.results.length > 0 ? (
              <>
                <div className="text-sc-fg-muted text-xs sm:text-sm">
                  <span className="font-medium">{docsResults.total}</span> results
                  <span className="hidden xs:inline"> for "{submittedQuery}"</span>
                  {docsResults.source_filter && (
                    <span className="text-sc-fg-subtle hidden sm:inline">
                      {' '}
                      in {docsResults.source_filter}
                    </span>
                  )}
                </div>
                <div className="space-y-3">
                  {docsResults.results.map(result => (
                    <DocResult
                      key={'chunk_id' in result ? result.chunk_id : result.document_id}
                      result={result}
                    />
                  ))}
                </div>
              </>
            ) : (
              <EmptyState
                icon=":"
                title="No documentation found"
                description="Try different keywords or check if sources have been crawled"
              />
            )
          ) : // Code Results
          codeResults && codeResults.examples.length > 0 ? (
            <>
              <div className="text-sc-fg-muted text-xs sm:text-sm">
                <span className="font-medium">{codeResults.total}</span> code examples
                <span className="hidden xs:inline"> for "{submittedQuery}"</span>
                {codeResults.language_filter && (
                  <span className="text-sc-fg-subtle hidden sm:inline">
                    {' '}
                    in {codeResults.language_filter}
                  </span>
                )}
              </div>
              <div className="space-y-3">
                {codeResults.examples.map(result => (
                  <CodeResult key={result.chunk_id} result={result} />
                ))}
              </div>
            </>
          ) : (
            <EmptyState
              icon=":"
              title="No code examples found"
              description="Try different keywords or check if sources contain code"
            />
          )}
        </div>
      ) : (
        <EmptyState
          icon=":"
          title={
            mode === 'knowledge'
              ? 'Search the knowledge graph'
              : mode === 'docs'
                ? 'Search crawled documentation'
                : 'Search code examples'
          }
          description={
            mode === 'knowledge'
              ? 'Find patterns, rules, templates, and tasks'
              : mode === 'docs'
                ? 'Semantic search with hybrid vector + full-text matching'
                : 'Find relevant code snippets from documentation'
          }
        />
      )}
    </div>
  );
}
