// Dialog
export type { FileMetadata, LocalSourceData, UrlSourceData } from './add-source-dialog';
export { AddSourceDialog } from './add-source-dialog';

// Progress tracking
export type { ActiveCrawlOperation } from './crawl-progress';
export { CrawlProgressPanel } from './crawl-progress';
// Legacy card (prefer SourceCardEnhanced)
export { SourceCard, SourceCardSkeleton as SourceCardBasicSkeleton } from './source-card';
// Source cards (use Enhanced by default)
export type { CrawlProgress } from './source-card-enhanced';
export {
  SourceCardEnhanced,
  SourceCardSkeleton,
} from './source-card-enhanced';
