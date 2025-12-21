'use client';

import {
  AlertCircle,
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  FileText,
  Globe,
  Loader2,
  Square,
  X,
} from '@/components/ui/icons';
import { AnimatePresence, motion } from 'motion/react';
import { memo, useState } from 'react';

export interface ActiveCrawlOperation {
  id: string;
  sourceId: string;
  sourceName: string;
  sourceUrl: string;
  status:
    | 'starting'
    | 'discovering'
    | 'crawling'
    | 'processing'
    | 'completed'
    | 'error'
    | 'stopped';
  progress: number;
  message: string;
  startedAt: string;
  pagesProcessed: number;
  documentsCreated: number;
  errorsCount: number;
  currentUrl?: string;
  discoveredUrls?: string[];
  error?: string;
}

interface CrawlProgressProps {
  operations: ActiveCrawlOperation[];
  onStop?: (operationId: string) => void;
  onDismiss?: (operationId: string) => void;
  onViewSource?: (sourceId: string) => void;
}

const STATUS_CONFIG = {
  starting: { label: 'Starting', color: 'text-sc-yellow', bg: 'bg-sc-yellow/10' },
  discovering: { label: 'Discovering', color: 'text-sc-cyan', bg: 'bg-sc-cyan/10' },
  crawling: { label: 'Crawling', color: 'text-sc-purple', bg: 'bg-sc-purple/10' },
  processing: { label: 'Processing', color: 'text-sc-coral', bg: 'bg-sc-coral/10' },
  completed: { label: 'Completed', color: 'text-sc-green', bg: 'bg-sc-green/10' },
  error: { label: 'Error', color: 'text-sc-red', bg: 'bg-sc-red/10' },
  stopped: { label: 'Stopped', color: 'text-sc-fg-subtle', bg: 'bg-sc-fg-subtle/10' },
};

