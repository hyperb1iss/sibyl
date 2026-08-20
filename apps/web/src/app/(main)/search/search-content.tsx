'use client';

import { useRouter, useSearchParams } from 'next/navigation';
import { useCallback, useEffect, useRef, useState } from 'react';
import { PageHeader } from '@/components/layout/page-header';
import { CodeResult } from '@/components/search/code-result';
import { DocResult } from '@/components/search/doc-result';
import { SearchResultCard } from '@/components/search/search-result';
import { Button } from '@/components/ui/button';
import { EnhancedEmptyState, ErrorState, SearchEmptyState } from '@/components/ui/empty-state';
import { Code, FileText } from '@/components/ui/icons';
import { SearchInput } from '@/components/ui/input';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { LoadingState } from '@/components/ui/spinner';
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { FilterChip } from '@/components/ui/toggle';
import type { MemoryScope, SearchResponse, SearchResult, StatsResponse } from '@/lib/api';
import { TASK_STATUS_CONFIG, TASK_STATUSES } from '@/lib/constants/tasks';
import { useCodeExamples, useRAGHybridSearch, useSearch, useSources, useStats } from '@/lib/hooks';

// Radix Select forbids empty-string item values, so the "All sources" option
// uses this sentinel and maps back to the empty filter in state.
const ALL_SOURCES = '__all__';

// Search modes
type SearchMode = 'all' | 'memory' | 'knowledge' | 'docs' | 'code';

const SEARCH_MODES: { id: SearchMode; label: string; icon: string; description: string }[] = [
  { id: 'all', label: 'All', icon: '✦', description: 'Knowledge, memory, docs' },
  { id: 'memory', label: 'Memory', icon: '◌', description: 'Raw captures and imports' },
  {
    id: 'knowledge',
    label: 'Knowledge',
    icon: '◇',
    description: 'Patterns, procedures, rules, tasks',
  },
  { id: 'docs', label: 'Docs', icon: '▤', description: 'Crawled documentation' },
  { id: 'code', label: 'Code', icon: '⟨⟩', description: 'Code examples' },
];

const MEMORY_SCOPES: Array<{ value: MemoryScope; label: string }> = [
  { value: 'private', label: 'Private' },
  { value: 'delegated', label: 'Delegated' },
  { value: 'project', label: 'Project' },
  { value: 'team', label: 'Team' },
  { value: 'organization', label: 'Organization' },
  { value: 'shared', label: 'Shared' },
  { value: 'public', label: 'Public' },
];

