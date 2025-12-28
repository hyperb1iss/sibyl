'use client';

import { AnimatePresence, motion } from 'motion/react';
import Link from 'next/link';
import { memo, useState } from 'react';
import {
  AlertCircle,
  CheckCircle2,
  Clock,
  ExternalLink,
  FileText,
  Folder,
  Globe,
  Loader2,
  MoreVertical,
  Play,
  RefreshCw,
  StopCircle,
  Trash2,
} from '@/components/ui/icons';
import type { SourceSummary } from '@/lib/api';
import {
  CRAWL_STATUS_CONFIG,
  type CrawlStatusType,
  formatDateTime,
  type SourceTypeValue,
} from '@/lib/constants';

interface SourceCardEnhancedProps {
  source: SourceSummary;
  onCrawl?: (id: string) => void;
  onCancel?: (id: string) => void;
  onDelete?: (id: string) => void;
  onRefresh?: (id: string) => void;
  isCrawling?: boolean;
  progress?: CrawlProgress;
}

export interface CrawlProgress {
  percentage: number;
  pagesProcessed: number;
  documentsCreated: number;
  currentUrl?: string;
  status: string;
}

const SOURCE_TYPE_ICONS: Record<SourceTypeValue, React.ReactNode> = {
  website: <Globe width={18} height={18} className="text-sc-purple" />,
  github: <Globe width={18} height={18} className="text-sc-cyan" />,
  local: <Folder width={18} height={18} className="text-sc-yellow" />,
  api_docs: <FileText width={18} height={18} className="text-sc-green" />,
};