const CrawlOperationCard = memo(function CrawlOperationCard({
  operation,
  onStop,
  onDismiss,
  onViewSource,
}: {
  operation: ActiveCrawlOperation;
  onStop?: (id: string) => void;
  onDismiss?: (id: string) => void;
  onViewSource?: (id: string) => void;
}) {
  const [isExpanded, setIsExpanded] = useState(false);
  const statusConfig = STATUS_CONFIG[operation.status];
  const isActive = ['starting', 'discovering', 'crawling', 'processing'].includes(operation.status);
  const isFinished = ['completed', 'error', 'stopped'].includes(operation.status);

  return (
    <motion.div
      layout
      initial={{ opacity: 0, y: -20, scale: 0.95 }}
      animate={{ opacity: 1, y: 0, scale: 1 }}
      exit={{ opacity: 0, x: 100, scale: 0.95 }}
      transition={{ duration: 0.2 }}
      className={`relative bg-sc-bg-base border rounded-xl overflow-hidden ${
        isActive
          ? 'border-sc-purple/40'
          : isFinished
            ? 'border-sc-fg-subtle/20'
            : 'border-sc-fg-subtle/20'
      }`}
    >
      {/* Progress bar */}
      {isActive && (
        <div className="absolute top-0 left-0 right-0 h-0.5 bg-sc-bg-dark overflow-hidden">
          <motion.div
            initial={{ width: 0 }}
            animate={{ width: `${operation.progress}%` }}
            transition={{ duration: 0.3 }}
            className="h-full bg-gradient-to-r from-sc-purple to-sc-cyan"
          />
        </div>
      )}

      <div className="p-4">
        {/* Header */}
        <div className="flex items-start justify-between gap-3 mb-3">
          <div className="flex items-center gap-3 min-w-0 flex-1">
            {/* Status Icon */}
            <div className={`shrink-0 p-2 rounded-lg ${statusConfig.bg}`}>
              {isActive ? (
                <Loader2 width={16} height={16} className={`${statusConfig.color} animate-spin`} />
              ) : operation.status === 'completed' ? (
                <CheckCircle2 width={16} height={16} className={statusConfig.color} />
              ) : operation.status === 'error' ? (
                <AlertCircle width={16} height={16} className={statusConfig.color} />
              ) : (
                <Square width={16} height={16} className={statusConfig.color} />
              )}
            </div>

            {/* Source Info */}
            <div className="min-w-0 flex-1">
              <h4 className="text-sm font-medium text-sc-fg-primary truncate">
                {operation.sourceName}
              </h4>
              <p className="text-xs text-sc-fg-subtle truncate">{operation.message}</p>
            </div>
          </div>

          {/* Actions */}
          <div className="flex items-center gap-1">
            <span
              className={`px-2 py-0.5 text-xs font-medium rounded ${statusConfig.bg} ${statusConfig.color}`}
            >
              {statusConfig.label}
            </span>
            {isActive && onStop && (
              <button
                type="button"
                onClick={() => onStop(operation.id)}
                className="p-1.5 text-sc-fg-subtle hover:text-sc-red hover:bg-sc-red/10 rounded-lg transition-colors"
                title="Stop crawl"
              >
                <Square width={14} height={14} />
              </button>
            )}
            {isFinished && onDismiss && (
              <button
                type="button"
                onClick={() => onDismiss(operation.id)}
                className="p-1.5 text-sc-fg-subtle hover:text-sc-fg-primary hover:bg-sc-bg-highlight rounded-lg transition-colors"
                title="Dismiss"
              >
                <X width={14} height={14} />
              </button>
            )}
          </div>
        </div>

        {/* Stats Grid */}
        <div className="grid grid-cols-3 gap-3 mb-3">
          <div className="text-center p-2 bg-sc-bg-dark rounded-lg">
            <p className="text-lg font-bold text-sc-cyan">{operation.pagesProcessed}</p>
            <p className="text-[10px] text-sc-fg-subtle uppercase tracking-wide">Pages</p>
          </div>
          <div className="text-center p-2 bg-sc-bg-dark rounded-lg">
            <p className="text-lg font-bold text-sc-green">{operation.documentsCreated}</p>
            <p className="text-[10px] text-sc-fg-subtle uppercase tracking-wide">Docs</p>
          </div>
          <div className="text-center p-2 bg-sc-bg-dark rounded-lg">
            <p
              className={`text-lg font-bold ${operation.errorsCount > 0 ? 'text-sc-red' : 'text-sc-fg-muted'}`}
            >
              {operation.errorsCount}
            </p>
            <p className="text-[10px] text-sc-fg-subtle uppercase tracking-wide">Errors</p>
          </div>
        </div>

        {/* Current URL */}
        {isActive && operation.currentUrl && (
          <div className="mb-3 p-2 bg-sc-bg-dark rounded-lg">
            <p className="text-[10px] text-sc-fg-subtle uppercase tracking-wide mb-1">
              Currently Processing
            </p>
            <p className="text-xs text-sc-cyan font-mono truncate">{operation.currentUrl}</p>
          </div>
        )}

        {/* Error Message */}
        {operation.status === 'error' && operation.error && (
          <div className="mb-3 p-2 bg-sc-red/10 border border-sc-red/20 rounded-lg">
            <p className="text-xs text-sc-red">{operation.error}</p>
          </div>
        )}

        {/* Discovered URLs Expandable */}
        {operation.discoveredUrls && operation.discoveredUrls.length > 0 && (
          <div className="border-t border-sc-fg-subtle/10 pt-2">
            <button
              type="button"
              onClick={() => setIsExpanded(!isExpanded)}
              className="w-full flex items-center justify-between text-xs text-sc-fg-subtle hover:text-sc-fg-muted transition-colors"
            >
              <span>{operation.discoveredUrls.length} discovered URLs</span>
              {isExpanded ? <ChevronUp width={14} height={14} /> : <ChevronDown width={14} height={14} />}
            </button>

            <AnimatePresence>
              {isExpanded && (
                <motion.div
                  initial={{ height: 0, opacity: 0 }}
                  animate={{ height: 'auto', opacity: 1 }}
                  exit={{ height: 0, opacity: 0 }}
                  className="overflow-hidden"
                >
                  <div className="mt-2 max-h-32 overflow-y-auto space-y-1">
                    {operation.discoveredUrls.map((url, i) => (
                      <p key={i} className="text-xs text-sc-fg-subtle font-mono truncate">
                        {url}
                      </p>
                    ))}
                  </div>
                </motion.div>
              )}
            </AnimatePresence>
          </div>
        )}

        {/* View Source Button */}
        {isFinished && onViewSource && (
          <button
            type="button"
            onClick={() => onViewSource(operation.sourceId)}
            className="w-full mt-2 px-3 py-2 text-sm text-sc-fg-muted hover:text-sc-purple hover:bg-sc-purple/10 rounded-lg transition-colors flex items-center justify-center gap-2"
          >
            <Globe width={14} height={14} />
            View Source
          </button>
        )}
      </div>
    </motion.div>
  );
});

