// =============================================================================
// Source/Crawl Status Styling
// =============================================================================

export const CRAWL_STATUSES = ['pending', 'in_progress', 'completed', 'failed', 'partial'] as const;
export type CrawlStatusType = (typeof CRAWL_STATUSES)[number];

export const CRAWL_STATUS_CONFIG: Record<
  CrawlStatusType,
  { label: string; color: string; bgClass: string; textClass: string; icon: string }
> = {
  pending: {
    label: 'Pending',
    color: 'var(--sc-fg-subtle)',
    bgClass: 'bg-sc-fg-subtle/20',
    textClass: 'text-sc-fg-muted',
    icon: '○',
  },
  in_progress: {
    label: 'Crawling',
    color: 'var(--sc-purple)',
    bgClass: 'bg-sc-purple/20',
    textClass: 'text-sc-purple',
    icon: '◉',
  },
  completed: {
    label: 'Completed',
    color: 'var(--sc-green)',
    bgClass: 'bg-sc-green/20',
    textClass: 'text-sc-green',
    icon: '◆',
  },
  failed: {
    label: 'Failed',
    color: 'var(--sc-red)',
    bgClass: 'bg-sc-red/20',
    textClass: 'text-sc-red',
    icon: '✕',
  },
  partial: {
    label: 'Partial',
    color: 'var(--sc-yellow)',
    bgClass: 'bg-sc-yellow/20',
    textClass: 'text-sc-yellow',
    icon: '◈',
  },
};

export const SOURCE_TYPES = ['website', 'github', 'local', 'api_docs'] as const;
export type SourceTypeValue = (typeof SOURCE_TYPES)[number];

export const SOURCE_TYPE_CONFIG: Record<SourceTypeValue, { label: string; icon: string }> = {
  website: { label: 'Website', icon: '⊕' },
  github: { label: 'GitHub', icon: '◈' },
  local: { label: 'Local', icon: '◇' },
  api_docs: { label: 'API Docs', icon: '⚙' },
};