export const SourceCardEnhanced = memo(function SourceCardEnhanced({
  source,
  onCrawl,
  onCancel,
  onDelete,
  onRefresh,
  isCrawling,
  progress,
}: SourceCardEnhancedProps) {
  const [showMenu, setShowMenu] = useState(false);
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false);

  const crawlStatus = (source.metadata.crawl_status as CrawlStatusType) || 'pending';
  const sourceType = (source.metadata.source_type as SourceTypeValue) || 'website';
  const documentCount = (source.metadata.document_count as number) || 0;
  const totalTokens = (source.metadata.total_tokens as number) || 0;
  const lastCrawled = source.metadata.last_crawled as string | undefined;
  const url = (source.metadata.url as string) || '';
  const tags = (source.metadata.tags as string[]) || [];
  const crawlError = source.metadata.crawl_error as string | undefined;

  const statusConfig = CRAWL_STATUS_CONFIG[crawlStatus];
  const isActive = crawlStatus === 'in_progress' || isCrawling;

  // Extract domain from URL
  const getDomain = (urlString: string) => {
    try {
      return new URL(urlString).hostname;
    } catch {
      return urlString;
    }
  };

  // Format token count
  const formatTokens = (tokens: number) => {
    if (tokens >= 1000000) return `${(tokens / 1000000).toFixed(1)}M`;
    if (tokens >= 1000) return `${(tokens / 1000).toFixed(1)}K`;
    return tokens.toString();
  };

  return (
    <motion.div
      layout
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, scale: 0.95 }}
      className={`group relative bg-gradient-to-br from-sc-bg-base to-sc-bg-elevated border rounded-2xl transition-all duration-300 ${
        isActive
          ? 'border-sc-purple/50 shadow-lg shadow-sc-purple/20'
          : crawlStatus === 'failed'
            ? 'border-sc-red/30 hover:border-sc-red/50'
            : 'border-sc-fg-subtle/20 hover:border-sc-purple/40 hover:shadow-xl hover:shadow-black/20'
      }`}
    >
      {/* Progress bar overlay for active crawls */}
      {isActive && progress && (
        <div className="absolute top-0 left-0 right-0 h-1 bg-sc-bg-dark overflow-hidden">
          <motion.div
            initial={{ width: 0 }}
            animate={{ width: `${progress.percentage}%` }}
            transition={{ duration: 0.3 }}
            className="h-full bg-gradient-to-r from-sc-purple via-sc-cyan to-sc-purple"
          />
        </div>
      )}

      <div className="p-5">
        {/* Header Row */}
        <div className="flex items-start justify-between gap-3 mb-4">
          <div className="flex items-start gap-3 min-w-0 flex-1">
            {/* Source Type Icon */}
            <div
              className={`shrink-0 w-10 h-10 rounded-xl flex items-center justify-center ${
                isActive
                  ? 'bg-sc-purple/20 animate-pulse'
                  : 'bg-sc-bg-dark border border-sc-fg-subtle/10'
              }`}
            >
              {isActive ? (
                <Loader2 width={18} height={18} className="text-sc-purple animate-spin" />
              ) : (
                SOURCE_TYPE_ICONS[sourceType]
              )}
            </div>

            {/* Title & URL */}
            <div className="min-w-0 flex-1">
              <h3 className="text-base font-semibold text-sc-fg-primary truncate group-hover:text-sc-purple transition-colors">
                {source.name}
              </h3>
              {sourceType === 'local' ? (
                <span className="inline-flex items-center gap-1.5 text-xs text-sc-fg-subtle truncate max-w-full">
                  <Folder width={12} height={12} className="text-sc-yellow shrink-0" />
                  <span className="font-mono truncate">{url.replace('file://', '')}</span>
                </span>
              ) : (
                <a
                  href={url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex items-center gap-1 text-xs text-sc-fg-subtle hover:text-sc-cyan transition-colors truncate max-w-full"
                  onClick={e => e.stopPropagation()}
                >
                  {getDomain(url)}
                  <ExternalLink width={10} height={10} />
                </a>
              )}
            </div>
          </div>

          {/* Status Badge */}
          <div
            className={`shrink-0 flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-xs font-medium ${statusConfig.bgClass} ${statusConfig.textClass}`}
          >
            {isActive ? (
              <Loader2 width={12} height={12} className="animate-spin" />
            ) : crawlStatus === 'completed' ? (
              <CheckCircle2 width={12} height={12} />
            ) : crawlStatus === 'failed' ? (
              <AlertCircle width={12} height={12} />
            ) : (
              <Clock width={12} height={12} />
            )}
            {isActive
              ? sourceType === 'local'
                ? 'Syncing...'
                : 'Crawling...'
              : statusConfig.label}
          </div>
        </div>

        {/* Description */}
        {source.description && (
          <p className="text-sm text-sc-fg-muted line-clamp-2 mb-4">{source.description}</p>
        )}

        {/* Active Crawl Progress */}
        <AnimatePresence>
          {isActive && progress && (
            <motion.div
              initial={{ height: 0, opacity: 0 }}
              animate={{ height: 'auto', opacity: 1 }}
              exit={{ height: 0, opacity: 0 }}
              className="mb-4 overflow-hidden"
            >
              <div className="bg-sc-bg-dark rounded-xl p-3 space-y-2">
                <div className="flex justify-between text-xs">
                  <span className="text-sc-fg-muted">{progress.status}</span>
                  <span className="text-sc-purple font-medium">{progress.percentage}%</span>
                </div>
                {progress.currentUrl && (
                  <p className="text-xs text-sc-fg-subtle truncate font-mono">
                    {progress.currentUrl}
                  </p>
                )}
                <div className="grid grid-cols-2 gap-3 pt-1">
                  <div className="text-center">
                    <p className="text-lg font-bold text-sc-cyan">{progress.pagesProcessed}</p>
                    <p className="text-xs text-sc-fg-subtle">Pages</p>
                  </div>
                  <div className="text-center">
                    <p className="text-lg font-bold text-sc-green">{progress.documentsCreated}</p>
                    <p className="text-xs text-sc-fg-subtle">Documents</p>
                  </div>
                </div>
              </div>
            </motion.div>
          )}
        </AnimatePresence>

        {/* Error Message */}
        {crawlStatus === 'failed' && crawlError && (
          <div className="mb-4 p-3 bg-sc-red/10 border border-sc-red/20 rounded-xl">
            <p className="text-xs text-sc-red line-clamp-2">{crawlError}</p>
          </div>
        )}

        {/* Stats Row */}
        <div className="flex items-center gap-4 text-xs text-sc-fg-subtle mb-4">
          <div className="flex items-center gap-1.5 px-2 py-1 bg-sc-bg-dark rounded-lg">
            <FileText width={12} height={12} className="text-sc-cyan" />
            <span className="font-medium text-sc-fg-muted">{documentCount}</span>
            <span>docs</span>
          </div>
          {totalTokens > 0 && (
            <div className="flex items-center gap-1.5 px-2 py-1 bg-sc-bg-dark rounded-lg">
              <span className="font-medium text-sc-fg-muted">{formatTokens(totalTokens)}</span>
              <span>tokens</span>
            </div>
          )}
          {lastCrawled && (
            <div className="flex items-center gap-1.5 text-sc-fg-subtle">
              <Clock width={12} height={12} />
              <span>{formatDateTime(lastCrawled)}</span>
            </div>
          )}
        </div>

        {/* Tags */}
        {tags.length > 0 && (
          <div className="flex flex-wrap gap-1.5 mb-4">
            {tags.slice(0, 4).map(tag => (
              <span
                key={tag}
                className="px-2 py-0.5 text-[10px] font-medium rounded-full bg-sc-purple/10 text-sc-purple border border-sc-purple/20"
              >
                {tag}
              </span>
            ))}
            {tags.length > 4 && (
              <span className="px-2 py-0.5 text-[10px] rounded-full bg-sc-bg-dark text-sc-fg-subtle">
                +{tags.length - 4}
              </span>
            )}
          </div>
        )}

        {/* Actions */}
        <div className="flex items-center gap-2">
          {/* Primary Action - Crawl or Cancel */}
          {isActive ? (
            <button
              type="button"
              onClick={() => onCancel?.(source.id)}
              className="flex-1 flex items-center justify-center gap-2 px-4 py-2.5 rounded-xl text-sm font-medium bg-sc-red/20 text-sc-red hover:bg-sc-red/30 border border-sc-red/30 transition-all"
            >
              <StopCircle width={14} height={14} />
              Cancel
            </button>
          ) : (
            <button
              type="button"
              onClick={() => onCrawl?.(source.id)}
              className={`flex-1 flex items-center justify-center gap-2 px-4 py-2.5 rounded-xl text-sm font-medium transition-all ${
                crawlStatus === 'completed'
                  ? 'bg-sc-bg-highlight text-sc-fg-primary hover:bg-sc-purple/20 hover:text-sc-purple border border-sc-fg-subtle/10'
                  : 'bg-sc-purple hover:bg-sc-purple/80 text-white shadow-lg shadow-sc-purple/25'
              }`}
            >
              {crawlStatus === 'completed' ? (
                <>
                  <RefreshCw width={14} height={14} />
                  {sourceType === 'local' ? 'Re-sync' : 'Re-crawl'}
                </>
              ) : (
                <>
                  <Play width={14} height={14} />
                  {sourceType === 'local' ? 'Sync' : 'Start Crawl'}
                </>
              )}
            </button>
          )}

          {/* View Button */}
          <Link
            href={`/sources/${source.id}`}
            className="px-4 py-2.5 rounded-xl text-sm font-medium bg-sc-bg-highlight text-sc-fg-muted hover:text-sc-cyan hover:bg-sc-cyan/10 border border-sc-fg-subtle/10 transition-colors"
          >
            View
          </Link>

          {/* More Actions */}
          <div className="relative">
            <button
              type="button"
              onClick={() => setShowMenu(!showMenu)}
              className="p-2.5 rounded-xl text-sc-fg-subtle hover:text-sc-fg-primary hover:bg-sc-bg-highlight transition-colors"
            >
              <MoreVertical width={16} height={16} />
            </button>

            <AnimatePresence>
              {showMenu && (
                <>
                  {/* Backdrop */}
                  <button
                    type="button"
                    aria-label="Close menu"
                    className="fixed inset-0 z-10 cursor-default"
                    onClick={() => setShowMenu(false)}
                    onKeyDown={e => e.key === 'Escape' && setShowMenu(false)}
                  />

                  {/* Menu - opens upward to avoid card overflow clipping */}
                  <motion.div
                    initial={{ opacity: 0, scale: 0.95, y: 4 }}
                    animate={{ opacity: 1, scale: 1, y: 0 }}
                    exit={{ opacity: 0, scale: 0.95, y: 4 }}
                    className="absolute right-0 bottom-full mb-1 z-20 w-40 bg-sc-bg-elevated border border-sc-fg-subtle/20 rounded-xl shadow-xl overflow-hidden"
                  >
                    {onRefresh && (
                      <button
                        type="button"
                        onClick={() => {
                          onRefresh(source.id);
                          setShowMenu(false);
                        }}
                        className="w-full flex items-center gap-2 px-3 py-2 text-sm text-sc-fg-muted hover:text-sc-fg-primary hover:bg-sc-bg-highlight transition-colors"
                      >
                        <RefreshCw width={14} height={14} />
                        Refresh
                      </button>
                    )}
                    {onDelete && (
                      <button
                        type="button"
                        onClick={() => {
                          setShowDeleteConfirm(true);
                          setShowMenu(false);
                        }}
                        className="w-full flex items-center gap-2 px-3 py-2 text-sm text-sc-red hover:bg-sc-red/10 transition-colors"
                      >
                        <Trash2 width={14} height={14} />
                        Delete
                      </button>
                    )}
                  </motion.div>
                </>
              )}
            </AnimatePresence>
          </div>
        </div>
      </div>

      {/* Delete Confirmation */}
      <AnimatePresence>
        {showDeleteConfirm && (
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            className="absolute inset-0 bg-sc-bg-dark/95 backdrop-blur-sm flex items-center justify-center p-4"
          >
            <div className="text-center space-y-3">
              <AlertCircle width={32} height={32} className="mx-auto text-sc-red" />
              <p className="text-sm text-sc-fg-primary">Delete this source?</p>
              <p className="text-xs text-sc-fg-subtle">
                This will remove all {documentCount} documents.
              </p>
              <div className="flex gap-2 justify-center">
                <button
                  type="button"
                  onClick={() => setShowDeleteConfirm(false)}
                  className="px-4 py-2 text-sm text-sc-fg-muted hover:text-sc-fg-primary transition-colors"
                >
                  Cancel
                </button>
                <button
                  type="button"
                  onClick={() => {
                    onDelete?.(source.id);
                    setShowDeleteConfirm(false);
                  }}
                  className="px-4 py-2 text-sm bg-sc-red text-white rounded-lg hover:bg-sc-red/80 transition-colors"
                >
                  Delete
                </button>
              </div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </motion.div>
  );
});