export const CrawlProgressPanel = memo(function CrawlProgressPanel({
  operations,
  onStop,
  onDismiss,
  onViewSource,
}: CrawlProgressProps) {
  const [isCollapsed, setIsCollapsed] = useState(false);

  const activeOperations = operations.filter(op =>
    ['starting', 'discovering', 'crawling', 'processing'].includes(op.status)
  );
  const finishedOperations = operations.filter(op =>
    ['completed', 'error', 'stopped'].includes(op.status)
  );

  if (operations.length === 0) return null;

  return (
    <div className="fixed bottom-4 right-4 z-40 w-80 max-h-[60vh] flex flex-col bg-sc-bg-elevated border border-sc-fg-subtle/20 rounded-2xl shadow-2xl shadow-black/40 overflow-hidden">
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-sc-fg-subtle/10 bg-sc-bg-base">
        <div className="flex items-center gap-2">
          {activeOperations.length > 0 && (
            <span className="relative flex h-2 w-2">
              <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-sc-purple opacity-75" />
              <span className="relative inline-flex rounded-full h-2 w-2 bg-sc-purple" />
            </span>
          )}
          <h3 className="text-sm font-semibold text-sc-fg-primary">Crawl Operations</h3>
          <span className="px-1.5 py-0.5 text-xs bg-sc-bg-dark text-sc-fg-muted rounded">
            {operations.length}
          </span>
        </div>
        <button
          type="button"
          onClick={() => setIsCollapsed(!isCollapsed)}
          className="p-1 text-sc-fg-subtle hover:text-sc-fg-primary transition-colors"
        >
          {isCollapsed ? <ChevronUp width={16} height={16} /> : <ChevronDown width={16} height={16} />}
        </button>
      </div>

      {/* Content */}
      <AnimatePresence>
        {!isCollapsed && (
          <motion.div
            initial={{ height: 0 }}
            animate={{ height: 'auto' }}
            exit={{ height: 0 }}
            className="overflow-hidden"
          >
            <div className="p-3 space-y-3 overflow-y-auto max-h-[calc(60vh-60px)]">
              {/* Active Operations */}
              {activeOperations.length > 0 && (
                <div className="space-y-2">
                  <h4 className="text-xs text-sc-fg-subtle uppercase tracking-wide flex items-center gap-2">
                    <Loader2 width={12} height={12} className="animate-spin text-sc-purple" />
                    Active ({activeOperations.length})
                  </h4>
                  <AnimatePresence mode="popLayout">
                    {activeOperations.map(op => (
                      <CrawlOperationCard
                        key={op.id}
                        operation={op}
                        onStop={onStop}
                        onViewSource={onViewSource}
                      />
                    ))}
                  </AnimatePresence>
                </div>
              )}

              {/* Finished Operations */}
              {finishedOperations.length > 0 && (
                <div className="space-y-2">
                  <h4 className="text-xs text-sc-fg-subtle uppercase tracking-wide flex items-center gap-2">
                    <FileText width={12} height={12} />
                    Recent ({finishedOperations.length})
                  </h4>
                  <AnimatePresence mode="popLayout">
                    {finishedOperations.map(op => (
                      <CrawlOperationCard
                        key={op.id}
                        operation={op}
                        onDismiss={onDismiss}
                        onViewSource={onViewSource}
                      />
                    ))}
                  </AnimatePresence>
                </div>
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      {/* Collapsed Summary */}
      {isCollapsed && activeOperations.length > 0 && (
        <div className="px-4 py-2 text-xs text-sc-fg-muted">
          {activeOperations.length} active crawl{activeOperations.length > 1 ? 's' : ''}...
        </div>
      )}
    </div>
  );
});
