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
    color: '#8b85a0',
    bgClass: 'bg-[#8b85a0]/20',
    textClass: 'text-[#8b85a0]',
    icon: '○',
  },
  in_progress: {
    label: 'Crawling',
    color: '#e135ff',
    bgClass: 'bg-[#e135ff]/20',
    textClass: 'text-[#e135ff]',
    icon: '◉',
  },
  completed: {
    label: 'Completed',
    color: '#50fa7b',
    bgClass: 'bg-[#50fa7b]/20',
    textClass: 'text-[#50fa7b]',
    icon: '◆',
  },
  failed: {
    label: 'Failed',
    color: '#ff6363',
    bgClass: 'bg-[#ff6363]/20',
    textClass: 'text-[#ff6363]',
    icon: '✕',
  },
  partial: {
    label: 'Partial',
    color: '#f1fa8c',
    bgClass: 'bg-[#f1fa8c]/20',
    textClass: 'text-[#f1fa8c]',
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