export function SourceCardSkeleton() {
  return (
    <div className="bg-sc-bg-base border border-sc-fg-subtle/20 rounded-2xl p-5 animate-pulse">
      <div className="flex items-start gap-3 mb-4">
        <div className="w-10 h-10 rounded-xl bg-sc-fg-subtle/10" />
        <div className="flex-1">
          <div className="h-5 bg-sc-fg-subtle/10 rounded w-3/4 mb-2" />
          <div className="h-3 bg-sc-fg-subtle/10 rounded w-1/2" />
        </div>
        <div className="h-6 w-20 bg-sc-fg-subtle/10 rounded-lg" />
      </div>
      <div className="h-4 bg-sc-fg-subtle/10 rounded w-full mb-4" />
      <div className="flex gap-3 mb-4">
        <div className="h-6 w-20 bg-sc-fg-subtle/10 rounded-lg" />
        <div className="h-6 w-24 bg-sc-fg-subtle/10 rounded-lg" />
      </div>
      <div className="flex gap-2">
        <div className="h-10 flex-1 bg-sc-fg-subtle/10 rounded-xl" />
        <div className="h-10 w-16 bg-sc-fg-subtle/10 rounded-xl" />
        <div className="h-10 w-10 bg-sc-fg-subtle/10 rounded-xl" />
      </div>
    </div>
  );
}
