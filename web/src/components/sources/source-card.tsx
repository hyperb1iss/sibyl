'use client';

import Link from 'next/link';
import { Button } from '@/components/ui/button';
import {
  CRAWL_STATUS_CONFIG,
  SOURCE_TYPE_CONFIG,
  type CrawlStatusType,
  type SourceTypeValue,
  formatDateTime,
} from '@/lib/constants';
import type { SourceSummary } from '@/lib/api';

interface SourceCardProps {
  source: SourceSummary;
  onCrawl?: (id: string) => void;
  onDelete?: (id: string) => void;
  isCrawling?: boolean;
}

export function SourceCard({ source, onCrawl, onDelete, isCrawling }: SourceCardProps) {
  const crawlStatus = (source.metadata.crawl_status as CrawlStatusType) || 'pending';
  const sourceType = (source.metadata.source_type as SourceTypeValue) || 'website';
  const documentCount = source.metadata.document_count || 0;
  const lastCrawled = source.metadata.last_crawled;
  const url = source.metadata.url || '';

  const statusConfig = CRAWL_STATUS_CONFIG[crawlStatus];
  const typeConfig = SOURCE_TYPE_CONFIG[sourceType];

  // Extract domain from URL for display
  const getDomain = (urlString: string) => {
    try {
      return new URL(urlString).hostname;
    } catch {
      return urlString;
    }
  };

  return (
    <div className="bg-sc-bg-base border border-sc-fg-subtle/20 rounded-xl p-4 hover:border-sc-purple/30 transition-colors">
      {/* Header */}
      <div className="flex items-start justify-between gap-3 mb-3">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 mb-1">
            <span className="text-lg">{typeConfig.icon}</span>
            <h3 className="text-base font-semibold text-sc-fg-primary truncate">
              {source.name}
            </h3>
          </div>
          <p className="text-xs text-sc-fg-subtle truncate" title={url}>
            {getDomain(url)}
          </p>
        </div>

        {/* Status Badge */}
        <div className={`flex items-center gap-1.5 px-2 py-1 rounded text-xs ${statusConfig.bgClass} ${statusConfig.textClass}`}>
          <span>{statusConfig.icon}</span>
          <span>{statusConfig.label}</span>
        </div>
      </div>

      {/* Description */}
      {source.description && (
        <p className="text-sm text-sc-fg-muted line-clamp-2 mb-3">
          {source.description}
        </p>
      )}

      {/* Stats Row */}
      <div className="flex items-center gap-4 text-xs text-sc-fg-subtle mb-4">
        <div className="flex items-center gap-1">
          <span>📄</span>
          <span>{documentCount} documents</span>
        </div>
        {lastCrawled && (
          <div className="flex items-center gap-1">
            <span>🕐</span>
            <span>Last crawled {formatDateTime(lastCrawled)}</span>
          </div>
        )}
      </div>

      {/* Actions */}
      <div className="flex items-center gap-2">
        <Button
          size="sm"
          variant="secondary"
          onClick={() => onCrawl?.(source.id)}
          disabled={isCrawling || crawlStatus === 'in_progress'}
          className="flex-1"
        >
          {crawlStatus === 'in_progress' ? 'Crawling...' : 'Crawl Now'}
        </Button>
        <Link
          href={`/sources/${source.id}`}
          className="px-3 py-1.5 text-sm rounded border border-sc-fg-subtle/20 text-sc-fg-muted hover:border-sc-purple/30 hover:text-sc-purple transition-colors"
        >
          View
        </Link>
        {onDelete && (
          <button
            onClick={() => onDelete(source.id)}
            className="px-3 py-1.5 text-sm rounded border border-sc-fg-subtle/20 text-sc-fg-subtle hover:border-[#ff6363]/30 hover:text-[#ff6363] transition-colors"
            title="Delete source"
          >
            ✕
          </button>
        )}
      </div>
    </div>
  );
}

export function SourceCardSkeleton() {
  return (
    <div className="bg-sc-bg-base border border-sc-fg-subtle/20 rounded-xl p-4 animate-pulse">
      <div className="flex items-start justify-between gap-3 mb-3">
        <div className="flex-1">
          <div className="h-5 bg-sc-fg-subtle/10 rounded w-3/4 mb-2" />
          <div className="h-3 bg-sc-fg-subtle/10 rounded w-1/2" />
        </div>
        <div className="h-6 w-20 bg-sc-fg-subtle/10 rounded" />
      </div>
      <div className="h-4 bg-sc-fg-subtle/10 rounded w-full mb-3" />
      <div className="flex gap-4 mb-4">
        <div className="h-3 bg-sc-fg-subtle/10 rounded w-24" />
        <div className="h-3 bg-sc-fg-subtle/10 rounded w-32" />
      </div>
      <div className="flex gap-2">
        <div className="h-8 bg-sc-fg-subtle/10 rounded flex-1" />
        <div className="h-8 w-16 bg-sc-fg-subtle/10 rounded" />
      </div>
    </div>
  );
}
