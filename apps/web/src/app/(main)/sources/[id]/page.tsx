'use client';

import Link from 'next/link';
import { use, useCallback, useState } from 'react';
import { toast } from 'sonner';
import { Breadcrumb, ROUTE_CONFIG } from '@/components/layout/breadcrumb';
import {
  ArrowLeft,
  Calendar,
  ChevronRight,
  Clock,
  ExternalLink,
  FileText,
  Folder,
  Globe,
  Hash,
  Loader2,
  Play,
  RefreshCw,
  Settings,
  StopCircle,
  X,
} from '@/components/ui/icons';
import { CRAWL_STATUS_CONFIG, formatDateTime, SOURCE_TYPE_CONFIG } from '@/lib/constants';
import {
  useCancelCrawl,
  useCrawlSource,
  useSource,
  useSourcePages,
  useSyncSource,
  useUpdateSource,
} from '@/lib/hooks';

interface SourceDetailPageProps {
  params: Promise<{ id: string }>;
}

export default function SourceDetailPage({ params }: SourceDetailPageProps) {
  const { id } = use(params);
  const { data: source, isLoading, error } = useSource(id);
  const { data: pagesData, isLoading: pagesLoading } = useSourcePages(id);
  const crawlSource = useCrawlSource();
  const cancelCrawl = useCancelCrawl();
  const syncSource = useSyncSource();
  const updateSource = useUpdateSource();

  const [isCrawling, setIsCrawling] = useState(false);
  const [isEditOpen, setIsEditOpen] = useState(false);
  const [editForm, setEditForm] = useState({
    crawl_depth: 2,
    include_patterns: '',
    exclude_patterns: '',
  });

  const openEditModal = useCallback(() => {
    if (source) {
      setEditForm({
        crawl_depth: source.crawl_depth,
        include_patterns: source.include_patterns?.join('\n') || '',
        exclude_patterns: source.exclude_patterns?.join('\n') || '',
      });
      setIsEditOpen(true);
    }
  }, [source]);

  const handleSaveSettings = useCallback(async () => {
    try {
      await updateSource.mutateAsync({
        id,
        updates: {
          crawl_depth: editForm.crawl_depth,
          include_patterns: editForm.include_patterns
            .split('\n')
            .map(p => p.trim())
            .filter(Boolean),
          exclude_patterns: editForm.exclude_patterns
            .split('\n')
            .map(p => p.trim())
            .filter(Boolean),
        },
      });
      setIsEditOpen(false);
      toast.success('Settings updated');
    } catch {
      toast.error('Failed to update settings');
    }
  }, [id, editForm, updateSource]);

  const handleCrawl = useCallback(async () => {
    if (isCrawling || source?.crawl_status === 'in_progress') {
      toast.info('Crawl already in progress');
      return;
    }
    try {
      setIsCrawling(true);
      await crawlSource.mutateAsync(id);
      toast.success('Started crawling');
    } catch {
      toast.error('Failed to start crawl');
      setIsCrawling(false);
    }
  }, [id, isCrawling, source, crawlSource]);

  const handleCancel = useCallback(async () => {
    try {
      await cancelCrawl.mutateAsync(id);
      setIsCrawling(false);
      toast.success('Cancelled crawl');
    } catch {
      toast.error('Failed to cancel crawl');
    }
  }, [id, cancelCrawl]);

  const handleSync = useCallback(async () => {
    try {
      await syncSource.mutateAsync(id);
      setIsCrawling(false);
      toast.success('Synced source');
    } catch {
      toast.error('Failed to sync source');
    }
  }, [id, syncSource]);

  const breadcrumbItems = [
    { label: ROUTE_CONFIG[''].label, href: '/', icon: ROUTE_CONFIG[''].icon },
    { label: 'Sources', href: '/sources', icon: ROUTE_CONFIG.sources.icon },
    { label: source?.name || 'Loading...' },
  ];

  if (error) {
    return (
      <div className="space-y-4 animate-fade-in">
        <Breadcrumb items={breadcrumbItems} />
        <div className="bg-sc-bg-base border border-sc-red/40 rounded-2xl p-8 text-center shadow-glow-red">
          <p className="text-sc-red font-medium">Failed to load source</p>
          <p className="text-sc-fg-subtle text-sm mt-2">
            {error instanceof Error ? error.message : 'Unknown error'}
          </p>
          <Link
            href="/sources"
            className="inline-flex items-center gap-2 mt-4 text-sc-cyan hover:text-sc-purple transition-colors"
          >
            <ArrowLeft width={16} height={16} />
            Back to Sources
          </Link>
        </div>
      </div>
    );
  }

  if (isLoading || !source) {
    return (
      <div className="space-y-6 animate-fade-in">
        <Breadcrumb items={breadcrumbItems} />
        <div className="bg-sc-bg-base border border-sc-fg-subtle/30 rounded-2xl p-8 shadow-card-elevated">
          <div className="animate-pulse space-y-4">
            <div className="h-8 bg-sc-bg-highlight rounded w-1/3" />
            <div className="h-4 bg-sc-bg-highlight rounded w-2/3" />
            <div className="grid grid-cols-4 gap-4 mt-6">
              {[...Array(4)].map((_, i) => (
                <div
                  key={`skeleton-${i}`}
                  className="h-20 bg-sc-bg-highlight rounded-xl shadow-card"
                />
              ))}
            </div>
          </div>
        </div>
      </div>
    );
  }

  const isActive = isCrawling || source.crawl_status === 'in_progress';
  const statusConfig = CRAWL_STATUS_CONFIG[source.crawl_status] || CRAWL_STATUS_CONFIG.pending;
  const typeConfig = SOURCE_TYPE_CONFIG[source.source_type] || SOURCE_TYPE_CONFIG.website;
  const pages = pagesData?.pages || [];

  return (
    <div className="space-y-6 animate-fade-in">
      <Breadcrumb items={breadcrumbItems} />

      {/* Header */}
      <div className="bg-sc-bg-base border border-sc-fg-subtle/30 rounded-2xl p-6 shadow-card-elevated">
        <div className="flex items-start justify-between gap-4">
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-3 mb-2">
              {source.source_type === 'local' ? (
                <Folder width={24} height={24} className="text-sc-yellow shrink-0" />
              ) : (
                <Globe width={24} height={24} className="text-sc-purple shrink-0" />
              )}
              <h1 className="text-2xl font-bold text-sc-fg-primary truncate">{source.name}</h1>
              <span
                className={`px-2.5 py-1 text-xs font-medium rounded-full ${statusConfig.bgClass} ${statusConfig.textClass}`}
              >
                {statusConfig.label}
              </span>
            </div>
            {source.description && <p className="text-sc-fg-muted mt-2">{source.description}</p>}
            {source.source_type === 'local' ? (
              <div className="inline-flex items-center gap-1.5 mt-3 text-sc-fg-muted text-sm">
                <Folder width={14} height={14} className="text-sc-yellow" />
                <span className="font-mono">{source.url.replace('file://', '')}</span>
              </div>
            ) : (
              <a
                href={source.url}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-1.5 mt-3 text-sc-cyan hover:text-sc-purple transition-colors text-sm"
              >
                <ExternalLink width={14} height={14} />
                {source.url}
              </a>
            )}
          </div>

          {/* Actions */}
          <div className="flex items-center gap-2 shrink-0">
            {isActive ? (
              <button
                type="button"
                onClick={handleCancel}
                className="flex items-center gap-2 px-4 py-2.5 rounded-xl text-sm font-medium bg-sc-red/20 text-sc-red hover:bg-sc-red/30 border border-sc-red/30 transition-all"
              >
                <StopCircle width={16} height={16} />
                Cancel {source.source_type === 'local' ? 'Sync' : 'Crawl'}
              </button>
            ) : (
              <button
                type="button"
                onClick={handleCrawl}
                className={`flex items-center gap-2 px-4 py-2.5 rounded-xl text-sm font-medium transition-all ${
                  source.source_type === 'local'
                    ? 'bg-sc-yellow hover:bg-sc-yellow/80 text-sc-bg-dark'
                    : 'bg-sc-purple hover:bg-sc-purple/80 text-white'
                }`}
              >
                {source.crawl_status === 'completed' ? (
                  <>
                    <RefreshCw width={16} height={16} />
                    {source.source_type === 'local' ? 'Re-sync' : 'Re-crawl'}
                  </>
                ) : (
                  <>
                    <Play width={16} height={16} />
                    {source.source_type === 'local' ? 'Sync' : 'Start Crawl'}
                  </>
                )}
              </button>
            )}
            <button
              type="button"
              onClick={handleSync}
              className="flex items-center gap-2 px-4 py-2.5 rounded-xl text-sm font-medium bg-sc-bg-highlight text-sc-fg-muted hover:text-sc-cyan hover:bg-sc-cyan/10 border border-sc-fg-subtle/10 transition-colors"
            >
              <RefreshCw width={16} height={16} />
              Sync
            </button>
            {source.source_type !== 'local' && (
              <button
                type="button"
                onClick={openEditModal}
                className="flex items-center gap-2 px-4 py-2.5 rounded-xl text-sm font-medium bg-sc-bg-highlight text-sc-fg-muted hover:text-sc-purple hover:bg-sc-purple/10 border border-sc-fg-subtle/10 transition-colors"
              >
                <Settings width={16} height={16} />
                Settings
              </button>
            )}
          </div>
        </div>

        {/* Stats */}
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-4 mt-6">
          <StatCard
            icon={<FileText width={18} height={18} className="text-sc-cyan" />}
            label={source.source_type === 'local' ? 'Files' : 'Documents'}
            value={source.document_count}
          />
          <StatCard
            icon={<Hash width={18} height={18} className="text-sc-coral" />}
            label="Chunks"
            value={source.chunk_count}
          />
          <StatCard
            icon={
              source.source_type === 'local' ? (
                <Folder width={18} height={18} className="text-sc-yellow" />
              ) : (
                <Globe width={18} height={18} className="text-sc-purple" />
              )
            }
            label="Type"
            value={typeConfig.label}
          />
          {source.source_type !== 'local' && (
            <StatCard
              icon={<Clock width={18} height={18} className="text-sc-yellow" />}
              label="Crawl Depth"
              value={source.crawl_depth}
            />
          )}
        </div>

        {/* Timestamps */}
        <div className="flex items-center gap-6 mt-6 text-xs text-sc-fg-subtle">
          <div className="flex items-center gap-1.5">
            <Calendar width={12} height={12} />
            <span>Created {formatDateTime(source.created_at)}</span>
          </div>
          {source.last_crawled_at && (
            <div className="flex items-center gap-1.5">
              <Clock width={12} height={12} />
              <span>
                Last {source.source_type === 'local' ? 'synced' : 'crawled'}{' '}
                {formatDateTime(source.last_crawled_at)}
              </span>
            </div>
          )}
        </div>

        {/* Error Message */}
        {source.last_error && (
          <div className="mt-4 p-3 bg-sc-red/10 border border-sc-red/20 rounded-xl">
            <p className="text-sm text-sc-red">{source.last_error}</p>
          </div>
        )}

        {/* Crawl Patterns */}
        {(source.include_patterns?.length > 0 || source.exclude_patterns?.length > 0) && (
          <div className="mt-6 grid grid-cols-1 sm:grid-cols-2 gap-4">
            {source.include_patterns?.length > 0 && (
              <div>
                <h3 className="text-xs font-medium text-sc-fg-subtle mb-2">Include Patterns</h3>
                <div className="flex flex-wrap gap-1.5">
                  {source.include_patterns.map(p => (
                    <span
                      key={p}
                      className="px-2 py-0.5 text-xs font-mono bg-sc-green/10 text-sc-green rounded"
                    >
                      {p}
                    </span>
                  ))}
                </div>
              </div>
            )}
            {source.exclude_patterns?.length > 0 && (
              <div>
                <h3 className="text-xs font-medium text-sc-fg-subtle mb-2">Exclude Patterns</h3>
                <div className="flex flex-wrap gap-1.5">
                  {source.exclude_patterns.map(p => (
                    <span
                      key={p}
                      className="px-2 py-0.5 text-xs font-mono bg-sc-red/10 text-sc-red rounded"
                    >
                      {p}
                    </span>
                  ))}
                </div>
              </div>
            )}
          </div>
        )}
      </div>

      {/* Documents List */}
      <div className="bg-sc-bg-base border border-sc-fg-subtle/30 rounded-2xl p-6 shadow-card-elevated">
        <h2 className="text-lg font-semibold text-sc-fg-primary mb-4 flex items-center gap-2">
          <FileText width={20} height={20} className="text-sc-cyan" />
          {source.source_type === 'local' ? 'Files' : 'Documents'}
          <span className="text-sc-fg-subtle font-normal">({pages.length})</span>
        </h2>

        {pagesLoading ? (
          <div className="flex items-center justify-center py-8">
            <Loader2 width={24} height={24} className="animate-spin text-sc-purple" />
          </div>
        ) : pages.length === 0 ? (
          <div className="text-center py-8 text-sc-fg-subtle">
            <FileText width={32} height={32} className="mx-auto mb-2 opacity-50" />
            <p>No {source.source_type === 'local' ? 'files indexed' : 'documents crawled'} yet</p>
            <p className="text-sm mt-1">
              {source.source_type === 'local'
                ? 'Sync the directory to index files'
                : 'Start a crawl to fetch documents from this source'}
            </p>
          </div>
        ) : (
          <div className="space-y-2">
            {pages.map(page => (
              <Link
                key={page.id}
                href={`/sources/${id}/documents/${page.id}`}
                className="flex items-center justify-between gap-4 p-3 bg-sc-bg-dark rounded-xl hover:bg-sc-bg-highlight transition-colors group"
              >
                <div className="flex-1 min-w-0">
                  <p className="font-medium text-sc-fg-primary truncate group-hover:text-sc-cyan transition-colors">
                    {page.title}
                  </p>
                  <p className="text-xs text-sc-fg-subtle truncate">{page.url}</p>
                </div>
                <div className="flex items-center gap-3 shrink-0">
                  {page.is_index && (
                    <span className="px-2 py-0.5 text-xs bg-sc-purple/20 text-sc-purple rounded">
                      Index
                    </span>
                  )}
                  {page.has_code && (
                    <span className="px-2 py-0.5 text-xs bg-sc-cyan/20 text-sc-cyan rounded">
                      Code
                    </span>
                  )}
                  <span className="text-xs text-sc-fg-subtle">
                    {page.word_count.toLocaleString()} words
                  </span>
                  <button
                    type="button"
                    onClick={e => {
                      e.preventDefault();
                      e.stopPropagation();
                      window.open(page.url, '_blank', 'noopener,noreferrer');
                    }}
                    className="p-1.5 text-sc-fg-subtle hover:text-sc-cyan transition-colors"
                  >
                    <ExternalLink width={14} height={14} />
                  </button>
                  <ChevronRight
                    width={16}
                    height={16}
                    className="text-sc-fg-subtle group-hover:text-sc-cyan transition-colors"
                  />
                </div>
              </Link>
            ))}
          </div>
        )}
      </div>

      {/* Edit Settings Modal */}
      {isEditOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center">
          <div
            className="absolute inset-0 bg-black/60 backdrop-blur-sm"
            onClick={() => setIsEditOpen(false)}
            onKeyDown={e => e.key === 'Escape' && setIsEditOpen(false)}
          />
          <div className="relative bg-sc-bg-base border border-sc-fg-subtle/20 rounded-2xl p-6 w-full max-w-md shadow-2xl">
            <div className="flex items-center justify-between mb-6">
              <h2 className="text-lg font-semibold text-sc-fg-primary">Crawl Settings</h2>
              <button
                type="button"
                onClick={() => setIsEditOpen(false)}
                className="p-1.5 text-sc-fg-subtle hover:text-sc-fg-primary transition-colors"
              >
                <X width={18} height={18} />
              </button>
            </div>

            <div className="space-y-4">
              <div>
                <label
                  htmlFor="crawl_depth"
                  className="block text-sm font-medium text-sc-fg-muted mb-1.5"
                >
                  Crawl Depth
                </label>
                <input
                  id="crawl_depth"
                  type="number"
                  min={1}
                  max={5}
                  value={editForm.crawl_depth}
                  onChange={e => setEditForm(f => ({ ...f, crawl_depth: Number(e.target.value) }))}
                  className="w-full px-3 py-2 bg-sc-bg-dark border border-sc-fg-subtle/20 rounded-lg text-sc-fg-primary focus:outline-none focus:border-sc-purple"
                />
                <p className="text-xs text-sc-fg-subtle mt-1">
                  How many links deep to follow (1-5)
                </p>
              </div>

              <div>
                <label
                  htmlFor="include_patterns"
                  className="block text-sm font-medium text-sc-fg-muted mb-1.5"
                >
                  Include Patterns
                </label>
                <textarea
                  id="include_patterns"
                  value={editForm.include_patterns}
                  onChange={e => setEditForm(f => ({ ...f, include_patterns: e.target.value }))}
                  placeholder="One pattern per line"
                  rows={3}
                  className="w-full px-3 py-2 bg-sc-bg-dark border border-sc-fg-subtle/20 rounded-lg text-sc-fg-primary font-mono text-sm focus:outline-none focus:border-sc-purple resize-none"
                />
              </div>

              <div>
                <label
                  htmlFor="exclude_patterns"
                  className="block text-sm font-medium text-sc-fg-muted mb-1.5"
                >
                  Exclude Patterns
                </label>
                <textarea
                  id="exclude_patterns"
                  value={editForm.exclude_patterns}
                  onChange={e => setEditForm(f => ({ ...f, exclude_patterns: e.target.value }))}
                  placeholder="One pattern per line"
                  rows={3}
                  className="w-full px-3 py-2 bg-sc-bg-dark border border-sc-fg-subtle/20 rounded-lg text-sc-fg-primary font-mono text-sm focus:outline-none focus:border-sc-purple resize-none"
                />
              </div>
            </div>

            <div className="flex justify-end gap-3 mt-6">
              <button
                type="button"
                onClick={() => setIsEditOpen(false)}
                className="px-4 py-2 text-sm font-medium text-sc-fg-muted hover:text-sc-fg-primary transition-colors"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={handleSaveSettings}
                disabled={updateSource.isPending}
                className="px-4 py-2 text-sm font-medium bg-sc-purple hover:bg-sc-purple/80 text-white rounded-lg transition-colors disabled:opacity-50"
              >
                {updateSource.isPending ? 'Saving...' : 'Save Changes'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function StatCard({
  icon,
  label,
  value,
}: {
  icon: React.ReactNode;
  label: string;
  value: string | number;
}) {
  return (
    <div className="bg-sc-bg-base border border-sc-fg-subtle/30 rounded-xl p-4 shadow-card">
      <div className="flex items-center gap-2 mb-1">
        {icon}
        <span className="text-xs text-sc-fg-subtle">{label}</span>
      </div>
      <p className="text-xl font-bold text-sc-fg-primary">{value}</p>
    </div>
  );
}
