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
  Globe,
  Hash,
  Loader2,
  Play,
  RefreshCw,
  StopCircle,
} from '@/components/ui/icons';
import { CRAWL_STATUS_CONFIG, formatDateTime, SOURCE_TYPE_CONFIG } from '@/lib/constants';
import {
  useCancelCrawl,
  useCrawlSource,
  useSource,
  useSourcePages,
  useSyncSource,
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

  const [isCrawling, setIsCrawling] = useState(false);

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
        <div className="bg-sc-bg-base border border-sc-red/30 rounded-2xl p-8 text-center">
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
        <div className="bg-sc-bg-base border border-sc-fg-subtle/10 rounded-2xl p-8">
          <div className="animate-pulse space-y-4">
            <div className="h-8 bg-sc-bg-highlight rounded w-1/3" />
            <div className="h-4 bg-sc-bg-highlight rounded w-2/3" />
            <div className="grid grid-cols-4 gap-4 mt-6">
              {[...Array(4)].map((_, i) => (
                <div key={`skeleton-${i}`} className="h-20 bg-sc-bg-highlight rounded-xl" />
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
      <div className="bg-sc-bg-base border border-sc-fg-subtle/10 rounded-2xl p-6">
        <div className="flex items-start justify-between gap-4">
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-3 mb-2">
              <Globe width={24} height={24} className="text-sc-purple shrink-0" />
              <h1 className="text-2xl font-bold text-sc-fg-primary truncate">{source.name}</h1>
              <span
                className={`px-2.5 py-1 text-xs font-medium rounded-full ${statusConfig.bgClass} ${statusConfig.textClass}`}
              >
                {statusConfig.label}
              </span>
            </div>
            {source.description && <p className="text-sc-fg-muted mt-2">{source.description}</p>}
            <a
              href={source.url}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1.5 mt-3 text-sc-cyan hover:text-sc-purple transition-colors text-sm"
            >
              <ExternalLink width={14} height={14} />
              {source.url}
            </a>
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
                Cancel Crawl
              </button>
            ) : (
              <button
                type="button"
                onClick={handleCrawl}
                className="flex items-center gap-2 px-4 py-2.5 rounded-xl text-sm font-medium bg-sc-purple hover:bg-sc-purple/80 text-white transition-all"
              >
                {source.crawl_status === 'completed' ? (
                  <>
                    <RefreshCw width={16} height={16} />
                    Re-crawl
                  </>
                ) : (
                  <>
                    <Play width={16} height={16} />
                    Start Crawl
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
          </div>
        </div>

        {/* Stats */}
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-4 mt-6">
          <StatCard
            icon={<FileText width={18} height={18} className="text-sc-cyan" />}
            label="Documents"
            value={source.document_count}
          />
          <StatCard
            icon={<Hash width={18} height={18} className="text-sc-coral" />}
            label="Chunks"
            value={source.chunk_count}
          />
          <StatCard
            icon={<Globe width={18} height={18} className="text-sc-purple" />}
            label="Type"
            value={typeConfig.label}
          />
          <StatCard
            icon={<Clock width={18} height={18} className="text-sc-yellow" />}
            label="Crawl Depth"
            value={source.crawl_depth}
          />
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
              <span>Last crawled {formatDateTime(source.last_crawled_at)}</span>
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
      <div className="bg-sc-bg-base border border-sc-fg-subtle/10 rounded-2xl p-6">
        <h2 className="text-lg font-semibold text-sc-fg-primary mb-4 flex items-center gap-2">
          <FileText width={20} height={20} className="text-sc-cyan" />
          Documents
          <span className="text-sc-fg-subtle font-normal">({pages.length})</span>
        </h2>

        {pagesLoading ? (
          <div className="flex items-center justify-center py-8">
            <Loader2 width={24} height={24} className="animate-spin text-sc-purple" />
          </div>
        ) : pages.length === 0 ? (
          <div className="text-center py-8 text-sc-fg-subtle">
            <FileText width={32} height={32} className="mx-auto mb-2 opacity-50" />
            <p>No documents crawled yet</p>
            <p className="text-sm mt-1">Start a crawl to fetch documents from this source</p>
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
    <div className="bg-sc-bg-dark rounded-xl p-4">
      <div className="flex items-center gap-2 mb-1">
        {icon}
        <span className="text-xs text-sc-fg-subtle">{label}</span>
      </div>
      <p className="text-xl font-bold text-sc-fg-primary">{value}</p>
    </div>
  );
}
