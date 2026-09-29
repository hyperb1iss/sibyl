import type { Metadata } from 'next';
import { Suspense } from 'react';

import { SearchSkeleton } from '@/components/suspense-boundary';
import { fetchStats } from '@/lib/api-server';
import { SearchContent } from './search-content';

export const metadata: Metadata = {
  title: 'Search',
  description: 'Semantic search across your knowledge graph',
};

interface PageProps {
  searchParams: Promise<{ mode?: string; q?: string }>;
}

export default async function SearchPage({ searchParams }: PageProps) {
  const params = await searchParams;
  const query = params.q || '';
  // Project selection hydrates on the client; prefetching here would widen it.
  const stats = await fetchStats().catch(() => undefined);

  return (
    <Suspense fallback={<SearchSkeleton />}>
      <SearchContent initialQuery={query} initialStats={stats} />
    </Suspense>
  );
}