// Curated searchable entity types for knowledge mode
const SEARCHABLE_TYPES = [
  'pattern',
  'procedure',
  'rule',
  'template',
  'task',
  'episode',
  'topic',
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

const FILTER_INPUT_CLASS =
  'w-full rounded-lg border border-sc-fg-subtle/20 bg-sc-bg-highlight px-2.5 py-2 text-sm text-sc-fg-primary transition-colors duration-200 placeholder:text-sc-fg-subtle focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan focus-visible:ring-offset-2 focus-visible:ring-offset-sc-bg-base';

function parseSearchMode(value: string | null): SearchMode {
  return SEARCH_MODES.some(mode => mode.id === value) ? (value as SearchMode) : 'all';
}

function parseDelimited(value: string): string[] {
  return value
    .split(',')
    .map(part => part.trim())
    .filter(Boolean);
}

interface SearchContentProps {
  initialQuery: string;
  initialResults?: SearchResponse;
  initialStats?: StatsResponse;
}

export function SearchContent({ initialQuery, initialResults, initialStats }: SearchContentProps) {
  const router = useRouter();
  const searchParams = useSearchParams();
  const urlQuery = searchParams.get('q') || '';
  const urlMode = parseSearchMode(searchParams.get('mode'));

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
        if (newParams.mode !== 'all') params.set('mode', newParams.mode);
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

  // Raw memory facets
  const [memorySourceId, setMemorySourceId] = useState<string>('');
  const [memoryScope, setMemoryScope] = useState<MemoryScope>('private');
  const [scopeKey, setScopeKey] = useState<string>('');
  const [participantsInput, setParticipantsInput] = useState<string>('');
  const [labelsInput, setLabelsInput] = useState<string>('');
  const [threadId, setThreadId] = useState<string>('');
  const [occurredAfter, setOccurredAfter] = useState<string>('');
  const [occurredBefore, setOccurredBefore] = useState<string>('');

  const { data: stats } = useStats(initialStats);
  const { data: sourcesData } = useSources();

  // Check if task type is selected to show status filter
  const showStatusFilter = selectedTypes.includes('task');
  const participants = parseDelimited(participantsInput);
  const labels = parseDelimited(labelsInput);
  const rawFacetParams = {
    source_id: memorySourceId.trim() || undefined,
    memory_scope: memoryScope,
    scope_key: scopeKey.trim() || undefined,
    participants: participants.length > 0 ? participants : undefined,
    labels: labels.length > 0 ? labels : undefined,
    thread_id: threadId.trim() || undefined,
    occurred_after: occurredAfter ? `${occurredAfter}T00:00:00Z` : undefined,
    occurred_before: occurredBefore ? `${occurredBefore}T23:59:59Z` : undefined,
  };
  const hasMemoryFacets = Boolean(
    memorySourceId.trim() ||
      memoryScope !== 'private' ||
      scopeKey.trim() ||
      participantsInput.trim() ||
      labelsInput.trim() ||
      threadId.trim() ||
      occurredAfter ||
      occurredBefore
  );

  const clearMemoryFacets = () => {
    setMemorySourceId('');
    setMemoryScope('private');
    setScopeKey('');
    setParticipantsInput('');
    setLabelsInput('');
    setThreadId('');
    setOccurredAfter('');
    setOccurredBefore('');
  };

  // Unified search
  const {
    data: allResults,
    isLoading: allLoading,
    error: allError,
  } = useSearch(
    {
      query: submittedQuery,
      limit: 50,
      include_documents: true,
      include_graph: true,
      include_raw_memory: true,
      ...rawFacetParams,
    },
    {
      enabled: mode === 'all' && submittedQuery.length > 0,
      initialData: submittedQuery === initialQuery && mode === 'all' ? initialResults : undefined,
    }
  );

  // Raw memory search
  const {
    data: memoryResults,
    isLoading: memoryLoading,
    error: memoryError,
  } = useSearch(
    {
      query: submittedQuery,
      types: ['raw_memory'],
      limit: 50,
      include_documents: false,
      include_graph: false,
      include_raw_memory: true,
      ...rawFacetParams,
    },
    {
      enabled: mode === 'memory' && submittedQuery.length > 0,
    }
  );

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
      include_graph: true,
      include_raw_memory: false,
    },
    {
      enabled: mode === 'knowledge' && submittedQuery.length > 0,
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
  const isUnifiedMode = mode === 'all' || mode === 'memory' || mode === 'knowledge';
  const unifiedResults =
    mode === 'all' ? allResults : mode === 'memory' ? memoryResults : knowledgeResults;
  const unifiedResultList = unifiedResults?.results;
  const isLoading = isUnifiedMode
    ? mode === 'all'
      ? allLoading
      : mode === 'memory'
        ? memoryLoading
        : knowledgeLoading
    : mode === 'docs'
      ? docsLoading
      : codeLoading;
  const error = isUnifiedMode
    ? mode === 'all'
      ? allError
      : mode === 'memory'
        ? memoryError
        : knowledgeError
    : mode === 'docs'
      ? docsError
      : codeError;

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

  // Bridge the shared Select (no empty values) to the source filter state
  const sourceValue = selectedSource || ALL_SOURCES;
  const handleSourceChange = (value: string) => {
    setSelectedSource(value === ALL_SOURCES ? '' : value);
  };

  // Get type counts from stats
  const getTypeCount = (type: string) => stats?.entity_counts[type] ?? 0;

  // Get sources list for dropdown
  const sources = sourcesData?.entities || [];
  const unifiedCount = unifiedResults?.total ?? unifiedResultList?.length ?? 0;
  const allBreakdown =
    mode === 'all' && unifiedResults
      ? [
          `${unifiedResults.graph_count ?? 0} knowledge`,
          `${unifiedResults.document_count ?? 0} docs`,
          `${unifiedResults.raw_memory_count ?? 0} memory`,
        ].join(' / ')
      : null;
  const pageMeta = submittedQuery
    ? isUnifiedMode
      ? allBreakdown || `${unifiedCount} results`
      : mode === 'docs'
        ? `${docsResults?.total ?? 0} results`
        : `${codeResults?.total ?? 0} results`
    : undefined;

  return (
    <div className="space-y-4 animate-fade-in">
      <PageHeader description="Find memory, knowledge, documentation, and code" meta={pageMeta} />

      {/* Mode Tabs */}
      <Tabs value={mode} onValueChange={v => handleModeChange(v as SearchMode)} variant="pills">
        <TabsList>
          {SEARCH_MODES.map(m => (
            <TabsTrigger key={m.id} value={m.id}>
              <span className="mr-1.5">{m.icon}</span>
              <span className="hidden sm:inline">{m.label}</span>
              <span className="sm:hidden">{m.label.slice(0, 4)}</span>
            </TabsTrigger>
          ))}
        </TabsList>
      </Tabs>

      {/* Search Form */}
      <form onSubmit={handleSubmit} className="space-y-3 sm:space-y-4">
        <div className="flex flex-col xs:flex-row gap-2 sm:gap-3">
          <div className="flex-1">
            <SearchInput
              ref={inputRef}
              value={query}
              onChange={e => setQuery(e.target.value)}
              aria-label="Search"
              placeholder={
                mode === 'all'
                  ? 'Search everything...'
                  : mode === 'memory'
                    ? 'Search raw memories...'
                    : mode === 'knowledge'
                      ? 'Search patterns, procedures, rules, templates...'
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
        <div className="bg-sc-bg-elevated border border-sc-fg-subtle/30 rounded-xl p-3 sm:p-4 space-y-3 sm:space-y-4 shadow-card">
          {/* All/Memory Mode Filters */}
          {(mode === 'all' || mode === 'memory') && (
            <div className="space-y-3">
              <div className="flex items-center gap-2">
                <span className="text-sc-fg-muted text-sm font-medium">Memory Facets</span>
                {hasMemoryFacets && (
                  <button
                    type="button"
                    onClick={clearMemoryFacets}
                    className="text-xs text-sc-purple hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan focus-visible:ring-offset-2 focus-visible:ring-offset-sc-bg-base rounded"
                  >
                    Clear
                  </button>
                )}
              </div>

              <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
                <label className="space-y-1.5">
                  <span className="text-xs font-medium text-sc-fg-muted">Source ID</span>
                  <input
                    type="text"
                    value={memorySourceId}
                    onChange={e => setMemorySourceId(e.target.value)}
                    className={FILTER_INPUT_CLASS}
                    placeholder="source-mail-1"
                  />
                </label>
                <label className="space-y-1.5">
                  <span className="text-xs font-medium text-sc-fg-muted">People</span>
                  <input
                    type="text"
                    value={participantsInput}
                    onChange={e => setParticipantsInput(e.target.value)}
                    className={FILTER_INPUT_CLASS}
                    placeholder="bliss@example.com, nova"
                  />
                </label>
                <label className="space-y-1.5">
                  <span className="text-xs font-medium text-sc-fg-muted">Labels</span>
                  <input
                    type="text"
                    value={labelsInput}
                    onChange={e => setLabelsInput(e.target.value)}
                    className={FILTER_INPUT_CLASS}
                    placeholder="email, launch"
                  />
                </label>
                <label className="space-y-1.5">
                  <span className="text-xs font-medium text-sc-fg-muted">Thread</span>
                  <input
                    type="text"
                    value={threadId}
                    onChange={e => setThreadId(e.target.value)}
                    className={FILTER_INPUT_CLASS}
                    placeholder="thread-1"
                  />
                </label>
              </div>

              <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
                <div className="space-y-1.5">
                  <span id="memory-scope-label" className="text-xs font-medium text-sc-fg-muted">
                    Scope
                  </span>
                  <Select
                    value={memoryScope}
                    onValueChange={value => setMemoryScope(value as MemoryScope)}
                  >
                    <SelectTrigger aria-labelledby="memory-scope-label">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {MEMORY_SCOPES.map(scope => (
                        <SelectItem key={scope.value} value={scope.value}>
                          {scope.label}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
                <label className="space-y-1.5">
                  <span className="text-xs font-medium text-sc-fg-muted">Scope Key</span>
                  <input
                    type="text"
                    value={scopeKey}
                    onChange={e => setScopeKey(e.target.value)}
                    className={FILTER_INPUT_CLASS}
                    placeholder="project_123"
                  />
                </label>
                <label className="space-y-1.5">
                  <span className="text-xs font-medium text-sc-fg-muted">Occurred After</span>
                  <input
                    type="date"
                    value={occurredAfter}
                    onChange={e => setOccurredAfter(e.target.value)}
                    className={FILTER_INPUT_CLASS}
                  />
                </label>
                <label className="space-y-1.5">
                  <span className="text-xs font-medium text-sc-fg-muted">Occurred Before</span>
                  <input
                    type="date"
                    value={occurredBefore}
                    onChange={e => setOccurredBefore(e.target.value)}
                    className={FILTER_INPUT_CLASS}
                  />
                </label>
              </div>
            </div>
          )}

          {/* Knowledge Mode Filters */}
          {mode === 'knowledge' && (
            <>
              {/* Entity Type Filters */}
              <div className="space-y-2">
                <div className="flex items-center gap-2">
                  <span className="text-sc-fg-muted text-sm font-medium">Entity Type</span>
                  {mode === 'knowledge' && selectedTypes.length > 0 && (
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
                  <span id="since-date-label" className="text-sc-fg-muted text-sm font-medium">
                    Created Since
                  </span>
                  {sinceDate && (
                    <button
                      type="button"
                      onClick={() => setSinceDate('')}
                      className="text-xs text-sc-purple hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan focus-visible:ring-offset-2 focus-visible:ring-offset-sc-bg-base rounded"
                    >
                      Clear
                    </button>
                  )}
                </div>
                <div
                  className="flex flex-wrap gap-2"
                  role="group"
                  aria-labelledby="since-date-label"
                >
                  <button
                    type="button"
                    onClick={() => {
                      const d = new Date();
                      d.setDate(d.getDate() - 7);
                      setSinceDate(d.toISOString().split('T')[0]);
                    }}
                    aria-pressed={
                      !!sinceDate &&
                      new Date(sinceDate) >= new Date(Date.now() - 7 * 24 * 60 * 60 * 1000)
                    }
                    className={`text-xs px-2 py-1 rounded border transition-colors duration-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan focus-visible:ring-offset-2 focus-visible:ring-offset-sc-bg-base ${
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
                    aria-pressed={
                      !!sinceDate &&
                      new Date(sinceDate) >= new Date(Date.now() - 30 * 24 * 60 * 60 * 1000) &&
                      new Date(sinceDate) < new Date(Date.now() - 7 * 24 * 60 * 60 * 1000)
                    }
                    className={`text-xs px-2 py-1 rounded border transition-colors duration-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan focus-visible:ring-offset-2 focus-visible:ring-offset-sc-bg-base ${
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
                    aria-label="Created since date"
                    className="text-xs px-2 py-1 rounded-lg border border-sc-fg-subtle/20 bg-sc-bg-highlight text-sc-fg-primary transition-colors duration-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan focus-visible:ring-offset-2 focus-visible:ring-offset-sc-bg-base"
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
                <span id="docs-source-label" className="text-sc-fg-muted text-sm font-medium block">
                  Source
                </span>
                <Select value={sourceValue} onValueChange={handleSourceChange}>
                  <SelectTrigger aria-labelledby="docs-source-label">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value={ALL_SOURCES}>All sources</SelectItem>
                    {sources.map(source => (
                      <SelectItem key={source.id} value={source.id}>
                        {source.name}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>

              {/* Return Mode */}
              <div className="space-y-2">
                <span id="return-mode-label" className="text-sc-fg-muted text-sm font-medium block">
                  Results as
                </span>
                <div
                  className="flex gap-1 p-1 bg-sc-bg-elevated rounded-lg"
                  role="group"
                  aria-labelledby="return-mode-label"
                >
                  <button
                    type="button"
                    onClick={() => setReturnMode('chunks')}
                    aria-pressed={returnMode === 'chunks'}
                    className={`px-3 py-1.5 text-xs font-medium rounded transition-colors duration-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan focus-visible:ring-offset-2 focus-visible:ring-offset-sc-bg-base ${
                      returnMode === 'chunks'
                        ? 'bg-sc-cyan/15 text-sc-cyan'
                        : 'text-sc-fg-muted hover:text-sc-fg-primary'
                    }`}
                  >
                    Chunks
                  </button>
                  <button
                    type="button"
                    onClick={() => setReturnMode('pages')}
                    aria-pressed={returnMode === 'pages'}
                    className={`px-3 py-1.5 text-xs font-medium rounded transition-colors duration-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan focus-visible:ring-offset-2 focus-visible:ring-offset-sc-bg-base ${
                      returnMode === 'pages'
                        ? 'bg-sc-cyan/15 text-sc-cyan'
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
                <span id="code-source-label" className="text-sc-fg-muted text-sm font-medium block">
                  Source
                </span>
                <Select value={sourceValue} onValueChange={handleSourceChange}>
                  <SelectTrigger aria-labelledby="code-source-label">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value={ALL_SOURCES}>All sources</SelectItem>
                    {sources.map(source => (
                      <SelectItem key={source.id} value={source.id}>
                        {source.name}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>

              {/* Language Filter */}
              <div className="space-y-2">
                <span id="language-label" className="text-sc-fg-muted text-sm font-medium block">
                  Language
                </span>
                <div
                  className="flex flex-wrap gap-1.5"
                  role="group"
                  aria-labelledby="language-label"
                >
                  <button
                    type="button"
                    onClick={() => setSelectedLanguage('')}
                    aria-pressed={!selectedLanguage}
                    className={`px-2 py-1 text-xs rounded border transition-colors duration-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan focus-visible:ring-offset-2 focus-visible:ring-offset-sc-bg-base ${
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
                      aria-pressed={selectedLanguage === lang}
                      className={`px-2 py-1 text-xs rounded border transition-colors duration-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan focus-visible:ring-offset-2 focus-visible:ring-offset-sc-bg-base ${
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
          ) : isUnifiedMode ? (
            unifiedResultList && unifiedResultList.length > 0 ? (
              <>
                <div className="text-sc-fg-muted text-xs sm:text-sm">
                  <span className="font-medium">{unifiedCount}</span> results
                  <span className="hidden xs:inline"> for "{submittedQuery}"</span>
                  {allBreakdown && (
                    <span className="text-sc-fg-subtle hidden sm:inline"> · {allBreakdown}</span>
                  )}
                  {selectedTypes.length > 0 && (
                    <span className="text-sc-fg-subtle hidden sm:inline">
                      {' '}
                      in {selectedTypes.join(', ')}
                    </span>
                  )}
                </div>
                <div className="space-y-2 sm:space-y-3">
                  {unifiedResultList.map((result: SearchResult) => (
                    <SearchResultCard key={result.id} result={result} />
                  ))}
                </div>
              </>
            ) : (
              <SearchEmptyState
                query={submittedQuery}
                onClear={() => {
                  setQuery('');
                  setSubmittedQuery('');
                }}
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
              <EnhancedEmptyState
                icon={<FileText width={40} height={40} className="text-sc-yellow" />}
                title="No documentation found"
                description="Try different keywords or check if sources have been crawled"
                variant="filtered"
                actions={[
                  {
                    label: 'Clear search',
                    onClick: () => {
                      setQuery('');
                      setSubmittedQuery('');
                    },
                  },
                  { label: 'Browse Sources', href: '/sources', variant: 'secondary' },
                ]}
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
            <EnhancedEmptyState
              icon={<Code width={40} height={40} className="text-sc-yellow" />}
              title="No code examples found"
              description="Try different keywords or check if sources contain code"
              variant="filtered"
              actions={[
                {
                  label: 'Clear search',
                  onClick: () => {
                    setQuery('');
                    setSubmittedQuery('');
                  },
                },
                { label: 'Browse Sources', href: '/sources', variant: 'secondary' },
              ]}
            />
          )}
        </div>
      ) : (
        <SearchEmptyState />
      )}
    </div>
  );
}
