'use client';

import { AnimatePresence, motion } from 'motion/react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { toast } from 'sonner';
import { Breadcrumb } from '@/components/layout/breadcrumb';
import { PageHeader } from '@/components/layout/page-header';
import {
  AddSourceDialog,
  type LocalSourceData,
  SourceCardEnhanced,
  SourceCardSkeleton,
  type UrlSourceData,
} from '@/components/sources';
import { EnhancedEmptyState } from '@/components/ui/empty-state';
import {
  Database,
  Filter,
  Folder,
  Globe,
  Grid3X3,
  LayoutList,
  RefreshCw,
  Search,
} from '@/components/ui/icons';
import type { CrawlStatusType, SourceTypeValue } from '@/lib/constants';
import {
  useAllCrawlProgress,
  useCancelCrawl,
  useCrawlSource,
  useCreateSource,
  useDeleteSource,
  useSources,
  useSyncSource,
} from '@/lib/hooks';
import { wsClient } from '@/lib/websocket';

type ViewMode = 'grid' | 'list';
type SortBy = 'name' | 'updated' | 'documents';
type FilterStatus = 'all' | CrawlStatusType;
type FilterType = 'all' | SourceTypeValue;

export default function SourcesPage() {
  const { data: sourcesData, isLoading, error, refetch } = useSources();
  const createSource = useCreateSource();
  const deleteSource = useDeleteSource();
  const crawlSource = useCrawlSource();
  const syncSource = useSyncSource();
  const cancelCrawl = useCancelCrawl();
  const crawlProgressMap = useAllCrawlProgress();

  // UI State
  const [showAddDialog, setShowAddDialog] = useState(false);
  const [viewMode, setViewMode] = useState<ViewMode>('grid');
  const [searchQuery, setSearchQuery] = useState('');
  const [filterStatus, setFilterStatus] = useState<FilterStatus>('all');
  const [filterType, setFilterType] = useState<FilterType>('all');
  const [sortBy, setSortBy] = useState<SortBy>('updated');
  const [showFilters, setShowFilters] = useState(false);

  // Track which sources have active crawl operations
  const [crawlingSourceIds, setCrawlingSourceIds] = useState<Set<string>>(new Set());

  // Sync crawling state with WebSocket events
  useEffect(() => {
    const unsubComplete = wsClient.on('crawl_complete', data => {
      const sourceId = data.source_id as string;
      if (sourceId) {
        setCrawlingSourceIds(prev => {
          const next = new Set(prev);
          next.delete(sourceId);
          return next;
        });
      }
    });

    const unsubStarted = wsClient.on('crawl_started', data => {
      const sourceId = data.source_id as string;
      if (sourceId) {
        setCrawlingSourceIds(prev => new Set(prev).add(sourceId));
      }
    });

    return () => {
      unsubComplete();
      unsubStarted();
    };
  }, []);

  const sources = sourcesData?.entities ?? [];

  // Filter and sort sources
  const filteredSources = useMemo(() => {
    let result = [...sources];

    // Search filter
    if (searchQuery) {
      const query = searchQuery.toLowerCase();
      result = result.filter(
        s =>
          s.name.toLowerCase().includes(query) ||
          s.description?.toLowerCase().includes(query) ||
          (s.metadata.url as string)?.toLowerCase().includes(query)
      );
    }

    // Status filter
    if (filterStatus !== 'all') {
      result = result.filter(s => s.metadata.crawl_status === filterStatus);
    }

    // Type filter
    if (filterType !== 'all') {
      result = result.filter(s => s.metadata.source_type === filterType);
    }

    // Sort
    result.sort((a, b) => {
      if (sortBy === 'name') {
        return a.name.localeCompare(b.name);
      }
      if (sortBy === 'documents') {
        return (
          ((b.metadata.document_count as number) || 0) -
          ((a.metadata.document_count as number) || 0)
        );
      }
      // Default: sort by updated/last_crawled
      const dateA = a.metadata.last_crawled || a.updated_at || '';
      const dateB = b.metadata.last_crawled || b.updated_at || '';
      return dateB.localeCompare(dateA);
    });

    return result;
  }, [sources, searchQuery, filterStatus, filterType, sortBy]);

  // Stats
  const stats = useMemo(() => {
    const completed = sources.filter(s => s.metadata.crawl_status === 'completed').length;
    const pending = sources.filter(s => s.metadata.crawl_status === 'pending').length;
    const inProgress = sources.filter(s => s.metadata.crawl_status === 'in_progress').length;
    const failed = sources.filter(s => s.metadata.crawl_status === 'failed').length;
    const totalDocs = sources.reduce(
      (acc, s) => acc + ((s.metadata.document_count as number) || 0),
      0
    );

    return { completed, pending, inProgress, failed, totalDocs, total: sources.length };
  }, [sources]);

  // Handlers
  const handleAddSource = useCallback(
    async (data: UrlSourceData) => {
      try {
        const result = await createSource.mutateAsync({
          name: data.name,
          url: data.url,
          description: data.description,
          source_type: 'website' as SourceTypeValue,
          crawl_depth: data.crawlDepth,
          crawl_patterns: data.includePatterns,
          exclude_patterns: data.excludePatterns,
        });

        toast.success(`Source "${data.name}" created`);

        // Optionally auto-start crawl
        if (result?.id) {
          setCrawlingSourceIds(prev => new Set(prev).add(result.id));
          await crawlSource.mutateAsync(result.id);
        }
      } catch (err) {
        console.error('Failed to create source:', err);
        toast.error('Failed to create source');
        throw err;
      }
    },
    [createSource, crawlSource]
  );

  const handleAddLocalSource = useCallback(
    async (data: LocalSourceData) => {
      try {
        // Convert path to file:// URL
        const fileUrl = data.path.startsWith('file://') ? data.path : `file://${data.path}`;

        const result = await createSource.mutateAsync({
          name: data.name,
          url: fileUrl,
          description: data.description,
          source_type: 'local' as SourceTypeValue,
        });

        toast.success(`Local source "${data.name}" created`);

        // Auto-start indexing for local sources
        if (result?.id) {
          setCrawlingSourceIds(prev => new Set(prev).add(result.id));
          await crawlSource.mutateAsync(result.id);
          toast.info('Started indexing local files...');
        }
      } catch (err) {
        console.error('Failed to create local source:', err);
        toast.error('Failed to create local source');
        throw err;
      }
    },
    [createSource, crawlSource]
  );

  const handleCrawl = useCallback(
    async (id: string) => {
      const source = sources.find(s => s.id === id);
      if (!source) return;

      // Check if already crawling
      if (crawlingSourceIds.has(id) || source.metadata.crawl_status === 'in_progress') {
        toast.info('Crawl already in progress');
        return;
      }

      try {
        // Mark as crawling immediately
        setCrawlingSourceIds(prev => new Set(prev).add(id));

        const result = await crawlSource.mutateAsync(id);

        // Check if backend says already running
        if (result?.status === 'already_running') {
          toast.info('Crawl already in progress');
        } else {
          toast.success(`Started crawling "${source.name}"`);
        }
      } catch (err) {
        console.error('Failed to start crawl:', err);
        toast.error('Failed to start crawl');
        // Remove from crawling set on error
        setCrawlingSourceIds(prev => {
          const next = new Set(prev);
          next.delete(id);
          return next;
        });
      }
    },
    [sources, crawlSource, crawlingSourceIds]
  );

  const handleDelete = useCallback(
    async (id: string) => {
      const source = sources.find(s => s.id === id);
      try {
        await deleteSource.mutateAsync(id);
        toast.success(`Deleted "${source?.name || 'source'}"`);
      } catch (err) {
        console.error('Failed to delete source:', err);
        toast.error('Failed to delete source');
      }
    },
    [sources, deleteSource]
  );

  const handleSync = useCallback(
    async (id: string) => {
      const source = sources.find(s => s.id === id);
      try {
        await syncSource.mutateAsync(id);
        // Clear from crawling set if it was there
        setCrawlingSourceIds(prev => {
          const next = new Set(prev);
          next.delete(id);
          return next;
        });
        toast.success(`Synced "${source?.name || 'source'}"`);
      } catch (err) {
        console.error('Failed to sync source:', err);
        toast.error('Failed to sync source');
      }
    },
    [sources, syncSource]
  );

  const handleCancel = useCallback(
    async (id: string) => {
      const source = sources.find(s => s.id === id);
      try {
        await cancelCrawl.mutateAsync(id);
        // Clear from crawling set
        setCrawlingSourceIds(prev => {
          const next = new Set(prev);
          next.delete(id);
          return next;
        });
        toast.success(`Cancelled crawl for "${source?.name || 'source'}"`);
      } catch (err) {
        console.error('Failed to cancel crawl:', err);
        toast.error('Failed to cancel crawl');
      }
    },
    [sources, cancelCrawl]
  );

  if (error) {
    return (
      <div className="space-y-4 animate-fade-in">
        <Breadcrumb />
        <PageHeader description="Manage documentation sources for the knowledge graph" />
        <div className="bg-sc-red/10 border border-sc-red/30 rounded-xl p-6 text-center">
          <p className="text-sc-red font-medium mb-2">Failed to load sources</p>
          <p className="text-sm text-sc-fg-muted mb-4">
            {error instanceof Error ? error.message : 'Unknown error'}
          </p>
          <button
            type="button"
            onClick={() => refetch()}
            className="px-4 py-2 bg-sc-red/20 text-sc-red rounded-lg hover:bg-sc-red/30 transition-colors"
          >
            Try Again
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-6 animate-fade-in">
      <Breadcrumb />

      <PageHeader
        description="Crawl documentation and upload files to build your knowledge graph"
        meta={
          <div className="flex items-center gap-2 sm:gap-3 text-[10px] sm:text-xs">
            <span className="flex items-center gap-1 sm:gap-1.5 px-1.5 sm:px-2 py-1 bg-sc-bg-base rounded-lg">
              <Globe width={10} height={10} className="text-sc-purple sm:w-3 sm:h-3" />
              <span className="text-sc-fg-muted">{stats.total}</span>
            </span>
            <span className="hidden xs:flex items-center gap-1.5 px-2 py-1 bg-sc-bg-base rounded-lg">
              <Database width={12} height={12} className="text-sc-cyan" />
              <span className="text-sc-fg-muted">{stats.totalDocs} docs</span>
            </span>
            {stats.inProgress > 0 && (
              <span className="flex items-center gap-1 sm:gap-1.5 px-1.5 sm:px-2 py-1 bg-sc-purple/20 text-sc-purple rounded-lg animate-pulse">
                {stats.inProgress} crawling
              </span>
            )}
          </div>
        }
        action={
          <button
            type="button"
            onClick={() => setShowAddDialog(true)}
            className="shrink-0 px-4 py-2 bg-sc-purple hover:bg-sc-purple/80 text-white rounded-lg font-medium transition-colors flex items-center gap-1.5 text-sm"
          >
            <span>+</span>
            <span>Add Source</span>
          </button>
        }
      />

      {/* Filters & Controls */}
      <div className="flex flex-col sm:flex-row gap-3 sm:gap-4">
        {/* Search */}
        <div className="relative flex-1 sm:max-w-md">
          <Search
            width={16}
            height={16}
            className="absolute left-3 top-1/2 -translate-y-1/2 text-sc-fg-subtle"
          />
          <input
            type="text"
            value={searchQuery}
            onChange={e => setSearchQuery(e.target.value)}
            placeholder="Search sources..."
            className="w-full pl-10 pr-4 py-2 sm:py-2.5 bg-sc-bg-base border border-sc-fg-subtle/20 rounded-lg sm:rounded-xl text-sm sm:text-base text-sc-fg-primary placeholder:text-sc-fg-subtle focus:border-sc-purple focus:outline-none focus:ring-1 focus:ring-sc-purple/30 transition-colors"
          />
        </div>

        {/* Controls */}
        <div className="flex items-center gap-2">
          {/* Filter Toggle */}
          <button
            type="button"
            onClick={() => setShowFilters(!showFilters)}
            className={`flex items-center gap-1.5 sm:gap-2 px-2.5 sm:px-3 py-2 sm:py-2.5 rounded-lg sm:rounded-xl border transition-colors ${
              showFilters || filterStatus !== 'all' || filterType !== 'all'
                ? 'bg-sc-purple/20 border-sc-purple/40 text-sc-purple'
                : 'bg-sc-bg-base border-sc-fg-subtle/20 text-sc-fg-muted hover:border-sc-fg-subtle/40'
            }`}
          >
            <Filter width={16} height={16} />
            <span className="hidden xs:inline text-sm">Filters</span>
            {(filterStatus !== 'all' || filterType !== 'all') && (
              <span className="w-2 h-2 bg-sc-purple rounded-full" />
            )}
          </button>

          {/* View Mode Toggle - Desktop only */}
          <div className="hidden sm:flex bg-sc-bg-base border border-sc-fg-subtle/20 rounded-xl overflow-hidden">
            <button
              type="button"
              onClick={() => setViewMode('grid')}
              className={`p-2.5 transition-colors ${
                viewMode === 'grid'
                  ? 'bg-sc-purple/20 text-sc-purple'
                  : 'text-sc-fg-muted hover:text-sc-fg-primary'
              }`}
              title="Grid view"
            >
              <Grid3X3 width={16} height={16} />
            </button>
            <button
              type="button"
              onClick={() => setViewMode('list')}
              className={`p-2.5 transition-colors ${
                viewMode === 'list'
                  ? 'bg-sc-purple/20 text-sc-purple'
                  : 'text-sc-fg-muted hover:text-sc-fg-primary'
              }`}
              title="List view"
            >
              <LayoutList width={16} height={16} />
            </button>
          </div>

          {/* Refresh */}
          <button
            type="button"
            onClick={() => refetch()}
            className="p-2 sm:p-2.5 bg-sc-bg-base border border-sc-fg-subtle/20 rounded-lg sm:rounded-xl text-sc-fg-muted hover:text-sc-fg-primary hover:border-sc-fg-subtle/40 transition-colors"
            title="Refresh"
          >
            <RefreshCw width={16} height={16} />
          </button>
        </div>
      </div>

      {/* Expanded Filters */}
      <AnimatePresence>
        {showFilters && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            className="overflow-hidden"
          >
            <div className="flex flex-col sm:flex-row sm:flex-wrap gap-4 p-3 sm:p-4 bg-sc-bg-base border border-sc-fg-subtle/20 rounded-lg sm:rounded-xl">
              {/* Status Filter */}
              <div className="flex-1 min-w-0">
                <span className="block text-xs text-sc-fg-subtle mb-2">Status</span>
                <div className="flex flex-wrap gap-1.5">
                  {(
                    [
                      { value: 'all', label: 'All' },
                      { value: 'completed', label: 'Done' },
                      { value: 'pending', label: 'Pending' },
                      { value: 'in_progress', label: 'Active' },
                      { value: 'failed', label: 'Failed' },
                    ] as const
                  ).map(status => (
                    <button
                      key={status.value}
                      type="button"
                      onClick={() => setFilterStatus(status.value)}
                      className={`px-2.5 sm:px-3 py-1.5 text-xs rounded-lg transition-colors capitalize ${
                        filterStatus === status.value
                          ? 'bg-sc-purple text-white'
                          : 'bg-sc-bg-highlight text-sc-fg-muted hover:text-sc-fg-primary'
                      }`}
                    >
                      {status.label}
                    </button>
                  ))}
                </div>
              </div>

              {/* Type Filter */}
              <div className="flex-1 min-w-0">
                <span className="block text-xs text-sc-fg-subtle mb-2">Type</span>
                <div className="flex flex-wrap gap-1.5">
                  {(
                    [
                      { value: 'all', label: 'All', icon: null },
                      { value: 'website', label: 'Website', icon: Globe },
                      { value: 'local', label: 'Local', icon: Folder },
                    ] as const
                  ).map(type => {
                    const Icon = type.icon;
                    return (
                      <button
                        key={type.value}
                        type="button"
                        onClick={() => setFilterType(type.value)}
                        className={`flex items-center gap-1.5 px-2.5 sm:px-3 py-1.5 text-xs rounded-lg transition-colors ${
                          filterType === type.value
                            ? type.value === 'local'
                              ? 'bg-sc-yellow text-sc-bg-dark'
                              : 'bg-sc-purple text-white'
                            : 'bg-sc-bg-highlight text-sc-fg-muted hover:text-sc-fg-primary'
                        }`}
                      >
                        {Icon && <Icon width={12} height={12} />}
                        {type.label}
                      </button>
                    );
                  })}
                </div>
              </div>

              {/* Sort By */}
              <div className="flex-1 min-w-0">
                <span className="block text-xs text-sc-fg-subtle mb-2">Sort by</span>
                <div className="flex flex-wrap gap-1.5">
                  {(
                    [
                      { value: 'updated', label: 'Updated' },
                      { value: 'name', label: 'Name' },
                      { value: 'documents', label: 'Docs' },
                    ] as const
                  ).map(option => (
                    <button
                      key={option.value}
                      type="button"
                      onClick={() => setSortBy(option.value)}
                      className={`px-2.5 sm:px-3 py-1.5 text-xs rounded-lg transition-colors ${
                        sortBy === option.value
                          ? 'bg-sc-cyan text-sc-bg-dark'
                          : 'bg-sc-bg-highlight text-sc-fg-muted hover:text-sc-fg-primary'
                      }`}
                    >
                      {option.label}
                    </button>
                  ))}
                </div>
              </div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      {/* Sources Grid/List - always list on mobile */}
      {isLoading ? (
        <div
          className={
            viewMode === 'grid'
              ? 'grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 gap-3 sm:gap-4'
              : 'space-y-3'
          }
        >
          {[...Array(6)].map((_, i) => (
            <SourceCardSkeleton key={`skeleton-${i}`} />
          ))}
        </div>
      ) : filteredSources.length === 0 ? (
        sources.length === 0 ? (
          <EnhancedEmptyState
            icon={<Globe width={48} height={48} className="text-sc-cyan" />}
            title="No knowledge sources yet"
            description="Add documentation websites or upload files to build your knowledge graph"
            actions={[{ label: 'Add Your First Source', onClick: () => setShowAddDialog(true) }]}
          />
        ) : (
          <EnhancedEmptyState
            icon={<Search width={48} height={48} className="text-sc-yellow" />}
            title="No sources match your filters"
            description="Try adjusting your search or filters"
            variant="filtered"
            actions={[
              {
                label: 'Clear filters',
                onClick: () => {
                  setSearchQuery('');
                  setFilterStatus('all');
                  setFilterType('all');
                },
              },
            ]}
          />
        )
      ) : (
        <motion.div
          layout
          className={
            viewMode === 'grid'
              ? 'grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 gap-3 sm:gap-4'
              : 'space-y-3'
          }
        >
          <AnimatePresence mode="popLayout">
            {filteredSources.map(source => {
              const progressData = crawlProgressMap.get(source.id);
              const progress = progressData
                ? {
                    percentage: progressData.percentage,
                    pagesProcessed: progressData.pages_crawled,
                    documentsCreated: progressData.pages_crawled, // Use pages as docs
                    currentUrl: progressData.current_url,
                    status: `Crawling ${progressData.pages_crawled}/${progressData.max_pages} pages`,
                  }
                : undefined;

              return (
                <SourceCardEnhanced
                  key={source.id}
                  source={source}
                  onCrawl={handleCrawl}
                  onCancel={handleCancel}
                  onDelete={handleDelete}
                  onRefresh={handleSync}
                  isCrawling={
                    crawlingSourceIds.has(source.id) ||
                    source.metadata.crawl_status === 'in_progress'
                  }
                  progress={progress}
                />
              );
            })}
          </AnimatePresence>
        </motion.div>
      )}

      {/* Add Source Dialog */}
      <AddSourceDialog
        isOpen={showAddDialog}
        onClose={() => setShowAddDialog(false)}
        onSubmitUrl={handleAddSource}
        onSubmitLocal={handleAddLocalSource}
        isSubmitting={createSource.isPending}
      />
    </div>
  );
}
